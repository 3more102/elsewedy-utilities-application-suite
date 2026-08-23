from __future__ import annotations

from app import auth as auth_module
from app import database as database_module
from app.auth_store import create_session, token_digest
from app.migrations import initialize_database


def _cheap_hash(password: str) -> str:
    return f'test${password}'


def _cheap_verify(password: str, stored: str) -> bool:
    return stored == _cheap_hash(password)


def test_existing_demo_credential_rotation_revokes_current_and_legacy_sessions(
    tmp_path, monkeypatch
):
    path = tmp_path / 'production-existing-demo-rotation.db'
    monkeypatch.setattr(database_module, 'DB_BACKEND', 'sqlite')
    monkeypatch.setattr(database_module, 'DB_PATH', path)
    monkeypatch.setattr(database_module, 'DATABASE_URL', '')

    # Materialize a historical/reference database containing the packaged demo
    # credentials, then give the administrator both current and legacy sessions.
    monkeypatch.setenv('EUAS_ENV', 'development')
    monkeypatch.delenv('EUAS_BOOTSTRAP_ADMIN_PASSWORD', raising=False)
    initialize_database(_cheap_hash)

    legacy_token = 'legacy-production-demo-session'
    with database_module.db() as conn:
        admin = conn.execute(
            "SELECT id FROM users WHERE username='omar' AND active=1"
        ).fetchone()
        assert admin is not None
        user_id = int(admin['id'])
        current = create_session(conn, user_id, 12, 'rotation-regression')
        conn.execute(
            'INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',
            (legacy_token, user_id, '2026-08-23T18:00:00', '2099-01-01T00:00:00'),
        )

    # Re-open the same populated database as production. Rotation must change the
    # credential and revoke every server-side bearer representation atomically.
    secret = 'Existing-production-admin-secret-2026!'
    monkeypatch.setenv('EUAS_ENV', 'production')
    monkeypatch.setenv('EUAS_BOOTSTRAP_ADMIN_PASSWORD', secret)
    monkeypatch.setattr(auth_module, 'verify_password', _cheap_verify)

    result = initialize_database(_cheap_hash)
    assert 'omar' in result['credential_hardening']['rotated_users']

    with database_module.db() as conn:
        admin = conn.execute(
            "SELECT id,password_hash FROM users WHERE username='omar'"
        ).fetchone()
        assert admin['password_hash'] == _cheap_hash(secret)

        hardened = conn.execute(
            'SELECT revoked_at FROM auth_sessions WHERE token_digest=?',
            (token_digest(current['token']),),
        ).fetchone()
        assert hardened is not None
        assert hardened['revoked_at']

        assert conn.execute(
            'SELECT COUNT(*) FROM sessions WHERE token=?', (legacy_token,)
        ).fetchone()[0] == 0
