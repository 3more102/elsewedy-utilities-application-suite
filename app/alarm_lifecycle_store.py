from __future__ import annotations

from fastapi import Depends, HTTPException

from . import application as _application
from .alarm_authorization import ALARM_ACKNOWLEDGE_ROLES, ALARM_CLOSE_ROLES
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


class AlarmLifecycleConflict(RuntimeError):
    """Raised when an alarm lifecycle transition is invalid or loses its claim."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _lock_and_load_alarm(conn, alarm_id: int) -> dict:
    locked = conn.execute(
        '''UPDATE operational_alarms
           SET occurrence_count=occurrence_count
           WHERE id=?''',
        (alarm_id,),
    )
    if not _rowcount_one(locked):
        raise KeyError('Alarm not found')
    row = conn.execute(
        'SELECT * FROM operational_alarms WHERE id=?',
        (alarm_id,),
    ).fetchone()
    if not row:
        raise KeyError('Alarm not found')
    return dict(row)


def acknowledge_alarm_atomic(conn, alarm_id: int, user: dict) -> dict:
    """Acknowledge Open once; replay Acknowledged and never regress Closed."""
    alarm = _lock_and_load_alarm(conn, alarm_id)
    status = alarm['status']

    if status == 'Acknowledged':
        return {'ok': True, 'status': 'Acknowledged'}
    if status != 'Open':
        raise AlarmLifecycleConflict(f'Alarm is {status}')

    stamp = now()
    changed = conn.execute(
        '''UPDATE operational_alarms
           SET status='Acknowledged',acknowledged_at=?,acknowledged_by=?
           WHERE id=? AND status='Open' ''',
        (stamp, user['id'], alarm_id),
    )
    if not _rowcount_one(changed):
        raise AlarmLifecycleConflict('Alarm acknowledge claim was lost')

    append_audit(
        conn,
        user['id'],
        'ACKNOWLEDGE ALARM',
        'Utilities Operations',
        alarm['alarm_no'],
        'Open',
        'Acknowledged',
    )
    return {'ok': True, 'status': 'Acknowledged'}


def close_alarm_atomic(conn, alarm_id: int, user: dict) -> dict:
    """Close any nonterminal alarm once and replay the terminal Closed state."""
    alarm = _lock_and_load_alarm(conn, alarm_id)
    status = alarm['status']

    if status == 'Closed':
        return {'ok': True, 'status': 'Closed'}

    stamp = now()
    changed = conn.execute(
        '''UPDATE operational_alarms
           SET status='Closed',closed_at=?,closed_by=?
           WHERE id=? AND status<>'Closed' ''',
        (stamp, user['id'], alarm_id),
    )
    if not _rowcount_one(changed):
        raise AlarmLifecycleConflict('Alarm close claim was lost')

    _application.emit_event(
        conn,
        'operations.alarm.closed',
        'alarm',
        alarm['alarm_no'],
        {'alarm_no': alarm['alarm_no'], 'asset_id': alarm['asset_id']},
    )
    append_audit(
        conn,
        user['id'],
        'CLOSE ALARM',
        'Utilities Operations',
        alarm['alarm_no'],
        status,
        'Closed',
    )
    return {'ok': True, 'status': 'Closed'}


def install_alarm_lifecycle_routes() -> None:
    app = _application.app
    marker = '_euas_alarm_lifecycle_atomicity'
    if getattr(app.state, marker, False):
        return

    replacements = {
        ('/api/alarms/{alarm_id}/acknowledge', 'POST'),
        ('/api/alarms/{alarm_id}/close', 'POST'),
    }
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and method in set(getattr(route, 'methods', set()) or set())
            for path, method in replacements
        )
    ]

    @app.post('/api/alarms/{alarm_id}/acknowledge')
    def acknowledge_alarm_route(
        alarm_id: int,
        user=Depends(require_roles(*ALARM_ACKNOWLEDGE_ROLES)),
    ):
        try:
            with db() as conn:
                return acknowledge_alarm_atomic(conn, alarm_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except AlarmLifecycleConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/alarms/{alarm_id}/close')
    def close_alarm_route(
        alarm_id: int,
        user=Depends(require_roles(*ALARM_CLOSE_ROLES)),
    ):
        try:
            with db() as conn:
                return close_alarm_atomic(conn, alarm_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except AlarmLifecycleConflict as exc:
            raise HTTPException(409, str(exc))

    _application.acknowledge_alarm = acknowledge_alarm_route
    _application.close_alarm = close_alarm_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
