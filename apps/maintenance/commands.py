from __future__ import annotations

from datetime import date

from apps.approvals import create_approval, resolve_approval
from apps.audit import audit
from apps.events import workflow_event
from apps.fmea import get_record as get_fmea_record
from apps.notifications import notify
from core.database import now
from core.shared import next_no

from .sla import ensure_work_sla, mark_sla_resolution, mark_sla_response
from .workflow import InvalidWorkTransition, WorkTransitionForbidden, transition_target, validate_transition_actor


class MaintenanceCommandError(RuntimeError):
    status_code = 409


class WorkOrderNotFound(MaintenanceCommandError):
    status_code = 404


class WorkOrderConflict(MaintenanceCommandError):
    status_code = 409


def _work_order(conn, work_order_id: int) -> dict:
    row = conn.execute('SELECT * FROM work_orders WHERE id=?', (work_order_id,)).fetchone()
    if not row:
        raise WorkOrderNotFound('Work order not found')
    return dict(row)


def create_work_order(conn, data: dict, actor: dict) -> dict:
    """Create a Draft work order and all command-side evidence in one transaction."""
    payload = dict(data)
    linked_fmea = None
    if payload.get('asset_fmea_id'):
        linked_fmea = get_fmea_record(conn, int(payload['asset_fmea_id']), payload.get('asset_id'))
    asset_id = payload.get('asset_id') or (linked_fmea['asset_id'] if linked_fmea else None)
    location_id = payload.get('location_id')
    if asset_id and not location_id:
        row = conn.execute('SELECT location_id FROM assets WHERE id=?', (asset_id,)).fetchone()
        location_id = row['location_id'] if row else None
    failure_code = payload.get('failure_code') or (linked_fmea['mode_no'] if linked_fmea else '')
    work_no = next_no(conn, 'work_orders', 'wo_no', 'WO-', 10026)
    stamp = now()
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,asset_fmea_id,
             requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,safety_requirements,
             instructions,checklist,estimated_cost,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,'Draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            work_no, payload['title'], payload.get('description', ''), asset_id, location_id,
            payload.get('priority', 'Medium'), payload.get('work_type', 'Corrective Maintenance'), failure_code,
            payload.get('asset_fmea_id'), actor['id'], payload.get('assigned_to'), payload.get('supervisor_id'),
            payload.get('target_start'), payload.get('target_finish'), payload.get('estimated_hours', 0),
            payload.get('safety_requirements', ''), payload.get('instructions', ''), payload.get('checklist', ''),
            payload.get('estimated_cost', 0), stamp, stamp,
        ),
    )
    work_order_id = cur.lastrowid
    checklist = [
        item.strip()
        for item in str(payload.get('checklist') or '').replace('\n', ';').replace(',', ';').split(';')
        if item.strip()
    ]
    for sequence_no, task in enumerate(checklist, 1):
        conn.execute(
            "INSERT INTO work_order_tasks(work_order_id,sequence_no,task,status) VALUES(?,?,?,'Pending')",
            (work_order_id, sequence_no, task),
        )
    ensure_work_sla(conn, work_order_id)
    workflow_event(conn, 'Work Management', 'work_order', work_order_id, work_no, 'CREATE', '', 'Draft', actor['id'])
    if payload.get('assigned_to'):
        notify(
            conn, 'Work order assigned', f"{work_no} — {payload['title']}",
            'High' if payload.get('priority') in ('High', 'Critical', 'Emergency') else 'Info',
            payload['assigned_to'], None, 'work', work_no,
        )
    audit(conn, actor['id'], 'CREATE', 'Work Management', work_no, '', payload)
    return {'id': work_order_id, 'wo_no': work_no}


def update_work_order(conn, work_order_id: int, changes: dict, actor: dict) -> dict:
    """Apply the API-approved patch fields while preserving command-side SLA/audit behavior."""
    old = _work_order(conn, work_order_id)
    changes = {key: value for key, value in dict(changes).items() if value is not None}
    if not changes:
        return {'ok': True}
    conn.execute(
        'UPDATE work_orders SET ' + ','.join(f'{key}=?' for key in changes) + ',updated_at=? WHERE id=?',
        (*changes.values(), now(), work_order_id),
    )
    audit(conn, actor['id'], 'UPDATE', 'Work Management', old['wo_no'], old, changes)
    if 'priority' in changes:
        ensure_work_sla(conn, work_order_id, force=True)
    if changes.get('assigned_to'):
        notify(
            conn, 'Work order assigned', f"{old['wo_no']} — {changes.get('title', old['title'])}",
            'Info', changes['assigned_to'], None, 'work', old['wo_no'],
        )
    return {'ok': True}


def transition_work_order(conn, work_order_id: int, action: str, actor: dict, *, notes: str = '', signature: str = '') -> dict:
    """Advance one work-order state with a compare-and-swap guard against stale concurrent commands."""
    work = _work_order(conn, work_order_id)
    normalized = action.lower().strip()
    try:
        target = transition_target(work['status'], normalized)
        validate_transition_actor(work, normalized, actor)
    except WorkTransitionForbidden:
        raise
    except InvalidWorkTransition:
        raise

    stamp = now()
    fields: dict[str, object] = {'status': target, 'updated_at': stamp}
    if normalized == 'start':
        fields['actual_start'] = stamp
    if normalized == 'complete':
        fields['actual_finish'] = stamp
        fields['completion_notes'] = notes or work.get('completion_notes') or ''
        fields['technician_signature'] = signature or work.get('technician_signature') or ''

    cur = conn.execute(
        'UPDATE work_orders SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=? AND status=?',
        (*fields.values(), work_order_id, work['status']),
    )
    if cur.rowcount != 1:
        raise WorkOrderConflict('Work order state changed concurrently; reload and retry')

    if normalized == 'start':
        mark_sla_response(conn, work_order_id, str(fields['actual_start']))
    if normalized == 'complete':
        mark_sla_resolution(conn, work_order_id, str(fields['actual_finish']))
    if normalized in ('submit', 'resubmit'):
        create_approval(
            conn, 'Work Management', 'work_order', work_order_id, work['wo_no'],
            f"Approve {work['wo_no']} — {work['title']}", actor['id'],
            assigned_user_id=work.get('supervisor_id'),
            assigned_role=None if work.get('supervisor_id') else 'maintenance_manager',
        )
    if normalized == 'approve':
        resolve_approval(conn, 'Work Management', 'work_order', work_order_id, 'approve', actor['id'], notes)
    if target == 'Closed' and work.get('asset_id'):
        conn.execute(
            'UPDATE assets SET last_maintenance=?,updated_at=? WHERE id=?',
            (date.today().isoformat(), now(), work['asset_id']),
        )
    workflow_event(
        conn, 'Work Management', 'work_order', work_order_id, work['wo_no'],
        normalized.upper(), work['status'], target, actor['id'], notes,
    )
    audit(conn, actor['id'], normalized.upper(), 'Work Management', work['wo_no'], work['status'], target)
    notify(
        conn, 'Work order status changed', f"{work['wo_no']} is now {target}", 'Info',
        work.get('requested_by'), None, 'work', work['wo_no'],
    )
    return {'ok': True, 'status': target}
