from __future__ import annotations

from fastapi.testclient import TestClient

from app.authorization import permission_codes_for_role, replace_role_permissions
from app.database import db
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return _bearer(response.json()['token'])


def test_business_capability_revocation_is_immediate_for_existing_session():
    """Revoking a business capability must affect an already-issued bearer."""
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'admin')
        assert 'assets.health.recalculate' in original

        baseline = client.post('/api/assets/health/recalculate', headers=headers)
        assert baseline.status_code == 200, baseline.text

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'admin',
                    [code for code in original if code != 'assets.health.recalculate'],
                )

            denied = client.post('/api/assets/health/recalculate', headers=headers)
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'admin', original)

        restored = client.post('/api/assets/health/recalculate', headers=headers)
        assert restored.status_code == 200, restored.text


def test_business_capability_cannot_expand_historical_role_ceiling():
    """A forbidden legacy role stays forbidden even if granted the capability."""
    with TestClient(app) as client:
        executive = _login(client, 'exec', 'Viewer@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'executive')

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'executive',
                    sorted(set(original) | {'assets.health.recalculate'}),
                )

            denied = client.post('/api/assets/health/recalculate', headers=executive)
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'executive', original)


def test_procurement_approval_revocation_is_immediate_before_resource_lookup():
    """The same bearer sees a revoked approval grant before endpoint lookup."""
    missing_pr = 2_147_483_647
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'admin')
        assert 'procurement.requisition.approve' in original

        baseline = client.post(
            f'/api/procurement/requisitions/{missing_pr}/approve', headers=admin
        )
        assert baseline.status_code == 404, baseline.text

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'admin',
                    [
                        code
                        for code in original
                        if code != 'procurement.requisition.approve'
                    ],
                )

            denied = client.post(
                f'/api/procurement/requisitions/{missing_pr}/approve', headers=admin
            )
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'admin', original)

        restored = client.post(
            f'/api/procurement/requisitions/{missing_pr}/approve', headers=admin
        )
        assert restored.status_code == 404, restored.text


def test_procurement_approval_capability_cannot_promote_executive_role():
    missing_pr = 2_147_483_647
    with TestClient(app) as client:
        executive = _login(client, 'exec', 'Viewer@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'executive')

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'executive',
                    sorted(set(original) | {'procurement.requisition.approve'}),
                )

            denied = client.post(
                f'/api/procurement/requisitions/{missing_pr}/approve', headers=executive
            )
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'executive', original)


def test_project_task_revocation_is_immediate_before_project_lookup():
    """Project task capability changes affect an already-issued bearer."""
    missing_project = 2_147_483_647
    payload = {'task_name': 'Authorization contract probe'}
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'admin')
        assert 'projects.tasks.create' in original

        baseline = client.post(
            f'/api/projects/{missing_project}/tasks', headers=admin, json=payload
        )
        assert baseline.status_code == 404, baseline.text

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'admin',
                    [code for code in original if code != 'projects.tasks.create'],
                )

            denied = client.post(
                f'/api/projects/{missing_project}/tasks', headers=admin, json=payload
            )
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'admin', original)

        restored = client.post(
            f'/api/projects/{missing_project}/tasks', headers=admin, json=payload
        )
        assert restored.status_code == 404, restored.text


def test_project_task_capability_cannot_promote_executive_role():
    missing_project = 2_147_483_647
    payload = {'task_name': 'Authorization contract probe'}
    with TestClient(app) as client:
        executive = _login(client, 'exec', 'Viewer@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'executive')

        try:
            with db() as conn:
                replace_role_permissions(
                    conn,
                    'executive',
                    sorted(set(original) | {'projects.tasks.create'}),
                )

            denied = client.post(
                f'/api/projects/{missing_project}/tasks', headers=executive, json=payload
            )
            assert denied.status_code == 403, denied.text
        finally:
            with db() as conn:
                replace_role_permissions(conn, 'executive', original)


def test_permission_management_rejects_unknown_business_capability():
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        with db() as conn:
            original = permission_codes_for_role(conn, 'executive')

        response = client.put(
            '/api/admin/roles/executive/permissions',
            headers=admin,
            json={'permissions': sorted(set(original) | {'assets.unknown.escalation'})},
        )
        assert response.status_code == 400, response.text

        with db() as conn:
            assert permission_codes_for_role(conn, 'executive') == original
