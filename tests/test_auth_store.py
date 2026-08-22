from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.auth_store import (
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_SECONDS,
    active_session_count,
    clear_login_failures,
    create_session,
    ensure_auth_schema,
    list_sessions,
    login_scope_digest,
    record_login_failure,
    resolve_session,
    revoke_all_sessions,
    revoke_other_sessions,
    revoke_session,
    throttle_status,
    token_digest,
)


def make_conn(path=':memory:'):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE roles(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL
    );
    CREATE TABLE users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      full_name TEXT NOT NULL,
      email TEXT,
      role_id INTEGER NOT NULL REFERENCES roles(id),
      department TEXT DEFAULT '',
      phone TEXT DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    INSERT INTO roles(code,name) VALUES('admin','Administrator');
    INSERT INTO users(username,password_hash,full_name,email,role_id,department,phone,active,created_at)
      VALUES('omar','unused','Omar','omar@example.test',1,'Engineering','',1,'2026-08-22T12:00:00');
    ''')
    return conn


def test_schema_migrates_legacy_raw_session_without_forcing_logout():
    conn = make_conn()
    raw = 'legacy-bearer-token-that-must-not-remain'
    conn.execute(
        'INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',
        (raw, 1, '2026-08-22T12:00:00', '2026-08-23T12:00:00'),
    )

    result = ensure_auth_schema(conn)

    assert result['legacy_sessions_migrated'] == 1
    assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
    stored = conn.execute('SELECT * FROM auth_sessions').fetchone()
    assert stored['token_digest'] == token_digest(raw)
    assert stored['token_digest'] != raw
    assert raw not in tuple(str(value) for value in stored)

    resolved = resolve_session(conn, raw, at=datetime(2026, 8, 22, 13, 0, 0))
    assert resolved is not None
    assert resolved['username'] == 'omar'
    assert resolved['session_id'] == stored['id']


def test_new_session_persists_only_digest_and_exposes_non_secret_id():
    conn = make_conn()
    ensure_auth_schema(conn)
    at = datetime(2026, 8, 22, 13, 0, 0)

    created = create_session(
        conn,
        1,
        12,
        'Mozilla/5.0 (Windows NT 10.0) Chrome/151.0.0.0 Safari/537.36',
        at=at,
    )

    assert len(created['token']) >= 64
    assert isinstance(created['session_id'], int)
    assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
    stored = conn.execute('SELECT * FROM auth_sessions WHERE id=?', (created['session_id'],)).fetchone()
    assert stored['token_digest'] == token_digest(created['token'])
    assert stored['token_digest'] != created['token']
    assert stored['client_label'] == 'Chrome on Windows'

    visible = list_sessions(conn, 1, at=at)
    assert visible == [{
        'session_id': created['session_id'],
        'created_at': '2026-08-22T13:00:00',
        'last_seen_at': '2026-08-22T13:00:00',
        'expires_at': '2026-08-23T01:00:00',
        'client_label': 'Chrome on Windows',
    }]
    assert 'token' not in visible[0]
    assert 'token_digest' not in visible[0]


def test_session_expiry_touch_and_revocation_lifecycle():
    conn = make_conn()
    ensure_auth_schema(conn)
    start = datetime(2026, 8, 22, 8, 0, 0)
    first = create_session(conn, 1, 2, 'curl/8.0', at=start)
    second = create_session(conn, 1, 4, 'python-httpx/0.28', at=start + timedelta(minutes=1))

    touched = resolve_session(conn, first['token'], at=start + timedelta(minutes=10))
    assert touched is not None
    assert touched['last_seen_at'] == '2026-08-22T08:10:00'
    assert active_session_count(conn, at=start + timedelta(minutes=10)) == 2

    assert revoke_session(conn, 1, second['session_id'], at=start + timedelta(minutes=11)) == 1
    assert resolve_session(conn, second['token'], at=start + timedelta(minutes=12)) is None
    assert active_session_count(conn, at=start + timedelta(minutes=12)) == 1

    third = create_session(conn, 1, 4, 'Mozilla/5.0 Firefox/142.0', at=start + timedelta(minutes=13))
    assert revoke_other_sessions(conn, 1, first['session_id'], at=start + timedelta(minutes=14)) == 1
    assert resolve_session(conn, third['token'], at=start + timedelta(minutes=15)) is None
    assert resolve_session(conn, first['token'], at=start + timedelta(minutes=15)) is not None

    assert revoke_all_sessions(conn, 1, at=start + timedelta(minutes=16)) == 1
    assert resolve_session(conn, first['token'], at=start + timedelta(minutes=17)) is None
    assert active_session_count(conn, at=start + timedelta(minutes=17)) == 0


def test_expired_session_is_rejected_without_deleting_forensics_row():
    conn = make_conn()
    ensure_auth_schema(conn)
    start = datetime(2026, 8, 22, 8, 0, 0)
    session = create_session(conn, 1, 1, at=start)

    assert resolve_session(conn, session['token'], at=start + timedelta(hours=2)) is None
    assert active_session_count(conn, at=start + timedelta(hours=2)) == 0
    assert conn.execute('SELECT COUNT(*) FROM auth_sessions WHERE id=?', (session['session_id'],)).fetchone()[0] == 1


def test_login_scope_is_normalized_and_does_not_store_account_or_ip():
    first = login_scope_digest(' Omar ', '203.0.113.25')
    second = login_scope_digest('omar', '203.0.113.25')
    assert first == second
    assert len(first) == 64
    assert 'omar' not in first
    assert '203.0.113.25' not in first


def test_throttle_state_persists_and_progressively_blocks(tmp_path):
    db_path = tmp_path / 'auth-throttle.db'
    conn = make_conn(db_path)
    ensure_auth_schema(conn)
    scope = login_scope_digest('omar', '203.0.113.25')
    start = datetime(2026, 8, 22, 12, 0, 0)

    for offset in range(LOGIN_MAX_FAILURES):
        state = record_login_failure(conn, scope, at=start + timedelta(seconds=offset))
    conn.commit()
    assert state['failure_count'] == LOGIN_MAX_FAILURES
    assert state['blocked_until'] is not None
    conn.close()

    reopened = sqlite3.connect(db_path)
    reopened.row_factory = sqlite3.Row
    blocked = throttle_status(reopened, scope, at=start + timedelta(seconds=10))
    assert blocked['blocked'] is True
    assert blocked['retry_after'] > 0

    # Once the first lock expires, another failure within the same rolling
    # window increases the backoff instead of permanently locking the account.
    later = start + timedelta(seconds=40)
    assert throttle_status(reopened, scope, at=later)['blocked'] is False
    next_state = record_login_failure(reopened, scope, at=later)
    assert next_state['failure_count'] == LOGIN_MAX_FAILURES + 1
    next_block = datetime.fromisoformat(next_state['blocked_until'])
    assert (next_block - later).total_seconds() == 60

    clear_login_failures(reopened, scope)
    assert throttle_status(reopened, scope, at=later)['failure_count'] == 0
    reopened.close()


def test_throttle_window_expires_without_sleeping():
    conn = make_conn()
    ensure_auth_schema(conn)
    scope = login_scope_digest('omar', '198.51.100.10')
    start = datetime(2026, 8, 22, 12, 0, 0)
    record_login_failure(conn, scope, at=start)

    state = throttle_status(
        conn,
        scope,
        at=start + timedelta(seconds=LOGIN_WINDOW_SECONDS + 1),
    )
    assert state == {'blocked': False, 'retry_after': 0, 'failure_count': 0}
    assert conn.execute(
        'SELECT COUNT(*) FROM auth_login_throttle WHERE scope_digest=?', (scope,)
    ).fetchone()[0] == 0
