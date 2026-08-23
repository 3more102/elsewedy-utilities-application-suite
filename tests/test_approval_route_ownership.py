from __future__ import annotations

from fastapi.testclient import TestClient

from app import application as _application
from app.approval_store import (
    install_approval_routes,
    list_approvals_view,
)
from app.database import db
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    response = client.post(
        '/api/auth/login', json={'username': username, 'password': password}
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_approval_route_ownership_is_registered_exactly_once():
    with TestClient(app):
        assert getattr(app.state, '_euas_approval_routes', False) is True
        # Re-running the installer must remain a no-op.
        before = len(app.routes)
        install_approval_routes()
        assert len(app.routes) == before
        for path, method in (
            ('/api/approvals', 'GET'),
            ('/api/approval-delegations', 'GET'),
            ('/api/approval-delegations', 'POST'),
            ('/api/approval-delegations/{delegation_id}/deactivate', 'PATCH'),
        ):
            matches = [
                route
                for route in app.routes
                if getattr(route, 'path', None) == path
                and method in set(getattr(route, 'methods', set()) or set())
            ]
            assert len(matches) == 1, f'{method} {path} registered {len(matches)} times'
        # Composition must keep the historical handler symbols resolvable.
        for symbol in (
            'list_approvals',
            'list_approval_delegations',
            'create_approval_delegation',
            'deactivate_approval_delegation',
            'decide_approval',
        ):
            assert getattr(_application, symbol, None) is not None


def test_approval_queue_scoping_keeps_historical_role_ceiling():
    with TestClient(app) as client:
        admin = _auth(client)
        tech = _auth(client, 'tech1', 'Tech@2026')

        # A work order submitted for supervisor approval is invisible to an
        # uninvolved technician through the unified queue.
        asset = next(x for x in client.get('/api/assets', headers=admin).json())
        users = {
            u['username']: u['id']
            for u in client.get('/api/reference', headers=admin).json()['users']
        }
        wo = client.post(
            '/api/work-orders',
            headers=admin,
            json={
                'title': 'Queue scoping regression',
                'asset_id': asset['id'],
                'priority': 'Low',
                'supervisor_id': users['supervisor'],
            },
        ).json()
        assert (
            client.post(
                f"/api/work-orders/{wo['id']}/transition",
                headers=admin,
                json={'action': 'submit'},
            ).status_code
            == 200
        )

        admin_view = client.get('/api/approvals?status=Pending', headers=admin).json()
        assert any(
            x['record_type'] == 'work_order' and x['record_id'] == wo['id']
            for x in admin_view
        )
        tech_view = client.get('/api/approvals?status=Pending', headers=tech).json()
        assert not any(
            x['record_type'] == 'work_order' and x['record_id'] == wo['id']
            for x in tech_view
        )

        # The service-level view must agree with the HTTP surface.
        with db() as conn:
            scoped = list_approvals_view(conn, 'Pending', '', {'role': 'technician', 'id': int(users['tech1'])})
        assert not any(x['record_id'] == wo['id'] for x in scoped)
