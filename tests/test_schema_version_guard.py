from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.database as database_module
from app.config import SCHEMA_VERSION
from scripts.disaster_recovery import create_backup, restore_backup


def _cheap_hash(password: str) -> str:
    return f'test${password}'


def _point_database_at(monkeypatch, path) -> None:
    monkeypatch.setattr(database_module, 'DB_BACKEND', 'sqlite')
    monkeypatch.setattr(database_module, 'DB_PATH', path)
    monkeypatch.setattr(database_module, 'DATABASE_URL', '')


def test_init_db_refuses_database_from_newer_release(tmp_path, monkeypatch):
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (SCHEMA_VERSION + 1, "2026-08-24T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    _point_database_at(monkeypatch, db_path)

    with pytest.raises(RuntimeError, match="newer than application schema version"):
        database_module.init_db(_cheap_hash)


def test_init_db_accepts_current_and_older_databases(tmp_path, monkeypatch):
    current = tmp_path / "current.db"
    _point_database_at(monkeypatch, current)
    database_module.init_db(_cheap_hash)

    conn = sqlite3.connect(current)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    assert int(version) == SCHEMA_VERSION


def _backup_with_schema_version(tmp_path: Path, schema_version: int) -> Path:
    source = tmp_path / f"source-{schema_version}.db"
    uploads = tmp_path / f"uploads-{schema_version}"
    uploads.mkdir()
    make_sample(source)
    backup = create_backup(
        tmp_path / f"backups-{schema_version}",
        backend="sqlite",
        sqlite_path=source,
        uploads_dir=uploads,
        include_uploads=False,
        now_utc=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return backup


def make_sample(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES ('alpha')")
        conn.commit()
    finally:
        conn.close()


def test_restore_refuses_backup_from_newer_schema(tmp_path: Path):
    backup = _backup_with_schema_version(tmp_path, SCHEMA_VERSION + 1)
    target = tmp_path / "target.db"
    target.write_bytes(b"precious existing data")

    with pytest.raises(RuntimeError, match="newer than application schema version"):
        restore_backup(backup, sqlite_target=target)

    assert target.read_bytes() == b"precious existing data"


def test_restore_allows_backup_from_same_or_older_schema(tmp_path: Path):
    backup = _backup_with_schema_version(tmp_path, SCHEMA_VERSION - 1)
    target = tmp_path / "target.db"

    result = restore_backup(backup, sqlite_target=target)

    assert result["restored"] is True
    assert result["schema_version"] == SCHEMA_VERSION - 1
