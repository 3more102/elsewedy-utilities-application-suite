from __future__ import annotations

import sqlite3

import pytest

from app.authorization import (
    PROTECTED_ADMIN_PERMISSION,
    ensure_permission_catalog,
    permission_codes_for_role,
    replace_role_permissions,
)


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        '''
        CREATE TABLE roles(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL
        );
        CREATE TABLE permissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL
        );
        CREATE TABLE role_permissions(
          role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
          permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
          PRIMARY KEY(role_id,permission_id)
        );
        CREATE TABLE users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT UNIQUE NOT NULL,
          role_id INTEGER NOT NULL REFERENCES roles(id)
        );
        '''
    )
    conn.executemany(
        'INSERT INTO roles(code,name) VALUES(?,?)',
        [
            ('admin', 'System Administrator'),
            ('maintenance_manager', 'Maintenance Manager'),
            ('executive', 'Executive Viewer'),
        ],
    )
    return conn


def test_catalog_seeds_once_and_does_not_restore_deliberate_revocation():
    conn = _conn()
    first = ensure_permission_catalog(conn)
    assert first['permissions_created'] >= 8
    assert PROTECTED_ADMIN_PERMISSION in permission_codes_for_role(conn, 'admin')
    assert 'observability.metrics.read' in permission_codes_for_role(conn, 'admin')

    admin = conn.execute("SELECT id FROM roles WHERE code='admin'").fetchone()['id']
    metrics = conn.execute(
        "SELECT id FROM permissions WHERE code='observability.metrics.read'"
    ).fetchone()['id']
    conn.execute(
        'DELETE FROM role_permissions WHERE role_id=? AND permission_id=?',
        (admin, metrics),
    )

    second = ensure_permission_catalog(conn)
    assert second == {'permissions_created': 0, 'grants_created': 0}
    assert 'observability.metrics.read' not in permission_codes_for_role(conn, 'admin')


def test_admin_recovery_permission_is_protected():
    conn = _conn()
    ensure_permission_catalog(conn)
    current = permission_codes_for_role(conn, 'admin')
    unsafe = [code for code in current if code != PROTECTED_ADMIN_PERMISSION]

    with pytest.raises(ValueError, match='protected_admin_permission'):
        replace_role_permissions(conn, 'admin', unsafe)

    assert PROTECTED_ADMIN_PERMISSION in permission_codes_for_role(conn, 'admin')
