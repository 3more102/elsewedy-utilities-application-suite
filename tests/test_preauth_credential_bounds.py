from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _admin_headers() -> dict[str, str]:
    with TestClient(app) as client:
        response = client.post(
            '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
        )
        assert response.status_code == 200, response.text
        return _bearer(response.json()['token'])


def test_login_rejects_oversized_preauth_credentials_before_hashing():
    with TestClient(app) as client:
        huge_password = 'A@1' + 'a' * 2000
        response = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': huge_password},
        )
        assert response.status_code == 422, response.text

        long_username = 'u' * 151
        response = client.post(
            '/api/auth/login',
            json={'username': long_username, 'password': 'Whatever@123'},
        )
        assert response.status_code == 422, response.text

        response = client.post(
            '/api/auth/login',
            json={'username': '', 'password': 'Whatever@123'},
        )
        assert response.status_code == 422, response.text

        response = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': ''},
        )
        assert response.status_code == 422, response.text


def test_login_still_accepts_boundary_length_credentials():
    with TestClient(app) as client:
        ok = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': 'EUAS@2026'},
        )
        assert ok.status_code == 200, ok.text

        # A 150-character username for a missing account must still fail with
        # the normal authentication error, not a validation rejection.
        boundary = client.post(
            '/api/auth/login',
            json={'username': 'x' * 150, 'password': 'Whatever@123'},
        )
        assert boundary.status_code == 401, boundary.text

        # 1024 characters is the accepted password ceiling.
        ceiling = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': 'Y' * 1024},
        )
        assert ceiling.status_code in (200, 401), ceiling.text
        assert ceiling.status_code != 429


def test_admin_user_creation_rejects_policy_violating_passwords():
    headers = _admin_headers()
    with TestClient(app) as client:
        too_long = client.post(
            '/api/admin/users',
            headers=headers,
            json={
                'username': 'bounds-user-longpw',
                'password': 'X1!' + 'a' * 200,
                'full_name': 'Bounds User',
                'role_code': 'technician',
            },
        )
        assert too_long.status_code == 422, too_long.text

        too_short = client.post(
            '/api/admin/users',
            headers=headers,
            json={
                'username': 'bounds-user-shortpw',
                'password': 'X1!aaaa',
                'full_name': 'Bounds User',
                'role_code': 'technician',
            },
        )
        assert too_short.status_code == 422, too_short.text

        with db() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username LIKE 'bounds-user-%'"
            ).fetchone()[0]
        assert remaining == 0
