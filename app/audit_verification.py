from __future__ import annotations

"""Audit-chain verification helpers.

This module extends the existing append-only audit implementation. It does not
write audit records; it validates records produced by audit_store.append_audit.
"""

from .database import audit_digest


class AuditIntegrityError(RuntimeError):
    pass


def _rows(conn):
    return conn.execute(
        """SELECT id,user_id,action,module,record_id,old_value,new_value,
                  created_at,prev_hash,audit_hash
             FROM audit_logs
             ORDER BY id ASC"""
    ).fetchall()


def verify_audit_chain(conn) -> bool:
    previous = ""
    for row in _rows(conn):
        expected = audit_digest(
            previous,
            row["user_id"],
            row["action"],
            row["module"],
            row["record_id"],
            row["old_value"],
            row["new_value"],
            row["created_at"],
        )
        if row["prev_hash"] != previous or row["audit_hash"] != expected:
            raise AuditIntegrityError(
                f"audit chain verification failed at record {row['id']}"
            )
        previous = row["audit_hash"]
    return True


def replay_audit_history(conn):
    """Return deterministic historical reconstruction events.

    Consumers can replay these events to rebuild an audit timeline without
    trusting a mutable current-state table.
    """
    verify_audit_chain(conn)
    return [dict(row) for row in _rows(conn)]
