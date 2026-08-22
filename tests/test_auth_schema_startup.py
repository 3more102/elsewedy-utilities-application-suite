from __future__ import annotations

import sqlite3

import pytest

from app import auth_store
from app import database as database_module


def _cheap_hash(password: str) -> str:
    # The startup-ordering tests exercise schema transactions, not password KDF
    # strength. Avoid spending production PBKDF2 work on seeded fixture users.
    return f'test${password}'


def _point_database_at(monkeypatch, path):
    monkeypatch.setattr(database_module, 'DB_BACKEND', 'sqlite')
    monkeypatch.setattr(database_module, 'DB_PATH', path)
    monkeypatch.setattr(database_module, 'DATABASE_URL', '')


def _versions(path) -> list[int]:
    with sqlite3.connect(path) as conn:
        return [int(row[0]) for row in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version'
        ).fetchall()]


def test_failed_auth_migration_never_preclaims_v10(tmp_path, monkeypatch):
    path = tmp_path / 'ordering-failure.db'
    _point_database_at(monkeypatch, path)

    def fail_auth_migration(_conn):
        raise RuntimeError('injected auth migration failure')

    monkeypatch.setattr(auth_store, 'ensure_auth_schema', fail_auth_migration)

    with pytest.raises(RuntimeError, match='injected auth migration failure'):
        auth_store.initialize_auth_database(_cheap_hash)

    versions = _versions(path)
    assert auth_store.BASE_SCHEMA_VERSION in versions
    assert auth_store.AUTH_SCHEMA_VERSION not in versions


def test_successful_auth_migration_advances_v9_to_v10(tmp_path, monkeypatch):
    path = tmp_path / 'ordering-success.db'
    _point_database_at(monkeypatch, path)

    result = auth_store.initialize_auth_database(_cheap_hash)
    assert result['legacy_sessions_migrated'] == 0

    versions = _versions(path)
    assert auth_store.BASE_SCHEMA_VERSION in versions
    assert auth_store.AUTH_SCHEMA_VERSION in versions

    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {'auth_sessions', 'auth_login_throttle'} <= tables
