from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.disaster_recovery import create_backup, restore_backup, verify_backup


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES ('alpha'), ('beta')")
        conn.commit()
    finally:
        conn.close()


def plant_stale_wal_target(target: Path) -> None:
    """Leave target.db plus a live-consistent -wal sidecar, as after a crash.

    EUAS runs SQLite in WAL mode; a crashed host leaves the main file together
    with WAL frames that SQLite will replay on the next open.
    """
    stale = target.with_name(target.name + ".stale-seed")
    conn = sqlite3.connect(stale)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES ('stale')")
        conn.commit()
        wal = Path(str(stale) + "-wal")
        assert wal.exists(), "expected an un-checkpointed WAL sidecar"
        shutil.copy2(stale, target)
        shutil.copy2(wal, str(target) + "-wal")
    finally:
        conn.close()


def test_sqlite_backup_verify_and_restore(tmp_path: Path):
    source = tmp_path / "source.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "note.txt").write_text("EUAS attachment", encoding="utf-8")
    make_db(source)

    backup = create_backup(
        tmp_path / "backups",
        backend="sqlite",
        sqlite_path=source,
        uploads_dir=uploads,
        now_utc=datetime(2026, 8, 22, 18, 30, tzinfo=timezone.utc),
    )

    verified = verify_backup(backup)
    assert verified["valid"] is True
    assert verified["database_backend"] == "sqlite"
    assert verified["artifacts_checked"] == 2

    restored = tmp_path / "restored.db"
    restored_uploads = tmp_path / "restored_uploads"
    result = restore_backup(
        backup,
        sqlite_target=restored,
        uploads_target=restored_uploads,
        restore_uploads=True,
    )
    assert result["restored"] is True
    assert result["uploads_restored"] is True

    conn = sqlite3.connect(restored)
    try:
        rows = conn.execute("SELECT value FROM sample ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("alpha",), ("beta",)]
    assert (restored_uploads / "note.txt").read_text(encoding="utf-8") == "EUAS attachment"


def test_verify_rejects_tampered_artifact(tmp_path: Path):
    source = tmp_path / "source.db"
    make_db(source)
    backup = create_backup(
        tmp_path / "backups",
        backend="sqlite",
        sqlite_path=source,
        uploads_dir=tmp_path / "missing-uploads",
        include_uploads=False,
    )

    database_artifact = backup / "database.sqlite3"
    database_artifact.write_bytes(database_artifact.read_bytes() + b"tamper")

    with pytest.raises(RuntimeError, match="size mismatch|SHA-256 mismatch"):
        verify_backup(backup)


def test_verify_rejects_manifest_path_escape(tmp_path: Path):
    source = tmp_path / "source.db"
    make_db(source)
    backup = create_backup(
        tmp_path / "backups",
        backend="sqlite",
        sqlite_path=source,
        include_uploads=False,
    )
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["path"] = "../outside.db"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="escapes backup directory"):
        verify_backup(backup, deep=False)


def test_restore_refuses_to_overwrite_without_force(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    make_db(source)
    target.write_text("do not overwrite", encoding="utf-8")
    backup = create_backup(
        tmp_path / "backups",
        backend="sqlite",
        sqlite_path=source,
        include_uploads=False,
    )

    with pytest.raises(RuntimeError, match="use --force"):
        restore_backup(backup, sqlite_target=target)

    assert target.read_text(encoding="utf-8") == "do not overwrite"


def test_restore_removes_stale_wal_sidecars(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    make_db(source)
    plant_stale_wal_target(target)
    backup = create_backup(
        tmp_path / "backups",
        backend="sqlite",
        sqlite_path=source,
        include_uploads=False,
    )

    restore_backup(backup, sqlite_target=target, force=True)

    # The stale WAL frames must not be replayed over the restored database.
    assert not Path(str(target) + "-wal").exists()
    conn = sqlite3.connect(target)
    try:
        rows = conn.execute("SELECT value FROM sample ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("alpha",), ("beta",)]
