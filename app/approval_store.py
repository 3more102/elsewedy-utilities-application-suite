from __future__ import annotations

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import current_user
from .database import db, now


class ApprovalTransitionConflict(RuntimeError):
    """Raised when an approval's target record changed before it was claimed."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def decide_approval_atomic(conn, approval_id: int, body, user: dict) -> dict:
    decision = body.decision.lower().strip()
    if decision not in ('approve', 'reject'):
        raise HTTPException(400, 'Decision must be approve or reject')

    approval = conn.execute(
        'SELECT * FROM approval_requests WHERE id=?', (approval_id,)
    ).fetchone()
    if not approval:
        raise KeyError('Approval request not found')
    approval = dict(approval)
    if approval['status'] != 'Pending':
        raise ApprovalTransitionConflict('Approval request is already decided')

    allowed = (
        user['role'] in ('admin', 'maintenance_manager')
        or approval['assigned_user_id'] == user['id']
        or (
            approval['assigned_role']
            and approval['assigned_role'] == user['role']
        )
        or _application._delegation_active(conn, approval, user['id'])
    )
    if not allowed:
        raise HTTPException(403, 'This approval is not assigned to your role or user')

    target = 'Approved' if decision == 'approve' else 'Rejected'

    # Claim the business record before the approval row. Direct domain-specific
    # approval paths use the same business-row -> approval-row lock order, which
    # avoids introducing a cross-route deadlock inversion.
    if approval['record_type'] == 'work_order':
        record = conn.execute(
            'SELECT * FROM work_orders WHERE id=?', (approval['record_id'],)
        ).fetchone()
        if not record:
            raise KeyError('Work order not found')
        record = dict(record)
        changed = conn.execute(
            '''UPDATE work_orders SET status=?,updated_at=?
               WHERE id=? AND status='Submitted' ''',
            (target, now(), record['id']),
        )
        if not _rowcount_one(changed):
            current = conn.execute(
                'SELECT status FROM work_orders WHERE id=?', (record['id'],)
            ).fetchone()
            status = current['status'] if current else 'missing'
            raise ApprovalTransitionConflict(
                f'Work order is {status}, not Submitted'
            )
        module = 'Work Management'
        record_code = record['wo_no']
    elif approval['record_type'] == 'purchase_requisition':
        record = conn.execute(
            'SELECT * FROM purchase_requisitions WHERE id=?',
            (approval['record_id'],),
        ).fetchone()
        if not record:
            raise KeyError('Purchase requisition not found')
        record = dict(record)
        changed = conn.execute(
            '''UPDATE purchase_requisitions
               SET status=?,approved_at=?
               WHERE id=? AND status='Submitted' ''',
            (
                target,
                now() if target == 'Approved' else None,
                record['id'],
            ),
        )
        if not _rowcount_one(changed):
            current = conn.execute(
                'SELECT status FROM purchase_requisitions WHERE id=?',
                (record['id'],),
            ).fetchone()
            status = current['status'] if current else 'missing'
            raise ApprovalTransitionConflict(
                f'Purchase requisition is {status}, not Submitted'
            )
        module = 'Procurement'
        record_code = record['pr_no']
    else:
        raise HTTPException(400, 'Unsupported approval record type')

    claimed = conn.execute(
        '''UPDATE approval_requests
           SET status=?,decided_at=?,decided_by=?,comments=?
           WHERE id=? AND status='Pending' ''',
        (target, now(), user['id'], body.comments, approval_id),
    )
    if not _rowcount_one(claimed):
        # The business-record claim above is in this same transaction, so this
        # conflict rolls that change back as well instead of splitting state.
        raise ApprovalTransitionConflict('Approval request is already decided')

    _application.workflow_event(
        conn,
        module,
        approval['record_type'],
        record['id'],
        record_code,
        decision.upper(),
        record['status'],
        target,
        user['id'],
        body.comments,
    )
    append_audit(
        conn,
        user['id'],
        decision.upper(),
        module,
        record_code,
        record['status'],
        target,
    )
    _application.notify(
        conn,
        'Approval decision',
        f"{approval['record_code']} was {target.lower()}",
        'Info',
        approval['requested_by'],
        None,
        'work' if approval['record_type'] == 'work_order' else 'procurement',
        approval['record_code'],
    )
    return {'ok': True, 'status': target, 'record_code': approval['record_code']}


def install_atomic_approval_route() -> None:
    app = _application.app
    marker = '_euas_atomic_approval_decision'
    if getattr(app.state, marker, False):
        return

    path = '/api/approvals/{approval_id}/decision'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def decide_approval_route(
        approval_id: int,
        body: _application.ApprovalDecisionIn,
        user=Depends(current_user),
    ):
        try:
            with db() as conn:
                return decide_approval_atomic(conn, approval_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ApprovalTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    _application.decide_approval = decide_approval_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
