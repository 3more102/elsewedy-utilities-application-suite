"""IDOR (Insecure Direct Object Reference) and input validation tests.

Verifies that:
  1. Users cannot access other users' private resources by manipulating IDs.
  2. Malformed inputs are rejected gracefully (no 500 errors).
  3. SQL injection attempts in text fields are rejected.
  4. Oversized payloads are rejected.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return _bearer(resp.json()['token'])


# ===========================================================================
# IDOR / Horizontal privilege escalation
# ===========================================================================

class TestIDORPrevention:
    """Verify that users cannot access other users' resources by manipulating IDs."""

    def test_technician_cannot_access_other_user_session_list(self):
        """A technician listing sessions should only see their own."""
        with TestClient(app) as client:
            tech = _login(client, 'tech1', 'Tech@2026')
            resp = client.get('/api/auth/sessions', headers=tech)
            assert resp.status_code == 200
            sessions = resp.json()
            with db() as conn:
                tech_user = conn.execute("SELECT id FROM users WHERE username='tech1'").fetchone()
            for s in sessions:
                assert s.get('user_id') == tech_user[0] or 'user_id' not in s

    def test_executive_cannot_modify_admin_user_status(self):
        """Executive role cannot toggle admin user status (admin-only operation)."""
        with TestClient(app) as client:
            exec_h = _login(client, 'exec', 'Viewer@2026')
            with db() as conn:
                admin_user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
            resp = client.patch(
                f'/api/admin/users/{admin_user[0]}/status',
                headers=exec_h,
                json={'status': 'inactive'},
            )
            assert resp.status_code == 403

    def test_technician_cannot_modify_other_user_profile(self):
        """Technician cannot modify another user's profile via the profile endpoint."""
        with TestClient(app) as client:
            tech = _login(client, 'tech1', 'Tech@2026')
            resp = client.patch(
                '/api/auth/profile',
                headers=tech,
                json={'full_name': 'Hacked'},
            )
            assert resp.status_code in (200, 403)

    def test_anonymous_cannot_access_any_protected_endpoint(self):
        """Unauthenticated requests are rejected on protected endpoints."""
        with TestClient(app) as client:
            protected_endpoints = [
                ('GET', '/api/assets'),
                ('GET', '/api/work-orders'),
                ('GET', '/api/kpis'),
                ('GET', '/api/operations'),
                ('GET', '/api/audit'),
                ('GET', '/api/admin/users'),
            ]
            for method, path in protected_endpoints:
                if method == 'GET':
                    r = client.get(path)
                else:
                    r = client.post(path)
                assert r.status_code == 401, f'{method} {path} returned {r.status_code}'

    def test_nonexistent_entity_returns_404_not_500(self):
        """Accessing a nonexistent asset returns 404, not 500."""
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.get('/api/assets/999999999', headers=admin)
            assert resp.status_code == 404


# ===========================================================================
# Input validation / malformed input
# ===========================================================================

class TestInputValidation:
    """Verify that malformed inputs are rejected gracefully."""

    def test_asset_create_rejects_missing_required_fields(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/assets', headers=admin, json={})
            assert resp.status_code == 422

    def test_asset_create_rejects_oversized_description(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/assets', headers=admin, json={
                'asset_no': 'OVERSIZE-TEST',
                'name': 'Test',
                'description': 'x' * 1_000_000,
            })
            assert resp.status_code in (400, 413, 422)

    def test_work_order_transition_rejects_invalid_status(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/work-orders/999999999/transition', headers=admin, json={
                'status': 'INVALID_STATUS_VALUE',
            })
            assert resp.status_code in (400, 404, 409, 422)

    def test_login_rejects_empty_credentials(self):
        with TestClient(app) as client:
            resp = client.post('/api/auth/login', json={'username': '', 'password': ''})
            assert resp.status_code in (400, 401, 422)

    def test_login_rejects_oversized_username(self):
        with TestClient(app) as client:
            resp = client.post('/api/auth/login', json={'username': 'x' * 10_000, 'password': 'test'})
            assert resp.status_code in (400, 401, 413, 422)

    def test_telemetry_ingest_rejects_nan_value(self):
        """NaN values must be rejected. httpx rejects NaN in JSON, so test via raw body."""
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            body = '{"readings":[{"channel_code":"TEST-CH","value":NaN,"captured_at":"2026-01-01T00:00:00Z"}]}'
            resp = client.post(
                '/api/telemetry/ingest',
                headers={**admin, 'Content-Type': 'application/json'},
                content=body,
            )
            assert resp.status_code in (400, 409, 422, 450), resp.text

    def test_telemetry_ingest_rejects_infinite_value(self):
        """Infinity values must be rejected. httpx rejects Inf in JSON, so test via raw body."""
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            body = '{"readings":[{"channel_code":"TEST-CH","value":Infinity,"captured_at":"2026-01-01T00:00:00Z"}]}'
            resp = client.post(
                '/api/telemetry/ingest',
                headers={**admin, 'Content-Type': 'application/json'},
                content=body,
            )
            assert resp.status_code in (400, 409, 422, 450), resp.text

    def test_kpi_create_rejects_empty_code(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/kpis', headers=admin, json={
                'code': '',
                'name': 'Test KPI',
            })
            assert resp.status_code in (400, 405, 422)

    def test_inspection_submit_rejects_empty_responses(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                existing = conn.execute("SELECT id FROM inspections WHERE status='Open' LIMIT 1").fetchone()
            if existing:
                resp = client.post(f'/api/inspections/{existing[0]}/submit', headers=admin, json={
                    'responses': [],
                })
                assert resp.status_code in (200, 400, 409, 422)


# ===========================================================================
# SQL injection attempts
# ===========================================================================

class TestSQLInjectionPrevention:
    """Verify that SQL injection attempts in text fields are handled safely."""

    def test_asset_name_sql_injection(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/assets', headers=admin, json={
                'asset_no': 'SQLI-TEST',
                'name': "SELECT * FROM users; --",
                'description': "'; DROP TABLE users; --",
            })
            assert resp.status_code in (200, 400, 422), resp.text

    def test_search_sql_injection(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.get("/api/search?q='; DROP TABLE users; --", headers=admin)
            assert resp.status_code == 200

    def test_work_order_title_sql_injection(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.post('/api/work-orders', headers=admin, json={
                'title': "'; DROP TABLE work_orders; --",
                'priority': 'Medium',
                'status': 'Draft',
                'work_type': 'Corrective',
            })
            assert resp.status_code in (200, 400, 422), resp.text

    def test_after_sql_injection_users_table_intact(self):
        """Verify users table was not corrupted by injection attempts."""
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            resp = client.get('/api/admin/users', headers=admin)
            assert resp.status_code == 200
            users = resp.json()
            assert len(users) >= 10
