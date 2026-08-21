"""Create a consistent SQLite + uploads EUAS backup bundle."""
from __future__ import annotations

import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


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
            z.writestr('backup_manifest.json', json.dumps({
                'application': 'Elsewedy Utilities Application Suite',
                'created_at': datetime.now().isoformat(timespec='seconds'),
                'database': str(db_path),
                'integrity_check': integrity,
            }, indent=2))
    print(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
