from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.auth import LEGACY_PBKDF2_ROUNDS, PBKDF2_ALGORITHM, PBKDF2_ROUNDS
from app.auth_store import login_scope_digest, token_digest
from app.database import db, now
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _legacy_hash(password: str, salt: str = 'legacy-auth-api-salt') -> str:
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode(), salt.encode(), LEGACY_PBKDF2_ROUNDS
    ).hex()
    return f'{salt}${digest}'


def _replace_user(username: str, password_hash: str, active: int = 1) -> int:
    with db() as conn:
        old = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if old:
            conn.execute('DELETE FROM users WHERE id=?', (old['id'],))
        role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
        cur = conn.execute(
            '''INSERT INTO users(
                 username,password_hash,full_name,email,role_id,department,phone,active,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                username,
                password_hash,
                'Authentication Regression User',
                f'{username}@example.test',
                role['id'],
                'QA',
                '',
                active,
                now(),
            ),
        )
        return int(cur.lastrowid)


def test_login_stores_only_digest_and_session_metadata():
    with TestClient(app) as client:
        response = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': 'EUAS@2026'},
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0) Chrome/151.0.0.0 Safari/537.36'},
        )
        assert response.status_code == 200, response.text
        token = response.json()['token']

        with db() as conn:
            assert conn.execute('SELECT COUNT(*) FROM sessions WHERE token=?', (token,)).fetchone()[0] == 0
            stored = conn.execute(
                'SELECT id,token_digest,client_label FROM auth_sessions WHERE token_digest=?',
                (token_digest(token),),
            ).fetchone()
            assert stored is not None
            assert stored['token_digest'] != token
            assert stored['client_label'] == 'Chrome on Windows'

        me = client.get('/api/auth/me', headers=_bearer(token))
        assert me.status_code == 200, me.text
        assert me.json()['username'] == 'omar'

        sessions = client.get('/api/auth/sessions', headers=_bearer(token))
        assert sessions.status_code == 200, sessions.text
        current = next(item for item in sessions.json() if item['current'])
        assert current['session_id'] == stored['id']
        assert 'token' not in current
        assert 'token_digest' not in current


def test_legacy_password_upgrades_and_other_sessions_are_revoked_on_change():
    username = 'authlegacy'
    original = 'Legacy@2026'
    replacement = 'Replacement@2026!'

    with TestClient(app) as client:
        user_id = _replace_user(username, _legacy_hash(original))

        first = client.post('/api/auth/login', json={'username': username, 'password': original})
        second = client.post('/api/auth/login', json={'username': username, 'password': original})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_token = first.json()['token']
        second_token = second.json()['token']

        with db() as conn:
            stored_hash = conn.execute(
                'SELECT password_hash FROM users WHERE id=?', (user_id,)
            ).fetchone()['password_hash']
            assert stored_hash.startswith(f'{PBKDF2_ALGORITHM}${PBKDF2_ROUNDS}$')
            assert '$legacy-auth-api-salt$' not in stored_hash

        changed = client.post(
            '/api/auth/change-password',
            headers=_bearer(first_token),
            json={'current_password': original, 'new_password': replacement},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()['other_sessions_revoked'] is True
        assert changed.json()['revoked'] >= 1

        assert client.get('/api/auth/me', headers=_bearer(first_token)).status_code == 200
        assert client.get('/api/auth/me', headers=_bearer(second_token)).status_code == 401
        assert client.post('/api/auth/login', json={'username': username, 'password': original}).status_code == 401
        assert client.post('/api/auth/login', json={'username': username, 'password': replacement}).status_code == 200


def test_single_revoke_revoke_others_and_revoke_all_reject_replay():
    username = 'authsessions'
    password = 'Sessions@2026!'

    with TestClient(app) as client:
        from app.auth import hash_password

        _replace_user(username, hash_password(password))
        first = client.post('/api/auth/login', json={'username': username, 'password': password}).json()['token']
        second = client.post('/api/auth/login', json={'username': username, 'password': password}).json()['token']
        third = client.post('/api/auth/login', json={'username': username, 'password': password}).json()['token']

        sessions = client.get('/api/auth/sessions', headers=_bearer(first)).json()
        current = next(item for item in sessions if item['current'])
        others = [item for item in sessions if not item['current']]
        assert len(others) == 2

        target = others[0]
        revoked = client.post(
            f"/api/auth/sessions/{target['session_id']}/revoke",
            headers=_bearer(first),
        )
        assert revoked.status_code == 200, revoked.text

        with db() as conn:
            target_token = second if conn.execute(
                'SELECT id FROM auth_sessions WHERE token_digest=?', (token_digest(second),)
            ).fetchone()['id'] == target['session_id'] else third
        assert client.get('/api/auth/me', headers=_bearer(target_token)).status_code == 401

        remaining_other = third if target_token == second else second
        keep = client.post('/api/auth/sessions/revoke-others', headers=_bearer(first))
        assert keep.status_code == 200, keep.text
        assert client.get('/api/auth/me', headers=_bearer(remaining_other)).status_code == 401
        assert client.get('/api/auth/me', headers=_bearer(first)).status_code == 200

        all_revoked = client.post('/api/auth/sessions/revoke-all', headers=_bearer(first))
        assert all_revoked.status_code == 200, all_revoked.text
        assert all_revoked.json()['current_session_revoked'] is True
        assert client.get('/api/auth/me', headers=_bearer(first)).status_code == 401


def test_login_throttle_survives_application_lifespan_restart():
    username = 'does-not-exist-auth-throttle'
    scope = login_scope_digest(username, 'testclient')
    with db() as conn:
        try:
            conn.execute('DELETE FROM auth_login_throttle WHERE scope_digest=?', (scope,))
        except Exception:
            pass

    with TestClient(app) as client:
        for _ in range(5):
            response = client.post(
                '/api/auth/login',
                json={'username': username, 'password': 'Wrong@2026!'},
            )
            assert response.status_code == 401, response.text
        blocked = client.post(
            '/api/auth/login',
            json={'username': username, 'password': 'Wrong@2026!'},
        )
        assert blocked.status_code == 429, blocked.text
        assert int(blocked.headers['Retry-After']) > 0

    # A new application lifespan uses the same database-backed state; an
    # in-memory limiter would incorrectly forget this lockout.
    with TestClient(app) as client:
        blocked_again = client.post(
            '/api/auth/login',
            json={'username': username, 'password': 'Wrong@2026!'},
        )
        assert blocked_again.status_code == 429, blocked_again.text


def test_account_deactivation_revokes_sessions_at_rest():
    username = 'authdisabled'
    password = 'Disable@2026!'

    with TestClient(app) as client:
        from app.auth import hash_password

        target_id = _replace_user(username, hash_password(password))
        target_token = client.post(
            '/api/auth/login', json={'username': username, 'password': password}
        ).json()['token']
        admin_token = client.post(
            '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
        ).json()['token']

        disabled = client.patch(
            f'/api/admin/users/{target_id}/status',
            headers=_bearer(admin_token),
            json={'active': False},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()['sessions_revoked'] >= 1
        assert client.get('/api/auth/me', headers=_bearer(target_token)).status_code == 401

        with db() as conn:
            row = conn.execute(
                'SELECT revoked_at FROM auth_sessions WHERE token_digest=?',
                (token_digest(target_token),),
            ).fetchone()
            assert row is not None and row['revoked_at']
