from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import auth_store
from app.auth import hash_password
from app.auth_store import (
    ACCOUNT_LOGIN_MAX_FAILURES,
    CLIENT_LOGIN_MAX_FAILURES,
    account_global_scope_digest,
    client_login_scope_digest,
    login_scope_digest,
    record_login_failure,
)
from app.database import db, now
from app.main import app


def _cleanup(username: str) -> None:
    with db() as conn:
        for digest in (
            login_scope_digest(username, 'testclient'),
            client_login_scope_digest('testclient'),
            account_global_scope_digest(username),
        ):
            conn.execute('DELETE FROM auth_login_throttle WHERE scope_digest=?', (digest,))


def _seed_user(username: str, password: str) -> None:
    with db() as conn:
        old = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if old:
            conn.execute('DELETE FROM users WHERE id=?', (old['id'],))
        role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
        conn.execute(
            '''INSERT INTO users(
                 username,password_hash,full_name,email,role_id,department,phone,active,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                username,
                hash_password(password),
                'Global Throttle User',
                f'{username}@example.test',
                role['id'],
                'QA',
                '',
                1,
                now(),
            ),
        )


def test_account_global_scope_is_host_independent_and_distinct():
    first = account_global_scope_digest('SprayTarget')
    second = account_global_scope_digest(' spraytarget ')

    assert first == second  # normalized exactly like the per-host pair scope
    assert account_global_scope_digest('user-a') != account_global_scope_digest('user-b')
    assert first != login_scope_digest('spraytarget', 'any-host')


def test_distributed_source_rotation_cannot_extend_single_account_guessing():
    username = 'global-spray-target'
    with TestClient(app) as client:
        _cleanup(username)
        global_scope = account_global_scope_digest(username)

        # Simulate an attacker rotating through many source addresses. Each
        # (account, host) pair stays below its own threshold forever, but the
        # host-independent counter accumulates every guess.
        with db() as conn:
            for index in range(ACCOUNT_LOGIN_MAX_FAILURES - 1):
                record_login_failure(
                    conn,
                    login_scope_digest(username, f'198.51.100.{index}'),
                )
                record_login_failure(
                    conn,
                    client_login_scope_digest(f'198.51.100.{index}'),
                    max_failures=CLIENT_LOGIN_MAX_FAILURES,
                )
                record_login_failure(
                    conn,
                    global_scope,
                    max_failures=ACCOUNT_LOGIN_MAX_FAILURES,
                )

        # This attempt is allowed by all per-host scopes and pushes the
        # account-global counter to its threshold.
        allowed = client.post(
            '/api/auth/login',
            json={'username': username, 'password': 'Guess@2026!'},
        )
        assert allowed.status_code == 401, allowed.text

        blocked = client.post(
            '/api/auth/login',
            json={'username': username, 'password': 'Guess@2026!'},
        )
        assert blocked.status_code == 429, blocked.text
        assert int(blocked.headers['Retry-After']) > 0

        # A different account from the same source address is unaffected.
        other = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': 'wrong-only@1A'},
        )
        assert other.status_code != 429

        _cleanup(username)


def test_global_block_expires_with_its_window_like_other_scopes():
    username = 'global-spray-expiry'
    with TestClient(app) as client:
        _cleanup(username)
        global_scope = account_global_scope_digest(username)

        with db() as conn:
            for _ in range(ACCOUNT_LOGIN_MAX_FAILURES):
                record_login_failure(
                    conn,
                    global_scope,
                    max_failures=ACCOUNT_LOGIN_MAX_FAILURES,
                )
            # Age the throttle window past LOGIN_WINDOW_SECONDS so the next
            # evaluation must reset it instead of staying blocked.
            stale = (
                datetime.now() - timedelta(seconds=auth_store.LOGIN_WINDOW_SECONDS + 5)
            ).isoformat(timespec='seconds')
            conn.execute(
                '''UPDATE auth_login_throttle
                   SET window_started_at=?,blocked_until=NULL WHERE scope_digest=?''',
                (stale, global_scope),
            )

        response = client.post(
            '/api/auth/login',
            json={'username': username, 'password': 'Wrong@2026!'},
        )
        assert response.status_code == 401, response.text

        _cleanup(username)


def test_successful_login_does_not_reset_the_account_global_counter():
    username = 'global-spray-noreset'
    password = 'Legit@2026x!'
    with TestClient(app) as client:
        _cleanup(username)
        _seed_user(username, password)

        with db() as conn:
            for _ in range(ACCOUNT_LOGIN_MAX_FAILURES - 3):
                record_login_failure(
                    conn,
                    account_global_scope_digest(username),
                    max_failures=ACCOUNT_LOGIN_MAX_FAILURES,
                )

        ok = client.post('/api/auth/login', json={'username': username, 'password': password})
        assert ok.status_code == 200, ok.text

        with db() as conn:
            row = conn.execute(
                'SELECT failure_count FROM auth_login_throttle WHERE scope_digest=?',
                (account_global_scope_digest(username),),
            ).fetchone()
        assert row is not None
        assert int(row['failure_count']) == ACCOUNT_LOGIN_MAX_FAILURES - 3

        _cleanup(username)
