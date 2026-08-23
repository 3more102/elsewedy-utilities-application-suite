from __future__ import annotations

from apps.notifications import notify
from core.database import now
from core.shared import next_no


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
) -> dict:
    """Create one pending approval per governed resource, idempotently."""
    existing = conn.execute(
        "SELECT * FROM approval_requests WHERE module=? AND record_type=? AND record_id=? AND status='Pending'",
        (module, record_type, record_id),
    ).fetchone()
    if existing:
        return dict(existing)
    approval_no = next_no(conn, 'approval_requests', 'approval_no', 'APR-', 9001)
    cur = conn.execute(
        """INSERT INTO approval_requests(
               approval_no,module,record_type,record_id,record_code,title,requested_by,assigned_role,assigned_user_id,status,requested_at
           ) VALUES(?,?,?,?,?,?,?,?,?,'Pending',?)""",
        (
            approval_no,
            module,
            record_type,
            record_id,
            record_code,
            title,
            requested_by,
            assigned_role,
            assigned_user_id,
            now(),
        ),
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
    return {'id': cur.lastrowid, 'approval_no': approval_no}


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
