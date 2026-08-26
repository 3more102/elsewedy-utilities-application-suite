from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _login_headers() -> dict[str, str]:
    with TestClient(app) as client:
        response = client.post(
            '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
        )
        assert response.status_code == 200, response.text
        return {'Authorization': f"Bearer {response.json()['token']}"}


def test_request_id_is_echoed_for_anonymous_and_authenticated_calls():
    with TestClient(app) as client:
        anonymous = client.get('/api/health', headers={'X-Request-ID': 'trace-abc-123'})
        assert anonymous.status_code == 200, anonymous.text
        assert anonymous.headers['X-Request-ID'] == 'trace-abc-123'

        authenticated = client.get(
            '/api/auth/me',
            headers={**_login_headers(), 'X-Request-ID': 'trace-auth-456'},
        )
        assert authenticated.status_code == 200, authenticated.text
        assert authenticated.headers['X-Request-ID'] == 'trace-auth-456'


def test_hostile_request_ids_are_sanitized_not_reflected():
    with TestClient(app) as client:
        control_chars = client.get(
            '/api/health', headers={'X-Request-ID': 'bad\x01\x02id'}
        )
        echoed = control_chars.headers['X-Request-ID']
        assert all(32 <= ord(ch) < 127 for ch in echoed)
        assert echoed.startswith('bad')

        oversized = 'x' * 500
        long_response = client.get('/api/health', headers={'X-Request-ID': oversized})
        assert len(long_response.headers['X-Request-ID']) <= 128

        quotes = client.get('/api/health', headers={'X-Request-ID': 'a"b\\c<d>e'})
        assert quotes.headers['X-Request-ID'] == 'abcde'


def test_missing_request_id_generates_one():
    with TestClient(app) as client:
        response = client.get('/api/health')
        generated = response.headers['X-Request-ID']
        assert generated
        assert len(generated) == 32  # uuid4().hex
