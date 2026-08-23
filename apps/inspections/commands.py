from __future__ import annotations

from datetime import date, timedelta

from apps.approvals import create_approval
from apps.audit import audit
from apps.events import emit_event, workflow_event
from apps.maintenance import ensure_work_sla
from apps.notifications import notify
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no

from .workflow import corrective_required, inspection_result


class InspectionCommandError(RuntimeError):
    status_code = 409


class InspectionNotFound(InspectionCommandError):
    status_code = 404


class InspectionInvalid(InspectionCommandError):
    status_code = 400


class InspectionConflict(InspectionCommandError):
    status_code = 409


def _begin_write(conn) -> None:
    if DB_BACKEND == 'sqlite' and not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def _locked_inspection(conn, inspection_id: int) -> dict:
    _begin_write(conn)
    suffix = ' FOR UPDATE' if DB_BACKEND == 'postgresql' else ''
    row = conn.execute(f'SELECT * FROM inspections WHERE id=?{suffix}', (inspection_id,)).fetchone()
    if not row:
        raise InspectionNotFound('Inspection not found')
    return dict(row)


def create_inspection(conn, data: dict, actor_id: int) -> dict:
    payload = dict(data)
    items = list(payload.get('items') or ['Visual Condition', 'Leaks', 'Temperature', 'Noise', 'Grounding', 'Physical Damage'])
    if not items or any(not str(item).strip() for item in items):
        raise InspectionInvalid('Inspection must contain non-empty inspection items')
    number = next_no(conn, 'inspections', 'inspection_no', 'INS-', 5001)
    cur = conn.execute(
        """INSERT INTO inspections(
             inspection_no,template_name,asset_id,work_order_id,inspector_id,status,created_at
           ) VALUES(?,?,?,?,?,'Draft',?)""",
        (number, payload['template_name'], payload.get('asset_id'), payload.get('work_order_id'), actor_id, now()),
    )
    for item in items:
        conn.execute('INSERT INTO inspection_items(inspection_id,item_name) VALUES(?,?)', (cur.lastrowid, str(item).strip()))
    audit(conn, actor_id, 'CREATE', 'Inspections', number, '', payload)
    return {'id': cur.lastrowid, 'inspection_no': number}


def _validate_responses(conn, inspection_id: int, responses: list[dict]) -> list[dict]:
    items = [dict(row) for row in conn.execute('SELECT * FROM inspection_items WHERE inspection_id=? ORDER BY id', (inspection_id,)).fetchall()]
    expected = {int(item['id']) for item in items}
    by_id: dict[int, dict] = {}
    for response in responses:
        try:
            item_id = int(response.get('id'))
        except (TypeError, ValueError):
            raise InspectionInvalid('Each inspection response must reference a valid item id')
        if item_id in by_id:
            raise InspectionInvalid('Each inspection item may be answered only once')
        value = str(response.get('response') or 'N/A')
        if value not in ('Pass', 'Fail', 'N/A'):
            raise InspectionInvalid(f'Invalid inspection response: {value}')
        by_id[item_id] = dict(response, response=value)
    if set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        unknown = sorted(set(by_id) - expected)
        detail = []
        if missing:
            detail.append(f'missing item ids {missing}')
        if unknown:
            detail.append(f'unknown item ids {unknown}')
        raise InspectionInvalid('Inspection responses must cover every configured item (' + '; '.join(detail) + ')')
    return [by_id[int(item['id'])] for item in items]


def _create_corrective_work_order(conn, inspection: dict, actor_id: int) -> dict:
    asset = conn.execute('SELECT * FROM assets WHERE id=?', (inspection['asset_id'],)).fetchone() if inspection.get('asset_id') else None
    number = next_no(conn, 'work_orders', 'wo_no', 'WO-', 10026)
    stamp = now()
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,description,asset_id,location_id,priority,status,work_type,requested_by,target_start,target_finish,
             instructions,created_at,updated_at
           ) VALUES(?,?,?,?,?,'High','Submitted','Corrective Maintenance',?,?,?,?,?,?)''',
        (
            number, f"Corrective action from {inspection['inspection_no']}",
            f"Inspection {inspection['inspection_no']} failed. Review failed items and correct defects.",
            inspection.get('asset_id'), asset['location_id'] if asset else None, actor_id,
            date.today().isoformat(), (date.today() + timedelta(days=2)).isoformat(),
            'Review failed inspection items and implement corrective actions.', stamp, stamp,
        ),
    )
    ensure_work_sla(conn, cur.lastrowid)
    create_approval(
        conn, 'Work Management', 'work_order', cur.lastrowid, number,
        f"Approve {number} — Corrective action from {inspection['inspection_no']}", actor_id,
        assigned_role='maintenance_manager',
    )
    workflow_event(
        conn, 'Work Management', 'work_order', cur.lastrowid, number,
        'INSPECTION FAILED', '', 'Submitted', actor_id, inspection['inspection_no'],
    )
    audit(
        conn, actor_id, 'CREATE', 'Work Management', number, '',
        {'source': 'inspection', 'inspection_no': inspection['inspection_no'], 'status': 'Submitted'},
    )
    return {'id': cur.lastrowid, 'wo_no': number}


def submit_inspection(conn, inspection_id: int, data: dict, actor_id: int) -> dict:
    inspection = _locked_inspection(conn, inspection_id)
    if inspection['status'] == 'Completed':
        raise InspectionConflict('Inspection is already completed')
    if inspection['status'] not in ('Draft', 'In Progress'):
        raise InspectionConflict(f"Inspection cannot be submitted from {inspection['status']}")
    responses = _validate_responses(conn, inspection_id, list(data.get('responses') or []))
    result = inspection_result(responses)
    for response in responses:
        conn.execute(
            'UPDATE inspection_items SET response=?,reading=?,remarks=? WHERE id=? AND inspection_id=?',
            (response['response'], response.get('reading', ''), response.get('remarks', ''), response['id'], inspection_id),
        )
    corrective = None
    corrective_no = None
    if corrective_required(result, bool(data.get('create_corrective_on_fail', True))):
        work = _create_corrective_work_order(conn, inspection, actor_id)
        corrective = work['id']
        corrective_no = work['wo_no']
        conn.execute('UPDATE inspections SET corrective_wo_id=? WHERE id=?', (corrective, inspection_id))
        notify(
            conn, 'Inspection failed', f"{inspection['inspection_no']} failed and generated {corrective_no}",
            'High', None, 'planner', 'inspections', inspection['inspection_no'],
        )
    stamp = now()
    claimed = conn.execute(
        """UPDATE inspections SET status='Completed',result=?,inspected_at=?,remarks=?
           WHERE id=? AND status=?""",
        (result, stamp, data.get('remarks', ''), inspection_id, inspection['status']),
    )
    if claimed.rowcount != 1:
        raise InspectionConflict('Inspection state changed concurrently; submission was not applied')
    if result == 'Fail':
        emit_event(
            conn, 'inspection.failed', 'inspection', inspection['inspection_no'],
            {
                'inspection_id': inspection_id, 'inspection_no': inspection['inspection_no'],
                'corrective_work_order_id': corrective,
            },
        )
    audit(conn, actor_id, 'SUBMIT', 'Inspections', inspection['inspection_no'], inspection['status'], result)
    return {'ok': True, 'result': result, 'corrective_work_order_id': corrective}
