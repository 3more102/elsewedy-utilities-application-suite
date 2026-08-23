from __future__ import annotations

import sqlite3

import pytest

from app import database as database_module
from app.migrations import MigrationError, migration_status, run_pending_migrations


def _cheap_hash(password: str) -> str:
    return f'test${password}'


def _point_database_at(monkeypatch, path) -> None:
    monkeypatch.setattr(database_module, 'DB_BACKEND', 'sqlite')
    monkeypatch.setattr(database_module, 'DB_PATH', path)
    monkeypatch.setattr(database_module, 'DATABASE_URL', '')


def _bootstrap_v9(tmp_path, monkeypatch, name: str):
    path = tmp_path / name
    _point_database_at(monkeypatch, path)
    previous = database_module.SCHEMA_VERSION
    database_module.SCHEMA_VERSION = 9
    try:
        database_module.init_db(_cheap_hash)
    finally:
        database_module.SCHEMA_VERSION = previous
    return path


def test_migration_status_reports_pending_v10_from_v9(tmp_path, monkeypatch):
    _bootstrap_v9(tmp_path, monkeypatch, 'pending.db')

    with database_module.db() as conn:
        status = migration_status(conn, backend='sqlite', target_version=10)

    assert status['current_version'] == 9
    assert status['pending_versions'] == [10]
    assert status['invalid_versions'] == []
    assert status['future_versions'] == []
    assert status['ready'] is False


def test_runner_repairs_preclaimed_v10_and_then_is_idempotent(tmp_path, monkeypatch):
    path = _bootstrap_v9(tmp_path, monkeypatch, 'repair.db')
    with sqlite3.connect(path) as raw:
        raw.execute(
            'INSERT INTO schema_migrations(version,applied_at) VALUES(10,?)',
            ('2026-08-23T00:00:00',),
        )

    with database_module.db() as conn:
        before = migration_status(conn, backend='sqlite', target_version=10)
        assert before['invalid_versions'] == [10]
        result = run_pending_migrations(conn, backend='sqlite', target_version=10)

    assert result['applied'] == []
    assert result['repaired'] == [10]
    assert result['status']['ready'] is True

    with sqlite3.connect(path) as raw:
        tables = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {'auth_sessions', 'auth_login_throttle'} <= tables

    with database_module.db() as conn:
        second = run_pending_migrations(conn, backend='sqlite', target_version=10)
    assert second['applied'] == []
    assert second['repaired'] == []
    assert second['skipped'] == [10]


def test_runner_refuses_database_newer_than_application(tmp_path, monkeypatch):
    path = _bootstrap_v9(tmp_path, monkeypatch, 'future.db')
    with sqlite3.connect(path) as raw:
        raw.execute(
            'INSERT INTO schema_migrations(version,applied_at) VALUES(999,?)',
            ('2026-08-23T00:00:00',),
        )

    with pytest.raises(MigrationError, match='newer than this application'):
        with database_module.db() as conn:
            run_pending_migrations(conn, backend='sqlite', target_version=10)
