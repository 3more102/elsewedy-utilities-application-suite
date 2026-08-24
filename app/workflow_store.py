from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


class WorkflowTransitionConflict(RuntimeError):
    """Raised when another transaction wins a workflow state transition."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


DISPATCH_WORK_STATES = {
    'accept': ('Assigned', 'In Progress'),
    'enroute': ('Assigned', 'In Progress'),
    'arrive': ('Assigned', 'In Progress'),
    # Field completion may legitimately trail direct work completion/closure;
    # allow it to settle the dispatch record without reopening the work order.
    'complete': ('In Progress', 'Completed', 'Closed'),
}


def _guard_dispatch_work_state(conn, dispatch: dict, action: str) -> None:
    """Lock and validate the linked work state for one dispatch transition.

    The dispatch row is claimed before this helper runs. The no-op guarded work
    update therefore preserves the established dispatch -> work lock order while
    making the linked work state part of the same transaction. If another route
    completed/closed the work first, this transition rolls back its dispatch
    claim instead of advancing stale field-service state.
    """
    allowed = DISPATCH_WORK_STATES.get(action)
    if not allowed:
        return
    placeholders = ','.join('?' for _ in allowed)
    changed = conn.execute(
        f'''UPDATE work_orders SET status=status
            WHERE id=? AND status IN ({placeholders})''',
        (dispatch['work_order_id'], *allowed),
    )
    if _rowcount_one(changed):
        return
    current = conn.execute(
        'SELECT status FROM work_orders WHERE id=?',
        (dispatch['work_order_id'],),
    ).fetchone()
    status = current['status'] if current else 'missing'
    raise WorkflowTransitionConflict(
        f'Dispatch action {action} is not valid while work order is {status}'
    )


def transition_work_atomic(conn, wo_id: int, body, user: dict) -> dict:
    work = conn.execute('SELECT * FROM work_orders WHERE id=?', (wo_id,)).fetchone()
    if not work:
        raise KeyError('Work order not found')
    work = dict(work)
    action = body.action.lower()
    target = _application.TRANSITIONS.get(work['status'], {}).get(action)
    if not target:
        raise WorkflowTransitionConflict(
            f"Action '{body.action}' is not valid from {work['status']}"
        )
    if action in _application.ACTION_ROLES and user['role'] not in _application.ACTION_ROLES[action]:
        raise HTTPException(403, f"Role {user['role']} cannot perform {action}")
    if (
        user['role'] == 'technician'
        and action in ('start', 'pause', 'complete')
        and work['assigned_to'] != user['id']
    ):
        raise HTTPException(403, 'Technicians can only execute work assigned to them')
    if action == 'assign' and not work['assigned_to']:
        raise WorkflowTransitionConflict('Assign a technician before moving to Assigned')
    if action == 'cancel':
        holder = _application._active_dispatch_holder(conn, wo_id)
        if holder:
            raise WorkflowTransitionConflict(
                'Cancel the active dispatch before cancelling the work order'
            )

    stamp = now()
    fields: dict[str, object] = {'status': target, 'updated_at': stamp}
    if action == 'start':
        fields['actual_start'] = stamp
    if action == 'complete':
        fields['actual_finish'] = stamp
        fields['completion_notes'] = body.notes or work['completion_notes']
        fields['technician_signature'] = (
            body.signature or work.get('technician_signature', '')
        )

    assignments = ','.join(f'{key}=?' for key in fields)
    changed = conn.execute(
        f'UPDATE work_orders SET {assignments} WHERE id=? AND status=?',
        (*fields.values(), wo_id, work['status']),
    )
    if not _rowcount_one(changed):
        current = conn.execute(
            'SELECT status FROM work_orders WHERE id=?', (wo_id,)
        ).fetchone()
        current_status = current['status'] if current else 'missing'
        raise WorkflowTransitionConflict(
            f"Action '{body.action}' lost a concurrent transition; current status is {current_status}"
        )

    if action == 'start':
        _application._mark_sla_response(conn, wo_id, fields['actual_start'])
    if action == 'complete':
        _application._mark_sla_resolution(conn, wo_id, fields['actual_finish'])
    if action == 'cancel':
        _application._settle_cancelled_work(conn, wo_id, user['id'], body.notes)
    if action in ('submit', 'resubmit'):
        _application.create_approval(
            conn,
            'Work Management',
            'work_order',
            wo_id,
            work['wo_no'],
            f"Approve {work['wo_no']} — {work['title']}",
            user['id'],
            assigned_user_id=work['supervisor_id'],
            assigned_role=None if work['supervisor_id'] else 'maintenance_manager',
        )
    if action == 'approve':
        _application.resolve_approval(
            conn,
            'Work Management',
            'work_order',
            wo_id,
            'approve',
            user['id'],
            body.notes,
        )
    if target == 'Closed' and work['asset_id']:
        conn.execute(
            'UPDATE assets SET last_maintenance=?,updated_at=? WHERE id=?',
            (date.today().isoformat(), now(), work['asset_id']),
        )

    _application.workflow_event(
        conn,
        'Work Management',
        'work_order',
        wo_id,
        work['wo_no'],
        action.upper(),
        work['status'],
        target,
        user['id'],
        body.notes,
    )
    append_audit(
        conn,
        user['id'],
        action.upper(),
        'Work Management',
        work['wo_no'],
        work['status'],
        target,
    )
    _application.notify(
        conn,
        'Work order status changed',
        f"{work['wo_no']} is now {target}",
        'Info',
        work['requested_by'],
        None,
        'work',
        work['wo_no'],
    )
    return {'ok': True, 'status': target}


def transition_dispatch_atomic(conn, dispatch_id: int, body, user: dict) -> dict:
    action = body.action.lower().replace(' ', '')
    mapping = {
        'accept': ('Dispatched', 'Accepted', 'accepted_at'),
        'enroute': ('Accepted', 'En Route', 'enroute_at'),
        'arrive': ('En Route', 'On Site', 'arrived_at'),
        'complete': ('On Site', 'Completed', 'completed_at'),
    }
    dispatch = conn.execute(
        '''SELECT d.*,w.wo_no,w.status work_status
           FROM dispatch_assignments d
           JOIN work_orders w ON w.id=d.work_order_id
           WHERE d.id=?''',
        (dispatch_id,),
    ).fetchone()
    if not dispatch:
        raise KeyError('Dispatch not found')
    dispatch = dict(dispatch)

    elevated = user['role'] in ('admin', 'maintenance_manager', 'planner', 'supervisor')
    if user['role'] == 'technician' and dispatch['technician_user_id'] != user['id']:
        raise HTTPException(403, 'Technicians can only update their own dispatch')

    if action == 'cancel':
        if not elevated:
            raise HTTPException(403, 'Only planners/supervisors can cancel dispatch')
        if dispatch['status'] in ('Completed', 'Cancelled'):
            raise WorkflowTransitionConflict(f"Dispatch is {dispatch['status']}")
        target = 'Cancelled'
        field = 'cancelled_at'
        expected_statuses = None
    else:
        if action not in mapping:
            raise HTTPException(
                400, 'Action must be accept, enroute, arrive, complete or cancel'
            )
        expected, target, field = mapping[action]
        if dispatch['status'] != expected:
            raise WorkflowTransitionConflict(
                f"Action {action} requires {expected}, current status is {dispatch['status']}"
            )
        expected_statuses = (expected,)

    stamp = now()
    if expected_statuses is None:
        changed = conn.execute(
            f'''UPDATE dispatch_assignments
                SET status=?,{field}=?,
                    notes=CASE WHEN ?<>'' THEN ? ELSE notes END
                WHERE id=? AND status NOT IN ('Completed','Cancelled')''',
            (target, stamp, body.notes, body.notes, dispatch_id),
        )
    else:
        changed = conn.execute(
            f'''UPDATE dispatch_assignments
                SET status=?,{field}=?,
                    notes=CASE WHEN ?<>'' THEN ? ELSE notes END
                WHERE id=? AND status=?''',
            (
                target,
                stamp,
                body.notes,
                body.notes,
                dispatch_id,
                expected_statuses[0],
            ),
        )
    if not _rowcount_one(changed):
        current = conn.execute(
            'SELECT status FROM dispatch_assignments WHERE id=?', (dispatch_id,)
        ).fetchone()
        status = current['status'] if current else 'missing'
        raise WorkflowTransitionConflict(
            f'Concurrent dispatch transition won; current status is {status}'
        )

    # Make linked work state part of the same transactional claim. The helper
    # locks work after dispatch, matching the established cross-entity lock order.
    _guard_dispatch_work_state(conn, dispatch, action)

    if action == 'arrive':
        work_changed = conn.execute(
            """UPDATE work_orders
               SET status='In Progress',actual_start=COALESCE(actual_start,?),updated_at=?
               WHERE id=? AND status='Assigned'""",
            (stamp, stamp, dispatch['work_order_id']),
        )
        if _rowcount_one(work_changed):
            _application._mark_sla_response(conn, dispatch['work_order_id'], stamp)
            _application.workflow_event(
                conn,
                'Work Management',
                'work_order',
                dispatch['work_order_id'],
                dispatch['wo_no'],
                'ARRIVE',
                'Assigned',
                'In Progress',
                user['id'],
                body.notes,
            )

    append_audit(
        conn,
        user['id'],
        'DISPATCH ' + action.upper(),
        'Field Service',
        dispatch['dispatch_no'],
        dispatch['status'],
        target,
    )
    return {'ok': True, 'status': target}


def toggle_work_task_atomic(conn, wo_id: int, task_id: int, user: dict) -> dict:
    """Flip one work-order task with a single audited state transition.

    The toggle itself is the historical contract: every sequential call is a
    real transition and keeps its audit record. Concurrency safety comes from
    claiming the transition with an expected-status predicate, so simultaneous
    identical toggles produce exactly one committed transition and one audit
    instead of duplicated evidence from stale reads.
    """
    work = conn.execute(
        'SELECT wo_no FROM work_orders WHERE id=?', (wo_id,)
    ).fetchone()
    if not work:
        raise KeyError('Work order not found')
    task = conn.execute(
        'SELECT * FROM work_order_tasks WHERE id=? AND work_order_id=?',
        (task_id, wo_id),
    ).fetchone()
    if not task:
        raise KeyError('Task not found')

    new_status = 'Pending' if task['status'] == 'Completed' else 'Completed'
    changed = conn.execute(
        '''UPDATE work_order_tasks
           SET status=?,completed_at=?
           WHERE id=? AND status=?''',
        (
            new_status,
            _application.now() if new_status == 'Completed' else None,
            task_id,
            task['status'],
        ),
    )
    if not _rowcount_one(changed):
        current = conn.execute(
            'SELECT status FROM work_order_tasks WHERE id=?', (task_id,)
        ).fetchone()
        status = current['status'] if current else 'missing'
        raise WorkflowTransitionConflict(
            f'Concurrent task transition won; current status is {status}'
        )

    append_audit(
        conn,
        user['id'],
        'TASK ' + new_status.upper(),
        'Work Management',
        work['wo_no'],
        task['status'],
        new_status,
    )
    return {'ok': True, 'status': new_status}


def append_work_note_atomic(conn, wo_id: int, body, user: dict) -> dict:
    """Append one work-order note without losing concurrent notes.

    The historical handler read the comments thread and wrote the concatenated
    result back unconditionally, so two simultaneous notes could overwrite each
    other. The append is now claimed against the observed ``updated_at`` stamp
    (the established EUAS compare-and-set pattern): a loser re-reads current
    state client-side and retries instead of silently dropping evidence.
    """
    work = conn.execute(
        'SELECT * FROM work_orders WHERE id=?', (wo_id,)
    ).fetchone()
    if not work:
        raise KeyError('Work order not found')
    work = dict(work)

    entry = f"[{_application.now()}] {user['full_name']}: {body.note}"
    new_comments = ((work['comments'] or '') + '\n' + entry).strip()
    # The claim includes the observed thread contents, not only updated_at:
    # that stamp has second resolution, so two concurrent appends in the same
    # second would otherwise both satisfy an updated_at-only predicate and the
    # loser would silently erase the winner's note.
    changed = conn.execute(
        '''UPDATE work_orders
           SET comments=?,updated_at=?
           WHERE id=? AND updated_at=? AND comments=?''',
        (
            new_comments,
            _application.now(),
            wo_id,
            work['updated_at'],
            work['comments'] or '',
        ),
    )
    if not _rowcount_one(changed):
        raise WorkflowTransitionConflict(
            'Work order changed concurrently; retry appending the note'
        )

    _application.audit(
        conn,
        user['id'],
        'ADD NOTE',
        'Work Management',
        work['wo_no'],
        work['comments'],
        new_comments,
    )
    return {'ok': True}


def install_workflow_transition_routes() -> None:
    app = _application.app
    marker = '_euas_workflow_transition_atomicity'
    if getattr(app.state, marker, False):
        return

    replacements = {
        ('/api/work-orders/{wo_id}/transition', 'POST'),
        ('/api/dispatch/{dispatch_id}/transition', 'POST'),
        ('/api/work-orders/{wo_id}/tasks/{task_id}/toggle', 'POST'),
        ('/api/work-orders/{wo_id}/notes', 'POST'),
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

    @app.post('/api/work-orders/{wo_id}/transition')
    def transition_work_route(
        wo_id: int,
        body: _application.TransitionIn,
        user=Depends(require_roles(*_application.WORK_ROLES)),
    ):
        try:
            with db() as conn:
                return transition_work_atomic(conn, wo_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except WorkflowTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/dispatch/{dispatch_id}/transition')
    def transition_dispatch_route(
        dispatch_id: int,
        body: _application.DispatchTransitionIn,
        user=Depends(require_roles(*_application.WORK_ROLES)),
    ):
        try:
            with db() as conn:
                return transition_dispatch_atomic(conn, dispatch_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except WorkflowTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/work-orders/{wo_id}/tasks/{task_id}/toggle')
    def toggle_work_task_route(
        wo_id: int,
        task_id: int,
        user=Depends(require_roles(*_application.WORK_ROLES)),
    ):
        try:
            with db() as conn:
                return toggle_work_task_atomic(conn, wo_id, task_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except WorkflowTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/work-orders/{wo_id}/notes')
    def add_work_note_route(
        wo_id: int,
        body: _application.NoteIn,
        user=Depends(require_roles(*_application.WORK_ROLES)),
    ):
        try:
            with db() as conn:
                return append_work_note_atomic(conn, wo_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except WorkflowTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    _application.transition_work = transition_work_route
    _application.transition_dispatch = transition_dispatch_route
    _application.toggle_work_task = toggle_work_task_route
    _application.add_work_note = add_work_note_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
