#!/usr/bin/env python3
"""Verify the EUAS tamper-evident audit hash chain from the configured database."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.database import db, audit_digest


def verify(conn):
    prev=''; checked=0
    for r in conn.execute('SELECT id,user_id,action,module,record_id,old_value,new_value,created_at,prev_hash,audit_hash FROM audit_logs ORDER BY id').fetchall():
        checked += 1
        expected=audit_digest(prev,r['user_id'],r['action'],r['module'],r['record_id'],r['old_value'],r['new_value'],r['created_at'])
        if (r['prev_hash'] or '') != prev or (r['audit_hash'] or '') != expected:
            return False, checked, r['id'], prev
        prev=r['audit_hash']
    return True, checked, None, prev


if __name__=='__main__':
    with db() as conn:
        valid,checked,bad,head=verify(conn)
    print(f"EUAS audit chain: {'VALID' if valid else 'INVALID'}")
    print(f"records_checked={checked}")
    print(f"head_hash={head or '-'}")
    if bad is not None: print(f"first_invalid_id={bad}")
    raise SystemExit(0 if valid else 2)
