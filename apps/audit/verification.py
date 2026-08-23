from __future__ import annotations

from app.database import audit_digest


_AUDIT_SELECT = (
    "SELECT id,user_id,action,module,record_id,old_value,new_value,"
    "created_at,prev_hash,audit_hash FROM audit_logs ORDER BY id"
)


def reconstruct_audit_history(conn) -> list[dict]:
    """Rebuild chain relationships and verification evidence from persisted rows."""
    previous = ''
    history: list[dict] = []
    for row in conn.execute(_AUDIT_SELECT).fetchall():
        expected = audit_digest(
            previous,
            row['user_id'],
            row['action'],
            row['module'],
            row['record_id'],
            row['old_value'],
            row['new_value'],
            row['created_at'],
        )
        link_valid = (row['prev_hash'] or '') == previous
        hash_valid = (row['audit_hash'] or '') == expected
        history.append(
            {
                'id': row['id'],
                'user_id': row['user_id'],
                'action': row['action'],
                'module': row['module'],
                'record_id': row['record_id'],
                'created_at': row['created_at'],
                'prev_hash': row['prev_hash'] or '',
                'audit_hash': row['audit_hash'] or '',
                'expected_hash': expected,
                'link_valid': link_valid,
                'hash_valid': hash_valid,
                'valid': link_valid and hash_valid,
            }
        )
        previous = row['audit_hash'] or ''
    return history


def verify_audit_chain(conn) -> dict:
    """Verify the complete persisted audit chain and identify the first bad record."""
    history = reconstruct_audit_history(conn)
    for index, record in enumerate(history, start=1):
        if not record['valid']:
            prior_head = history[index - 2]['audit_hash'] if index > 1 else ''
            return {
                'valid': False,
                'checked': index,
                'first_invalid_id': record['id'],
                'head_hash': prior_head,
            }
    head_hash = history[-1]['audit_hash'] if history else ''
    return {
        'valid': True,
        'checked': len(history),
        'first_invalid_id': None,
        'head_hash': head_hash,
    }


def replay_verify_audit_chain(conn) -> dict:
    """Replay the stored history deterministically and return verification evidence."""
    result = verify_audit_chain(conn)
    result['replayed'] = result['checked']
    return result
