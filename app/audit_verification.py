from __future__ import annotations

"""Audit-chain verification helpers.

This module extends the existing append-only audit implementation. It does not
write audit records; it validates records produced by audit_store.append_audit.
"""

from .config import DB_BACKEND
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


def _anchor_row(conn):
    """Return the singleton anchor row, or None on legacy databases without it."""
    if DB_BACKEND == 'postgresql':
        present = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='audit_chain_anchor'"
        ).fetchone()
    else:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_chain_anchor'"
        ).fetchone()
    if present is None:
        return None
    return conn.execute(
        'SELECT head_hash,record_count FROM audit_chain_anchor WHERE id=1'
    ).fetchone()


def _invalid(checked: int, head: str) -> dict:
    return {
        'valid': False,
        'checked': checked,
        'first_invalid_id': None,
        'head_hash': head,
    }


def verify_audit_chain_report(conn) -> dict:
    """Return the historical API evidence shape for the whole chain.

    This is the single shared implementation behind ``/api/audit/integrity``,
    the replay validator and the operational CLI, so all three can never drift
    apart on digest or linkage rules.

    Beyond hash-linkage validation, the chain head and record count are compared
    against the transactionally maintained anchor row so that deleting recent
    (tail) records — invisible to pure linkage checks — fails verification.
    Databases predating the anchor remain verifiable only while empty.
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

    anchor = _anchor_row(conn)
    if anchor is None:
        if checked:
            return _invalid(checked, previous)
    elif (
        (anchor['head_hash'] or '') != previous
        or int(anchor['record_count'] or 0) != checked
    ):
        return _invalid(checked, previous)
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
