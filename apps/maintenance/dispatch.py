from __future__ import annotations

from apps.audit import audit
from apps.events import workflow_event
from apps.notifications import notify
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no

from .sla import mark_sla_response


class DispatchError(RuntimeError):
    status_code = 409


class DispatchNotFound(DispatchError):
    status_code = 404


class DispatchForbidden(DispatchError):
    status_code = 403


class DispatchInvalid(DispatchError):
    status_code = 400


class DispatchConflict(DispatchError):
    status_code = 409


ACTIVE_DISPATCH_STATES = ('Dispatched', 'Accepted', 'En Route', 'On Site')
DISPATCH_TRANSITIONS = {
    'accept': ('Dispatched', 'Accepted', 'accepted_at'),
    'enroute': ('Accepted', 'En Route', 'enroute_at'),
    'arrive': ('En Route', 'On Site', 'arrived_at'),
    'complete': ('On Site', 'Completed', 'completed_at'),
}


def _acquire_dispatch_lock(conn, work_order_id: int, technician_user_id: int) -> None:
    """Serialize dispatch creation for the work order and technician.

    PostgreSQL uses row locks. SQLite has no row-level FOR UPDATE, so an
    IMMEDIATE transaction is used only when this command owns the transaction.
    That keeps the race deterministic without a process-local mutex.
    """
    if DB_BACKEND == 'postgresql':
        conn.execute('SELECT id FROM work_orders WHERE id=? FOR UPDATE', (work_order_id,)).fetchone()
        conn.execute('SELECT id FROM users WHERE id=? FOR UPDATE', (technician_user_id,)).fetchone()
        return
    if not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def create_dispatch(conn, work_order_id: int, technician_user_id: int, actor: dict, *, eta_minutes=None, notes: str = '') -> dict:
    _acquire_dispatch_lock(conn, work_order_id, technician_user_id)
    row = conn.execute('SELECT * FROM work_orders WHERE id=?', (work_order_id,)).fetchone()
    if not row:
        raise DispatchNotFound('Work order not found')
    work = dict(row)
    if work['status'] not in ('Approved', 'Assigned'):
        raise DispatchConflict('Work order must be Approved or Assigned before dispatch')
    tech_row = conn.execute(
        "SELECT u.id,u.full_name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.active=1 AND r.code='technician'",
        (technician_user_id,),
    ).fetchone()
    if not tech_row:
        raise DispatchNotFound('Active technician not found')
    tech = dict(tech_row)
    active_work = conn.execute(
        """SELECT dispatch_no FROM dispatch_assignments
           WHERE work_order_id=? AND status IN ('Dispatched','Accepted','En Route','On Site')
           ORDER BY id DESC LIMIT 1""",
        (work_order_id,),
    ).fetchone()
    if active_work:
        raise DispatchConflict(
            f"Work order already has active dispatch {active_work['dispatch_no']}; cancel it before redispatch"
        )
    busy = conn.execute(
        """SELECT d.dispatch_no,w.wo_no FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id
           WHERE d.technician_user_id=? AND d.work_order_id<>? AND d.status IN ('Dispatched','Accepted','En Route','On Site')
           ORDER BY d.id DESC LIMIT 1""",
        (technician_user_id, work_order_id),
    ).fetchone()
    if busy:
        raise DispatchConflict(f"Technician already has active dispatch {busy['dispatch_no']} for {busy['wo_no']}")

    stamp = now()
    dispatch_no = next_no(conn, 'dispatch_assignments', 'dispatch_no', 'DSP-', 40001)
    cur = conn.execute(
        """INSERT INTO dispatch_assignments(
             dispatch_no,work_order_id,technician_user_id,dispatched_by,status,eta_minutes,notes,dispatched_at
           ) VALUES(?,?,?,?, 'Dispatched',?,?,?)""",
        (dispatch_no, work_order_id, technician_user_id, actor['id'], eta_minutes, notes, stamp),
    )
    updated = conn.execute(
        "UPDATE work_orders SET assigned_to=?,status='Assigned',updated_at=? WHERE id=? AND status IN ('Approved','Assigned')",
        (technician_user_id, stamp, work_order_id),
    )
    if updated.rowcount != 1:
        raise DispatchConflict('Work order state changed concurrently; dispatch was not applied')
    workflow_event(
        conn, 'Work Management', 'work_order', work_order_id, work['wo_no'],
        'DISPATCH', work['status'], 'Assigned', actor['id'], f'{dispatch_no} → {tech["full_name"]}',
    )
    audit(
        conn, actor['id'], 'DISPATCH', 'Field Service', dispatch_no, '',
        {'work_order': work['wo_no'], 'technician': tech['full_name'], 'eta_minutes': eta_minutes},
    )
    notify(
        conn, 'Dispatch assigned', f'{dispatch_no} — {work["wo_no"]}: {work["title"]}',
        'High' if work['priority'] in ('Emergency', 'Critical', 'High') else 'Info',
        technician_user_id, None, 'dispatch', dispatch_no,
    )
    return {'id': cur.lastrowid, 'dispatch_no': dispatch_no, 'status': 'Dispatched'}


