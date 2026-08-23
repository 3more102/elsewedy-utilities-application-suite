from __future__ import annotations

import json
from typing import Any

from app.database import audit_digest, now


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def write_audit(
    conn,
    user_id: int,
    action: str,
    module: str,
    record_id: str,
    old: Any = '',
    new: Any = '',
) -> str:
    """Append one tamper-evident audit record to the existing EUAS hash chain."""
    old_value = _serialize(old)
    new_value = _serialize(new)
    created_at = now()
    previous = conn.execute(
        "SELECT audit_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = previous['audit_hash'] if previous and previous['audit_hash'] else ''
    digest = audit_digest(
        prev_hash,
        user_id,
        action,
        module,
        record_id,
        old_value,
        new_value,
        created_at,
    )
    conn.execute(
        """INSERT INTO audit_logs(
               user_id,action,module,record_id,old_value,new_value,created_at,prev_hash,audit_hash
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            action,
            module,
            record_id,
            old_value,
            new_value,
            created_at,
            prev_hash,
            digest,
        ),
    )
    return digest


# Compatibility name used throughout the existing monolithic application.
audit = write_audit
