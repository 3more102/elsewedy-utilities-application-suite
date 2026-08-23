"""Restore an EUAS SQLite backup bundle after integrity validation.

Stop the EUAS application before running this command.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit_verification import verify_audit_chain_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('backup_zip')
    parser.add_argument('--db', default=os.getenv('EUAS_DB_PATH', str(ROOT / 'euas.db')))
    parser.add_argument('--uploads', default=str(ROOT / 'uploads'))
    parser.add_argument('--force', action='store_true', help='Required to replace the current database')
    args = parser.parse_args()
    if not args.force:
        raise SystemExit('Refusing restore without --force. Stop EUAS first, then rerun with --force.')

    bundle = Path(args.backup_zip).resolve()
    db_path = Path(args.db).resolve()
    uploads = Path(args.uploads).resolve()
    if not bundle.exists():
        raise SystemExit(f'Backup not found: {bundle}')

    with tempfile.TemporaryDirectory(prefix='euas-restore-') as td:
        td = Path(td)
        with zipfile.ZipFile(bundle) as z:
            names = set(z.namelist())
            if 'database/euas.db' not in names or 'backup_manifest.json' not in names:
                raise SystemExit('Invalid EUAS backup bundle')
            z.extract('database/euas.db', td)
            for name in names:
                if name.startswith('uploads/') and not name.endswith('/'):
                    z.extract(name, td)
        restored = td / 'database' / 'euas.db'
        conn = sqlite3.connect(restored)
        try:
            integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if 'audit_logs' in tables:
                # A restore replaces the live database wholesale; refusing a
                # tampered snapshot here is the last line of defense before the
                # corrupted evidence chain becomes production state.
                conn.row_factory = sqlite3.Row
                try:
                    report = verify_audit_chain_report(conn)
                finally:
                    conn.row_factory = None
                if not report['valid']:
                    raise SystemExit(
                        f'Restore refused: audit chain invalid at record '
                        f"{report['first_invalid_id']} "
                        '(the bundle may be tampered or corrupted)'
                    )
        finally:
            conn.close()
        if integrity != 'ok':
            raise SystemExit(f'Restore integrity check failed: {integrity}')

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            safety = db_path.with_name(db_path.name + '.pre_restore_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
            shutil.copy2(db_path, safety)
            print(f'Safety copy: {safety}')
        shutil.copy2(restored, db_path)

        extracted_uploads = td / 'uploads'
        if extracted_uploads.exists():
            uploads.mkdir(parents=True, exist_ok=True)
            for f in extracted_uploads.rglob('*'):
                if f.is_file():
                    dest = uploads / f.relative_to(extracted_uploads)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
    print(f'Restored: {db_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