def transition_dispatch(conn, dispatch_id: int, action: str, actor: dict, *, notes: str = '') -> dict:
    normalized = action.lower().replace(' ', '')
    row = conn.execute(
        '''SELECT d.*,w.wo_no,w.status work_status FROM dispatch_assignments d
           JOIN work_orders w ON w.id=d.work_order_id WHERE d.id=?''',
        (dispatch_id,),
    ).fetchone()
    if not row:
        raise DispatchNotFound('Dispatch not found')
    dispatch = dict(row)
    elevated = actor['role'] in ('admin', 'maintenance_manager', 'planner', 'supervisor')
    if actor['role'] == 'technician' and dispatch['technician_user_id'] != actor['id']:
        raise DispatchForbidden('Technicians can only update their own dispatch')
    if normalized == 'cancel':
        if not elevated:
            raise DispatchForbidden('Only planners/supervisors can cancel dispatch')
        if dispatch['status'] in ('Completed', 'Cancelled'):
            raise DispatchConflict(f"Dispatch is {dispatch['status']}")
        expected = dispatch['status']
        target, field = 'Cancelled', 'cancelled_at'
    else:
        if normalized not in DISPATCH_TRANSITIONS:
            raise DispatchInvalid('Action must be accept, enroute, arrive, complete or cancel')
        expected, target, field = DISPATCH_TRANSITIONS[normalized]
        if dispatch['status'] != expected:
            raise DispatchConflict(
                f"Action {normalized} requires {expected}, current status is {dispatch['status']}"
            )

    stamp = now()
    cur = conn.execute(
        f'''UPDATE dispatch_assignments SET status=?,{field}=?,
            notes=CASE WHEN ?<>'' THEN ? ELSE notes END WHERE id=? AND status=?''',
        (target, stamp, notes, notes, dispatch_id, expected),
    )
    if cur.rowcount != 1:
        raise DispatchConflict('Dispatch state changed concurrently; reload and retry')
    if normalized == 'arrive' and dispatch['work_status'] == 'Assigned':
        work_cur = conn.execute(
            """UPDATE work_orders SET status='In Progress',actual_start=COALESCE(actual_start,?),updated_at=?
               WHERE id=? AND status='Assigned'""",
            (stamp, stamp, dispatch['work_order_id']),
        )
        if work_cur.rowcount == 1:
            mark_sla_response(conn, dispatch['work_order_id'], stamp)
            workflow_event(
                conn, 'Work Management', 'work_order', dispatch['work_order_id'], dispatch['wo_no'],
                'ARRIVE', 'Assigned', 'In Progress', actor['id'], notes,
            )
    audit(
        conn, actor['id'], 'DISPATCH ' + normalized.upper(), 'Field Service', dispatch['dispatch_no'],
        dispatch['status'], target,
    )
    return {'ok': True, 'status': target}
