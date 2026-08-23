from __future__ import annotations

from datetime import datetime

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import current_user
from .database import db, now


class ApprovalTransitionConflict(RuntimeError):
    """Raised when an approval's target record changed before it was claimed."""


APPROVAL_SELECT = """SELECT ap.*,req.full_name requested_by_name,dec.full_name decided_by_name,ass.full_name assigned_user_name
FROM approval_requests ap
JOIN users req ON req.id=ap.requested_by
LEFT JOIN users dec ON dec.id=ap.decided_by
LEFT JOIN users ass ON ass.id=ap.assigned_user_id"""


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


def list_approvals_view(conn, status: str, module: str, user: dict) -> list[dict]:
    """Approval queue with historical visibility scoping and delegation flags.

    Privileged roles see the whole queue; every other authenticated role sees
    only approvals assigned to their user/role, requested by them, or actively
    delegated to them. This mirrors the historical role ceiling exactly.
    """
    sql = APPROVAL_SELECT + ' WHERE 1=1'
    args: list[object] = []
    if status:
        sql += ' AND ap.status=?'
        args.append(status)
    if module:
        sql += ' AND ap.module=?'
        args.append(module)
    if user['role'] not in ('admin', 'maintenance_manager', 'executive'):
        sql += """ AND (ap.assigned_user_id=? OR ap.assigned_role=? OR ap.requested_by=? OR EXISTS (
          SELECT 1 FROM approval_delegations d WHERE d.delegator_user_id=ap.assigned_user_id AND d.delegate_user_id=?
          AND d.active=1 AND d.start_at<=? AND d.end_at>=? AND (d.module='*' OR d.module=ap.module)
        ))"""
        stamp = now()
        args += [user['id'], user['role'], user['id'], user['id'], stamp, stamp]
    sql += " ORDER BY CASE ap.status WHEN 'Pending' THEN 0 ELSE 1 END,ap.id DESC"
    result = _application.rows(conn.execute(sql, args))
    for a in result:
        a['delegated_to_me'] = bool(
            a['status'] == 'Pending'
            and _application._delegation_active(conn, a, user['id'])
        )
    return result


def create_delegation(conn, body, user: dict) -> dict:
    if body.delegate_user_id == user['id']:
        raise HTTPException(400, 'You cannot delegate approvals to yourself')
    try:
        start = _application._dt(body.start_at)
        end = _application._dt(body.end_at)
    except Exception:
        raise HTTPException(400, 'Invalid delegation date/time')
    if end <= start:
        raise HTTPException(400, 'Delegation end must be after start')
    if (end - start).days > 366:
        raise HTTPException(400, 'Delegation cannot exceed 366 days')
    delegate = _application.get_or_404(
        conn,
        'SELECT u.id,u.active,u.full_name FROM users u WHERE u.id=?',
        (body.delegate_user_id,),
        'Delegate user not found',
    )
    if not delegate['active']:
        raise HTTPException(409, 'Delegate user is inactive')
    cur = conn.execute(
        '''INSERT INTO approval_delegations(
             delegator_user_id,delegate_user_id,module,start_at,end_at,active,
             created_by,created_at
           ) VALUES(?,?,?,?,?,1,?,?)''',
        (
            user['id'],
            body.delegate_user_id,
            body.module or '*',
            start.isoformat(timespec='seconds'),
            end.isoformat(timespec='seconds'),
            user['id'],
            now(),
        ),
    )
    append_audit(
        conn,
        user['id'],
        'DELEGATE',
        'Approvals',
        str(cur.lastrowid),
        '',
        {
            'delegate': delegate['full_name'],
            'module': body.module or '*',
            'start_at': body.start_at,
            'end_at': body.end_at,
        },
    )
    _application.notify(
        conn,
        'Approval delegation',
        f"{user['full_name']} delegated approvals to you through {end.date().isoformat()}",
        'Info',
        body.delegate_user_id,
        None,
        'approvals',
        str(cur.lastrowid),
    )
    return {'id': cur.lastrowid, 'active': True}


def list_delegations_view(conn, user: dict) -> list[dict]:
    sql = """SELECT d.*,src.full_name delegator_name,src.username delegator_username,dst.full_name delegate_name,dst.username delegate_username,creator.full_name created_by_name
    FROM approval_delegations d JOIN users src ON src.id=d.delegator_user_id JOIN users dst ON dst.id=d.delegate_user_id JOIN users creator ON creator.id=d.created_by"""
    args: list[object] = []
    if user['role'] != 'admin':
        sql += ' WHERE d.delegator_user_id=? OR d.delegate_user_id=?'
        args = [user['id'], user['id']]
    sql += ' ORDER BY d.active DESC,d.end_at DESC,d.id DESC'
    return _application.rows(conn.execute(sql, args))


def deactivate_delegation(conn, delegation_id: int, user: dict) -> dict:
    delegation = _application.get_or_404(
        conn,
        'SELECT * FROM approval_delegations WHERE id=?',
        (delegation_id,),
        'Delegation not found',
    )
    if (
        user['role'] != 'admin'
        and delegation['delegator_user_id'] != user['id']
    ):
        raise HTTPException(
            403, 'Only the delegator or administrator can deactivate this delegation'
        )
    conn.execute(
        'UPDATE approval_delegations SET active=0 WHERE id=?', (delegation_id,)
    )
    append_audit(
        conn,
        user['id'],
        'DEACTIVATE DELEGATION',
        'Approvals',
        str(delegation_id),
        1,
        0,
    )
    return {'ok': True}


def install_approval_routes() -> None:
    """Own the approval queue and delegation APIs inside the approval domain.

    The atomic decision route is installed separately; this installer owns the
    read/delegation surface. Behavior, paths, models and role ceilings are
    preserved verbatim from the historical application.py definitions.
    """
    app = _application.app
    marker = '_euas_approval_routes'
    if getattr(app.state, marker, False):
        return

    removals = [
        ('/api/approvals', {'GET'}),
        ('/api/approval-delegations', {'GET'}),
        ('/api/approval-delegations', {'POST'}),
        ('/api/approval-delegations/{delegation_id}/deactivate', {'PATCH'}),
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

    @app.get('/api/approvals')
    def list_approvals_route(
        status: str = 'Pending',
        module: str = '',
        user=Depends(current_user),
    ):
        with db() as conn:
            return list_approvals_view(conn, status, module, user)

    @app.get('/api/approval-delegations')
    def list_delegations_route(user=Depends(current_user)):
        with db() as conn:
            return list_delegations_view(conn, user)

    @app.post('/api/approval-delegations')
    def create_delegation_route(
        body: _application.ApprovalDelegationIn,
        user=Depends(current_user),
    ):
        with db() as conn:
            return create_delegation(conn, body, user)

    @app.patch('/api/approval-delegations/{delegation_id}/deactivate')
    def deactivate_delegation_route(
        delegation_id: int,
        user=Depends(current_user),
    ):
        with db() as conn:
            return deactivate_delegation(conn, delegation_id, user)

    _application.list_approvals = list_approvals_route
    _application.list_approval_delegations = list_delegations_route
    _application.create_approval_delegation = create_delegation_route
    _application.deactivate_approval_delegation = deactivate_delegation_route
    app.openapi_schema = None
    setattr(app.state, marker, True)


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
