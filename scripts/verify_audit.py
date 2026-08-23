#!/usr/bin/env python3
"""Verify the EUAS tamper-evident audit hash chain from the configured database."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.audit_verification import verify_audit_chain_report
from app.database import db


if __name__=='__main__':
    with db() as conn:
        report=verify_audit_chain_report(conn)
    valid,checked,head=report['valid'],report['checked'],report['head_hash']
    bad=report['first_invalid_id']
    print(f"EUAS audit chain: {'VALID' if valid else 'INVALID'}")
    print(f"records_checked={checked}")
    print(f"head_hash={head or '-'}")
    if bad is not None: print(f"first_invalid_id={bad}")
    raise SystemExit(0 if valid else 2)
