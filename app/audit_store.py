from __future__ import annotations

import json

from .database import audit_digest, now


AUDIT_LOCK_ID = 1


def ensure_audit_chain_lock(conn) -> None:
    """Create the singleton row used to serialize audit-chain appends."""
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS audit_chain_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO audit_chain_lock(id,guard) VALUES(?,0)',
        (AUDIT_LOCK_ID,),
    )


def append_audit(
    conn,
    user_id: int,
    action: str,
    module: str,
    record_id: str,
    old='',
    new='',
):
    """Append one audit record while holding the global chain-head row lock.

    PostgreSQL row-locks the singleton through UPDATE; SQLite serializes the
    write transaction. The lock is held until the caller's surrounding DB
    transaction commits or rolls back, so no concurrent writer can derive a
    second child from the same audit-chain head.
    """
    locked = conn.execute(
        'UPDATE audit_chain_lock SET guard=guard WHERE id=?',
        (AUDIT_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise RuntimeError('audit chain lock is not initialized')

    if not isinstance(old, str):
        old = json.dumps(old, ensure_ascii=False, default=str, sort_keys=True)
    if not isinstance(new, str):
        new = json.dumps(new, ensure_ascii=False, default=str, sort_keys=True)

    created = now()
    previous = conn.execute(
        'SELECT audit_hash FROM audit_logs ORDER BY id DESC LIMIT 1'
    ).fetchone()
    previous_hash = (
        previous['audit_hash'] if previous and previous['audit_hash'] else ''
    )
    digest = audit_digest(
        previous_hash,
        user_id,
        action,
        module,
        record_id,
        old,
        new,
        created,
    )
    conn.execute(
        '''INSERT INTO audit_logs(
             user_id,action,module,record_id,old_value,new_value,created_at,
             prev_hash,audit_hash
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            user_id,
            action,
            module,
            record_id,
            old,
            new,
            created,
            previous_hash,
            digest,
        ),
    )
    return digest
