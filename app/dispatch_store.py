from __future__ import annotations

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


ACTIVE_DISPATCH_STATUSES = ('Dispatched', 'Accepted', 'En Route', 'On Site')
DISPATCH_ASSIGN_ROLES = ('admin', 'maintenance_manager', 'planner', 'supervisor')


class DispatchAssignmentConflict(RuntimeError):
    """Raised when dispatch availability or the work snapshot is no longer valid."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def ensure_dispatch_assignment_lock(conn) -> None:
    """Create the singleton coordinator used only by dispatch assignment writes."""
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS dispatch_assignment_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO dispatch_assignment_lock(id,guard) VALUES(1,0)'
    )


def _active_dispatch_ids(conn, wo_id: int) -> tuple[int, ...]:
    rows = conn.execute(
        '''SELECT id FROM dispatch_assignments
           WHERE work_order_id=?
             AND status IN ('Dispatched','Accepted','En Route','On Site')
           ORDER BY id''',
        (wo_id,),
    ).fetchall()
    return tuple(int(row['id']) for row in rows)


def load_dispatch_work_snapshot(conn, wo_id: int) -> dict:
    work = conn.execute('SELECT * FROM work_orders WHERE id=?', (wo_id,)).fetchone()
    if not work:
        raise KeyError('Work order not found')
    work = dict(work)
    if work['status'] not in ('Approved', 'Assigned'):
        raise DispatchAssignmentConflict(
            'Work order must be Approved or Assigned before dispatch'
        )
    # The active-dispatch generation distinguishes a genuinely later re-dispatch
    # from two concurrent requests that captured the same Assigned work state.
    work['_dispatch_active_ids'] = _active_dispatch_ids(conn, wo_id)
    return work


def _lock_assignment_coordinator(conn) -> None:
    ensure_dispatch_assignment_lock(conn)
    locked = conn.execute(
        'UPDATE dispatch_assignment_lock SET guard=guard WHERE id=1'
    )
    if not _rowcount_one(locked):
        raise RuntimeError('dispatch assignment coordinator is unavailable')


def _lock_and_load_technician(conn, technician_user_id: int) -> dict:
    locked = conn.execute(
        'UPDATE users SET active=active WHERE id=?', (technician_user_id,)
    )
    if not _rowcount_one(locked):
        raise KeyError('Active technician not found')
    tech = conn.execute(
        '''SELECT u.id,u.full_name,u.active,r.code role
           FROM users u JOIN roles r ON r.id=u.role_id
           WHERE u.id=?''',
        (technician_user_id,),
    ).fetchone()
    if not tech or not tech['active'] or tech['role'] != 'technician':
        raise KeyError('Active technician not found')
    return dict(tech)


def _lock_active_dispatches_for_work(conn, wo_id: int) -> tuple[int, ...]:
    ids = _active_dispatch_ids(conn, wo_id)
    for dispatch_id in ids:
        conn.execute(
            'UPDATE dispatch_assignments SET status=status WHERE id=?',
            (dispatch_id,),
        )
    # A transition may have completed/cancelled an existing row while this
    # transaction waited for its lock, so compare the fresh active generation.
    return _active_dispatch_ids(conn, wo_id)


def assign_dispatch_atomic(conn, work_snapshot: dict, body, user: dict) -> dict:
    """Create/reassign a dispatch without stale availability or work overwrites.

    Lock order is coordinator -> technician -> active dispatch row(s) -> work row
    -> audit lock. The initial work and active-dispatch generation are captured
    before the coordinator so requests based on the same historical assignment
    cannot silently replace each other.
    """
    _lock_assignment_coordinator(conn)
    tech = _lock_and_load_technician(conn, body.technician_user_id)
    current_dispatch_ids = _lock_active_dispatches_for_work(conn, work_snapshot['id'])
    expected_dispatch_ids = tuple(work_snapshot.get('_dispatch_active_ids', ()))
    if current_dispatch_ids != expected_dispatch_ids:
        raise DispatchAssignmentConflict(
            'Dispatch assignment changed before this request could be claimed'
        )

    busy = conn.execute(
        '''SELECT d.dispatch_no,w.wo_no
           FROM dispatch_assignments d
           JOIN work_orders w ON w.id=d.work_order_id
           WHERE d.technician_user_id=?
             AND d.work_order_id<>?
             AND d.status IN ('Dispatched','Accepted','En Route','On Site')
           ORDER BY d.id DESC LIMIT 1''',
        (body.technician_user_id, work_snapshot['id']),
    ).fetchone()
    if busy:
        raise DispatchAssignmentConflict(
            f"Technician already has active dispatch {busy['dispatch_no']} for {busy['wo_no']}"
        )

    expected_assignee = (
        int(work_snapshot['assigned_to'])
        if work_snapshot.get('assigned_to') is not None
        else -1
    )
    claimed = conn.execute(
        '''UPDATE work_orders
           SET assigned_to=?,status='Assigned',updated_at=?
           WHERE id=? AND status=? AND COALESCE(assigned_to,-1)=?''',
        (
            body.technician_user_id,
            now(),
            work_snapshot['id'],
            work_snapshot['status'],
            expected_assignee,
        ),
    )
    if not _rowcount_one(claimed):
        current = conn.execute(
            'SELECT status,assigned_to FROM work_orders WHERE id=?',
            (work_snapshot['id'],),
        ).fetchone()
        if not current:
            raise KeyError('Work order not found')
        raise DispatchAssignmentConflict(
            'Work order assignment changed before this dispatch could be claimed'
        )

    stamp = now()
    conn.execute(
        '''UPDATE dispatch_assignments
           SET status='Cancelled',cancelled_at=?
           WHERE work_order_id=?
             AND status IN ('Dispatched','Accepted','En Route','On Site')''',
        (stamp, work_snapshot['id']),
    )

    number = _application.next_no(
        conn, 'dispatch_assignments', 'dispatch_no', 'DSP-', 40001
    )
    created = conn.execute(
        '''INSERT INTO dispatch_assignments(
             dispatch_no,work_order_id,technician_user_id,dispatched_by,status,
             eta_minutes,notes,dispatched_at
           ) VALUES(?,?,?,?,'Dispatched',?,?,?)''',
        (
            number,
            work_snapshot['id'],
            body.technician_user_id,
            user['id'],
            body.eta_minutes,
            body.notes,
            stamp,
        ),
    )

    _application.workflow_event(
        conn,
        'Work Management',
        'work_order',
        work_snapshot['id'],
        work_snapshot['wo_no'],
        'DISPATCH',
        work_snapshot['status'],
        'Assigned',
        user['id'],
        f"{number} → {tech['full_name']}",
    )
    append_audit(
        conn,
        user['id'],
        'DISPATCH',
        'Field Service',
        number,
        '',
        {
            'work_order': work_snapshot['wo_no'],
            'technician': tech['full_name'],
            'eta_minutes': body.eta_minutes,
        },
    )
    _application.notify(
        conn,
        'Dispatch assigned',
        f"{number} — {work_snapshot['wo_no']}: {work_snapshot['title']}",
        'High'
        if work_snapshot['priority'] in ('Emergency', 'Critical', 'High')
        else 'Info',
        body.technician_user_id,
        None,
        'dispatch',
        number,
    )
    return {'id': int(created.lastrowid), 'dispatch_no': number, 'status': 'Dispatched'}


def install_dispatch_assignment_route() -> None:
    app = _application.app
    marker = '_euas_dispatch_assignment_atomicity'
    if getattr(app.state, marker, False):
        return

    path = '/api/work-orders/{wo_id}/dispatch'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def dispatch_work_route(
        wo_id: int,
        body: _application.DispatchIn,
        user=Depends(require_roles(*DISPATCH_ASSIGN_ROLES)),
    ):
        try:
            with db() as conn:
                snapshot = load_dispatch_work_snapshot(conn, wo_id)
                return assign_dispatch_atomic(conn, snapshot, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except DispatchAssignmentConflict as exc:
            raise HTTPException(409, str(exc))

    _application.dispatch_work = dispatch_work_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
