from __future__ import annotations

from apps.notifications import notify
from core.correlation import correlation_id as new_correlation_id
from core.database import now
from core.shared import next_no
from .evidence import append_evidence_event, capture_request_snapshot


def create_approval(
    conn,
    module: str,
    record_type: str,
    record_id: int,
    record_code: str,
    title: str,
    requested_by: int,
    assigned_role: str | None = None,
    assigned_user_id: int | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Create one pending approval per governed resource, idempotently, with immutable request fingerprint."""
    existing = conn.execute(
        "SELECT * FROM approval_requests WHERE module=? AND record_type=? AND record_id=? AND status='Pending'",
        (module, record_type, record_id),
    ).fetchone()
    if existing:
        return dict(existing)
    approval_no = next_no(conn, 'approval_requests', 'approval_no', 'APR-', 9001)
    requested_at = now()
    corr = correlation_id or new_correlation_id()
    snapshot_json, request_hash, resource_version = capture_request_snapshot(conn, record_type, record_id)
    cur = conn.execute(
        """INSERT INTO approval_requests(
               approval_no,module,record_type,record_id,record_code,title,requested_by,assigned_role,assigned_user_id,status,requested_at,
               request_snapshot_json,request_snapshot_hash,request_resource_version,correlation_id
           ) VALUES(?,?,?,?,?,?,?,?,?,'Pending',?,?,?,?,?)""",
        (
            approval_no, module, record_type, record_id, record_code, title, requested_by,
            assigned_role, assigned_user_id, requested_at, snapshot_json, request_hash, resource_version, corr,
        ),
    )
    approval_id = cur.lastrowid
    append_evidence_event(
        conn, 'ApprovalRequested', requested_by, approval_id=approval_id,
        resource_type=record_type, resource_id=record_id, resource_fingerprint=request_hash,
        correlation_id=corr, details={'approval_no': approval_no, 'module': module, 'record_code': record_code},
    )
    notify(
        conn,
        'Approval waiting',
        f'{record_code} requires approval',
        'Info',
        assigned_user_id,
        assigned_role,
        'approvals',
        approval_no,
    )
    return {
        'id': approval_id, 'approval_no': approval_no, 'request_snapshot_hash': request_hash,
        'request_resource_version': resource_version, 'correlation_id': corr,
    }


def resolve_approval(conn, module: str, record_type: str, record_id: int, decision: str, user_id: int, comments: str = '') -> dict | None:
    pending = conn.execute(
        """SELECT * FROM approval_requests
           WHERE module=? AND record_type=? AND record_id=? AND status='Pending'
           ORDER BY id DESC LIMIT 1""",
        (module, record_type, record_id),
    ).fetchone()
    if not pending:
        return None
    new_status = 'Approved' if decision.lower() == 'approve' else 'Rejected'
    conn.execute(
        'UPDATE approval_requests SET status=?,decided_at=?,decided_by=?,comments=? WHERE id=?',
        (new_status, now(), user_id, comments, pending['id']),
    )
    return dict(pending) | {'status': new_status}
