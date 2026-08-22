from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException

from . import application as _application
from .auth import require_roles
from .database import db, now


WORK_ORDER_NUMBER_LOCK_ID = 1
_legacy_next_no = _application.next_no


class WorkCreationConflict(RuntimeError):
    """Raised when a stale work-creation prerequisite loses its claim."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def ensure_work_order_creation_lock(conn) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS work_order_creation_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO work_order_creation_lock(id,guard) VALUES(?,0)',
        (WORK_ORDER_NUMBER_LOCK_ID,),
    )


def _lock_work_order_numbering(conn) -> None:
    locked = conn.execute(
        'UPDATE work_order_creation_lock SET guard=guard WHERE id=?',
        (WORK_ORDER_NUMBER_LOCK_ID,),
    )
    if not _rowcount_one(locked):
        raise RuntimeError('work-order number coordinator is unavailable')


def next_no_with_work_order_lock(conn, table, column, prefix, start):
    """Serialize only the shared WO number namespace; delegate everything else."""
    if str(table) == 'work_orders' and str(column) == 'wo_no':
        _lock_work_order_numbering(conn)
    return _legacy_next_no(conn, table, column, prefix, start)


def install_work_order_number_allocator() -> None:
    _application.next_no = next_no_with_work_order_lock


def create_alarm_work_order_atomic(conn, alarm_id: int, body, user: dict) -> dict:
    """Create at most one work order for an alarm under concurrent requests."""
    claimed_alarm = conn.execute(
        '''UPDATE operational_alarms
           SET work_order_id=work_order_id
           WHERE id=?''',
        (alarm_id,),
    )
    if not _rowcount_one(claimed_alarm):
        raise KeyError('Alarm not found')

    alarm = conn.execute(
        '''SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,
                  a.asset_no,a.location_id
           FROM operational_alarms oa
           JOIN telemetry_channels tc ON tc.id=oa.channel_id
           JOIN assets a ON a.id=oa.asset_id
           WHERE oa.id=?''',
        (alarm_id,),
    ).fetchone()
    if not alarm:
        raise KeyError('Alarm not found')
    alarm = dict(alarm)

    if alarm.get('work_order_id'):
        work = conn.execute(
            'SELECT id,wo_no FROM work_orders WHERE id=?',
            (alarm['work_order_id'],),
        ).fetchone()
        if not work:
            raise WorkCreationConflict('alarm references a missing work order')
        return {'id': work['id'], 'wo_no': work['wo_no'], 'existing': True}

    number = _application.next_no(
        conn, 'work_orders', 'wo_no', 'WO-', 10026
    )
    priority = 'Critical' if alarm['severity'] == 'Critical' else 'High'
    finish = body.target_finish or (
        date.today() + timedelta(days=1 if priority == 'Critical' else 2)
    ).isoformat()
    title = f"Investigate {alarm['channel_name']} alarm"
    description = (
        f"Generated from {alarm['alarm_no']} on {alarm['asset_no']}. "
        f"{alarm['message']}"
    )
    created = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,description,asset_id,location_id,priority,status,
             work_type,failure_code,requested_by,assigned_to,supervisor_id,
             target_start,target_finish,estimated_hours,instructions,
             created_at,updated_at
           ) VALUES(?,?,?,?,?,?,'Submitted','Corrective Maintenance',
                    ?,?,?,?,?,?,?,?, ?,?)''',
        (
            number,
            title,
            description,
            alarm['asset_id'],
            alarm['location_id'],
            priority,
            f"ALARM-{alarm['channel_code']}",
            user['id'],
            body.assigned_to,
            body.supervisor_id,
            date.today().isoformat(),
            finish,
            2,
            body.notes
            or f"Validate {alarm['channel_name']} reading and investigate root cause.",
            now(),
            now(),
        ),
    )
    work_id = int(created.lastrowid)

    linked = conn.execute(
        '''UPDATE operational_alarms SET work_order_id=?
           WHERE id=? AND work_order_id IS NULL''',
        (work_id, alarm_id),
    )
    if not _rowcount_one(linked):
        raise WorkCreationConflict('alarm work-order link changed concurrently')

    _application._ensure_work_sla(conn, work_id)
    _application.create_approval(
        conn,
        'Work Management',
        'work_order',
        work_id,
        number,
        f'Approve {number} — {title}',
        user['id'],
        assigned_user_id=body.supervisor_id,
        assigned_role=None if body.supervisor_id else 'maintenance_manager',
    )
    _application.workflow_event(
        conn,
        'Work Management',
        'work_order',
        work_id,
        number,
        'ALARM GENERATED',
        '',
        'Submitted',
        user['id'],
        alarm['alarm_no'],
    )
    _application.emit_event(
        conn,
        'operations.alarm.work_order_created',
        'alarm',
        alarm['alarm_no'],
        {'alarm_no': alarm['alarm_no'], 'work_order': number},
    )
    _application.audit(
        conn,
        user['id'],
        'CREATE WORK FROM ALARM',
        'Utilities Operations',
        alarm['alarm_no'],
        '',
        number,
    )
    return {'id': work_id, 'wo_no': number, 'existing': False}


def install_alarm_work_order_route() -> None:
    app = _application.app
    marker = '_euas_alarm_work_order_atomicity'
    if getattr(app.state, marker, False):
        return

    path = '/api/alarms/{alarm_id}/work-order'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def alarm_create_work_order_route(
        alarm_id: int,
        body: _application.AlarmWorkOrderIn,
        user=Depends(
            require_roles(
                'admin',
                'asset_manager',
                'maintenance_manager',
                'planner',
                'supervisor',
            )
        ),
    ):
        try:
            with db() as conn:
                return create_alarm_work_order_atomic(conn, alarm_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except WorkCreationConflict as exc:
            raise HTTPException(409, str(exc))

    _application.alarm_create_work_order = alarm_create_work_order_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
