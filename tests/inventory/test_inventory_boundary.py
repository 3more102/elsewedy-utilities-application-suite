from fastapi.testclient import TestClient

from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_inventory_transaction_boundary_preserves_reserved_stock_guard():
    with TestClient(app) as client:
        admin = auth(client)
        item = next(x for x in client.get('/api/inventory', headers=admin).json() if x['current_stock'] > 0)
        available = float(item['current_stock']) - float(item['reserved_stock'])
        blocked = client.post(
            f"/api/inventory/{item['id']}/transaction",
            headers=admin,
            json={'tx_type': 'ISSUE', 'quantity': available + 1},
        )
        assert blocked.status_code == 409
        assert 'Insufficient unreserved stock' in blocked.text
