from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def _make_euas_db(path: Path, *, tamper: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            '''
            CREATE TABLE audit_logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL, action TEXT NOT NULL, module TEXT NOT NULL,
              record_id TEXT NOT NULL, old_value TEXT DEFAULT '', new_value TEXT DEFAULT '',
              created_at TEXT NOT NULL, prev_hash TEXT DEFAULT '', audit_hash TEXT DEFAULT ''
            );
            '''
        )
        from app.database import audit_digest

        prev = ''
        for index in range(3):
            created = f'2026-08-2{index}T10:00:00'
            digest = audit_digest(prev, 1, 'CREATE', 'DR', str(index), '', '', created)
            conn.execute(
                '''INSERT INTO audit_logs(
                     user_id,action,module,record_id,old_value,new_value,
                     created_at,prev_hash,audit_hash
                   ) VALUES(1,'CREATE','DR',?,'','',?,?,?)''',
                (str(index), created, prev, digest),
            )
            prev = digest
        if tamper:
            conn.execute("UPDATE audit_logs SET new_value='tampered' WHERE id=2")
        conn.commit()
    finally:
        conn.close()


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_backup_manifest_records_valid_audit_chain(tmp_path: Path):
    db = tmp_path / 'euas.db'
    _make_euas_db(db)
    out = tmp_path / 'bundle.zip'

    result = _run('backup_sqlite.py', '--db', str(db), '--uploads', str(tmp_path / 'no-uploads'), '--output', str(out))
    assert result.returncode == 0, result.stderr
    assert out.exists()

    with __import__('zipfile').ZipFile(out) as z:
        manifest = json.loads(z.read('backup_manifest.json'))
    assert manifest['audit_chain']['valid'] is True
    assert manifest['audit_chain']['records_checked'] == 3
    assert len(manifest['audit_chain']['head_hash']) == 64


def test_backup_manifest_records_tampered_chain_as_invalid(tmp_path: Path):
    db = tmp_path / 'euas.db'
    _make_euas_db(db, tamper=True)
    out = tmp_path / 'bundle.zip'

    result = _run('backup_sqlite.py', '--db', str(db), '--output', str(out))
    assert result.returncode == 0, result.stderr
    assert 'audit chain INVALID' in result.stderr

    with __import__('zipfile').ZipFile(out) as z:
        manifest = json.loads(z.read('backup_manifest.json'))
    assert manifest['audit_chain']['valid'] is False
    assert manifest['audit_chain']['first_invalid_id'] == 2


def test_restore_refuses_tampered_audit_chain(tmp_path: Path):
    db = tmp_path / 'euas.db'
    _make_euas_db(db, tamper=True)
    bundle = tmp_path / 'tampered-bundle.zip'
    result = _run('backup_sqlite.py', '--db', str(db), '--output', str(bundle))
    assert result.returncode == 0, result.stderr

    target = tmp_path / 'live.db'
    target.write_text('keep-me', encoding='utf-8')
    restored = _run('restore_sqlite.py', str(bundle), '--db', str(target), '--force')
    assert restored.returncode != 0
    assert 'Restore refused' in restored.stderr + restored.stdout
    # The live database must be untouched after the refusal.
    assert target.read_text(encoding='utf-8') == 'keep-me'


def test_restore_accepts_valid_audit_chain(tmp_path: Path):
    db = tmp_path / 'euas.db'
    _make_euas_db(db)
    bundle = tmp_path / 'good-bundle.zip'
    result = _run('backup_sqlite.py', '--db', str(db), '--output', str(bundle))
    assert result.returncode == 0, result.stderr + result.stdout

    target = tmp_path / 'live.db'
    restored = _run('restore_sqlite.py', str(bundle), '--db', str(target), '--force')
    assert restored.returncode == 0, restored.stderr + restored.stdout
    conn = sqlite3.connect(target)
    try:
        count = conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0]
    finally:
        conn.close()
    assert count == 3


def test_restore_cli_removes_stale_wal_sidecars(tmp_path: Path):
    import shutil

    good = tmp_path / 'good.db'
    _make_euas_db(good)
    bundle = tmp_path / 'bundle.zip'
    result = _run('backup_sqlite.py', '--db', str(good), '--output', str(bundle))
    assert result.returncode == 0, result.stderr + result.stdout

    # Simulate a crashed host that leaves a WAL sidecar consistent with the
    # pre-restore database; SQLite replays those frames on the next open.
    stale = tmp_path / 'stale-seed.db'
    target = tmp_path / 'live.db'
    conn = sqlite3.connect(stale)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(
            '''CREATE TABLE audit_logs(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 user_id INTEGER NOT NULL, action TEXT NOT NULL, module TEXT NOT NULL,
                 record_id TEXT NOT NULL, old_value TEXT DEFAULT '', new_value TEXT DEFAULT '',
                 created_at TEXT NOT NULL, prev_hash TEXT DEFAULT '', audit_hash TEXT DEFAULT ''
               )'''
        )
        from app.database import audit_digest

        digest = audit_digest('', 1, 'CREATE', 'StaleWAL', 'stale', '', '', '2026-08-01T00:00:00')
        conn.execute(
            '''INSERT INTO audit_logs(
                 user_id,action,module,record_id,old_value,new_value,
                 created_at,prev_hash,audit_hash
               ) VALUES(1,'CREATE','StaleWAL','stale','','',?,?,?)''',
            ('2026-08-01T00:00:00', '', digest),
        )
        conn.commit()
        wal = tmp_path / 'stale-seed.db-wal'
        assert wal.exists(), 'expected an un-checkpointed WAL sidecar'
        # Copy db+wal while the writer is still open so the sidecar survives.
        shutil.copy2(stale, target)
        shutil.copy2(wal, str(target) + '-wal')
    finally:
        conn.close()

    restored = _run('restore_sqlite.py', str(bundle), '--db', str(target), '--force')
    assert restored.returncode == 0, restored.stderr + restored.stdout

    assert not Path(str(target) + '-wal').exists()
    conn = sqlite3.connect(target)
    try:
        rows = conn.execute("SELECT module FROM audit_logs ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [('DR',), ('DR',), ('DR',)]
