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


def verify_audit_chain_report(conn) -> dict:
    """Return the historical API evidence shape for the whole chain.

    This is the single shared implementation behind ``/api/audit/integrity``,
    the replay validator and the operational CLI, so all three can never drift
    apart on digest or linkage rules.
    """
    previous = ""
    checked = 0
    for row in _rows(conn):
        checked += 1
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
        if (
            (row["prev_hash"] or "") != previous
            or (row["audit_hash"] or "") != expected
        ):
            return {
                'valid': False,
                'checked': checked,
                'first_invalid_id': row['id'],
                'head_hash': previous,
            }
        previous = row["audit_hash"]
    return {'valid': True, 'checked': checked, 'first_invalid_id': None, 'head_hash': previous}


def verify_audit_chain(conn) -> bool:
    report = verify_audit_chain_report(conn)
    if not report['valid']:
        raise AuditIntegrityError(
            f"audit chain verification failed at record {report['first_invalid_id']}"
        )
    return True


def replay_audit_history(conn):
    """Return deterministic historical reconstruction events.

    Consumers can replay these events to rebuild an audit timeline without
    trusting a mutable current-state table.
    """
    verify_audit_chain(conn)
    return [dict(row) for row in _rows(conn)]
