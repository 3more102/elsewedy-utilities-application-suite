from __future__ import annotations

from datetime import date

from fastapi import Depends

from . import application as _application


PM_GENERATION_LOCK_ID = 1
_legacy_generate_due_pm = _application._generate_due_pm


class PMGenerationCoordinatorUnavailable(RuntimeError):
    """Raised when preventive-maintenance generation was not initialized."""


def ensure_pm_generation_lock(conn) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS pm_generation_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO pm_generation_lock(id,guard) VALUES(?,0)',
        (PM_GENERATION_LOCK_ID,),
    )


def generate_due_pm_atomic(conn, actor_id: int, target):
    """Serialize the complete due-plan scan/check/create cycle.

    The historical generator performs a check for an existing nonterminal work
    order and then creates a new work order/approval/audit in the same caller
    transaction. Serializing before that scan makes the check meaningful under
    PostgreSQL read-committed concurrency without changing business semantics.
    """
    locked = conn.execute(
        'UPDATE pm_generation_lock SET guard=guard WHERE id=?',
        (PM_GENERATION_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise PMGenerationCoordinatorUnavailable(
            'preventive maintenance generation coordinator is unavailable'
        )
    return _legacy_generate_due_pm(conn, actor_id, target)


def install_pm_generator() -> None:
    _application._generate_due_pm = generate_due_pm_atomic


def install_pm_routes() -> None:
    """Own the maintenance-plan API surface inside the PM domain.

    Behavior, paths, models and the historical WRITE_ROLES ceiling are
    preserved verbatim from the original application.py route definitions.
    Audit/numbering helpers resolve through the application module at call
    time, exactly as the historical handlers did.
    """
    app = _application.app
    marker = '_euas_pm_routes'
    if getattr(app.state, marker, False):
        return

    removals = [
        ('/api/maintenance-plans', {'GET'}),
        ('/api/maintenance-plans', {'POST'}),
        ('/api/maintenance-plans/generate', {'POST'}),
    ]
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and methods.intersection(set(getattr(route, 'methods', set()) or set()))
            for path, methods in removals
        )
    ]

    @app.get('/api/maintenance-plans')
    def list_pm_route(user=Depends(_application.current_user)):
        with _application.db() as conn:
            return _application.rows(
                conn.execute(
                    '''SELECT p.*,a.asset_no,a.name asset_name,a.meter_reading
                       FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id
                       ORDER BY COALESCE(p.next_due,'9999-12-31'),p.pm_no'''
                )
            )

    @app.post('/api/maintenance-plans')
    def create_pm_route(body: _application.PMIn, user=Depends(_application.require_roles(*_application.WRITE_ROLES))):
        with _application.db() as conn:
            no = _application.next_no(conn, 'maintenance_plans', 'pm_no', 'PM-', 1000)
            cur = conn.execute(
                '''INSERT INTO maintenance_plans(
                     pm_no,name,asset_id,trigger_type,interval_days,meter_interval,
                     next_due,priority,job_plan
                   ) VALUES(?,?,?,?,?,?,?,?,?)''',
                (
                    no,
                    body.name,
                    body.asset_id,
                    body.trigger_type,
                    body.interval_days,
                    body.meter_interval,
                    body.next_due,
                    body.priority,
                    body.job_plan,
                ),
            )
            _application.audit(
                conn,
                user['id'],
                'CREATE',
                'Preventive Maintenance',
                no,
                '',
                body.model_dump(),
            )
            return {'id': cur.lastrowid, 'pm_no': no}

    @app.post('/api/maintenance-plans/generate')
    def generate_pm_route(user=Depends(_application.require_roles(*_application.WRITE_ROLES))):
        with _application.db() as conn:
            generated = _application._generate_due_pm(conn, user['id'], date.today())
            return {'count': len(generated), 'generated': generated}

    _application.list_pm = list_pm_route
    _application.create_pm = create_pm_route
    _application.generate_pm = generate_pm_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
