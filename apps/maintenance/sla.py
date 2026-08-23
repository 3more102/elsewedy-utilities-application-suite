from __future__ import annotations

from datetime import datetime, timedelta

from core.database import now


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def ensure_work_sla(conn, work_order_id: int, force: bool = False) -> dict | None:
    work = conn.execute(
        'SELECT id,priority,status,created_at,actual_start,actual_finish FROM work_orders WHERE id=?',
        (work_order_id,),
    ).fetchone()
    if not work:
        return None
    existing = conn.execute('SELECT * FROM work_order_sla WHERE work_order_id=?', (work_order_id,)).fetchone()
    if existing and not force:
        return dict(existing)
    policy = conn.execute(
        'SELECT * FROM sla_policies WHERE priority=? AND active=1', (work['priority'],)
    ).fetchone() or conn.execute("SELECT * FROM sla_policies WHERE priority='Medium' AND active=1").fetchone()
    if not policy:
        return None
    created = _dt(work['created_at'])
    response_due = created + timedelta(minutes=policy['response_minutes'])
    resolution_due = created + timedelta(minutes=policy['resolution_minutes'])
    first = work['actual_start']
    resolved = work['actual_finish']
    response_status = 'Pending' if not first else ('Met' if _dt(first) <= response_due else 'Breached')
    resolution_status = 'Pending' if not resolved else ('Met' if _dt(resolved) <= resolution_due else 'Breached')
    if existing:
        conn.execute(
            '''UPDATE work_order_sla SET policy_id=?,response_due=?,resolution_due=?,first_response_at=?,resolved_at=?,
               response_status=?,resolution_status=?,updated_at=? WHERE work_order_id=?''',
            (
                policy['id'], response_due.isoformat(timespec='seconds'), resolution_due.isoformat(timespec='seconds'),
                first, resolved, response_status, resolution_status, now(), work_order_id,
            ),
        )
    else:
        conn.execute(
            '''INSERT INTO work_order_sla(
                 work_order_id,policy_id,response_due,resolution_due,first_response_at,resolved_at,response_status,resolution_status,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                work_order_id, policy['id'], response_due.isoformat(timespec='seconds'), resolution_due.isoformat(timespec='seconds'),
                first, resolved, response_status, resolution_status, now(),
            ),
        )
    return dict(conn.execute('SELECT * FROM work_order_sla WHERE work_order_id=?', (work_order_id,)).fetchone())


def backfill_work_order_slas(conn) -> int:
    missing = conn.execute(
        'SELECT id FROM work_orders WHERE id NOT IN (SELECT work_order_id FROM work_order_sla)'
    ).fetchall()
    for work in missing:
        ensure_work_sla(conn, work['id'])
    return len(missing)


def mark_sla_response(conn, work_order_id: int, at_value: str) -> None:
    sla = ensure_work_sla(conn, work_order_id)
    if not sla:
        return
    status = 'Met' if _dt(at_value) <= _dt(sla['response_due']) else 'Breached'
    conn.execute(
        'UPDATE work_order_sla SET first_response_at=?,response_status=?,updated_at=? WHERE work_order_id=?',
        (at_value, status, now(), work_order_id),
    )


def mark_sla_resolution(conn, work_order_id: int, at_value: str) -> None:
    sla = ensure_work_sla(conn, work_order_id)
    if not sla:
        return
    status = 'Met' if _dt(at_value) <= _dt(sla['resolution_due']) else 'Breached'
    conn.execute(
        'UPDATE work_order_sla SET resolved_at=?,resolution_status=?,updated_at=? WHERE work_order_id=?',
        (at_value, status, now(), work_order_id),
    )
