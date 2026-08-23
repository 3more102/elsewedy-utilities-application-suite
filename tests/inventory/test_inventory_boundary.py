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


def test_reserved_stock_cache_matches_active_reservation_ledger_on_fresh_startup():
    import sqlite3
    from pathlib import Path

    test_db = Path(__file__).resolve().parents[1] / 'euas_test.db'
    with TestClient(app):
        pass
    with sqlite3.connect(test_db) as conn:
        mismatches = conn.execute(
            """SELECT i.item_no,i.reserved_stock,COALESCE(SUM(CASE WHEN r.status IN ('Reserved','Partially Issued')
                       THEN r.quantity-r.issued_quantity ELSE 0 END),0) ledger_reserved
               FROM inventory_items i LEFT JOIN inventory_reservations r ON r.inventory_item_id=i.id
               GROUP BY i.id HAVING ABS(i.reserved_stock-ledger_reserved)>0.000001"""
        ).fetchall()
    assert mismatches == []
