"""Create a consistent SQLite + uploads EUAS backup bundle."""
from __future__ import annotations

import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit_verification import verify_audit_chain_report


def _audit_chain_evidence(conn):
    """Return tamper-evident chain evidence for an EUAS snapshot, or None."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'audit_logs' not in tables:
        return None
    conn.row_factory = sqlite3.Row
    try:
        report = verify_audit_chain_report(conn)
    finally:
        conn.row_factory = None
    return {
        'valid': report['valid'],
        'records_checked': report['checked'],
        'head_hash': report['head_hash'] or '',
        'first_invalid_id': report['first_invalid_id'],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('EUAS_DB_PATH', str(ROOT / 'euas.db')))
    parser.add_argument('--uploads', default=str(ROOT / 'uploads'))
    parser.add_argument('--output', default='')
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    uploads = Path(args.uploads).resolve()
    if not db_path.exists():
        raise SystemExit(f'Database not found: {db_path}')
    out = Path(args.output).resolve() if args.output else ROOT / f"EUAS_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"

    with tempfile.TemporaryDirectory(prefix='euas-backup-') as td:
        snap = Path(td) / 'euas.db'
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        check = sqlite3.connect(snap)
        try:
            integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
            audit_chain = _audit_chain_evidence(check)
            if audit_chain is not None and not audit_chain['valid']:
                # A broken chain must never be silently preserved as recovery
                # evidence; the bundle is still written for forensics, but the
                # manifest records the corruption explicitly.
                print(
                    f"WARNING: audit chain INVALID at record "
                    f"{audit_chain['first_invalid_id']}; recorded in backup manifest",
                    file=sys.stderr,
                )
        finally:
            check.close()
        if integrity != 'ok':
            raise SystemExit(f'Backup integrity check failed: {integrity}')

        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(snap, 'database/euas.db')
            if uploads.exists():
                for f in uploads.rglob('*'):
                    if f.is_file() and f.name != '.gitkeep':
                        z.write(f, 'uploads/' + str(f.relative_to(uploads)))
            manifest = {
                'application': 'Elsewedy Utilities Application Suite',
                'created_at': datetime.now().isoformat(timespec='seconds'),
                'database': str(db_path),
                'integrity_check': integrity,
            }
            if audit_chain is not None:
                manifest['audit_chain'] = audit_chain
            z.writestr('backup_manifest.json', json.dumps(manifest, indent=2))
    print(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
