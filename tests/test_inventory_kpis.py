from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_store import compute_inventory_kpis
from app.main import app


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_item(
    conn,
    suffix: str,
    *,
    current_stock: float,
    reserved_stock: float = 0.0,
    reorder_point: float = 0.0,
    unit_price: float = 10.0,
) -> int:
    site = conn.execute('SELECT id FROM sites ORDER BY id LIMIT 1').fetchone()
    warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
    if not warehouse:
        created_wh = conn.execute(
            '''INSERT INTO warehouses(warehouse_code,name,site_id) VALUES(?,?,?)''',
            (f'WH-KPI-{suffix.upper()}', f'KPI probe store {suffix}', int(site['id'])),
        )
        warehouse_id = int(created_wh.lastrowid)
    else:
        warehouse_id = int(warehouse['id'])
    created = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,category,warehouse_id,current_stock,reserved_stock,
             reorder_point,unit_price
           ) VALUES(?,?,?,?,?,?,?,?)''',
        (
            f'ITM-KPI-{suffix.upper()}',
            f'KPI probe item {suffix}',
            'Spare Parts',
            warehouse_id,
            current_stock,
            reserved_stock,
            reorder_point,
            unit_price,
        ),
    )
    return int(created.lastrowid)


def _seed_open_pr(conn, item_id: int) -> int:
    user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    stamp = now()
    created = conn.execute(
        '''INSERT INTO purchase_requisitions(pr_no,title,requester_id,status,total_estimate,created_at)
           VALUES(?,?,?,?,?,?)''',
        (
            f'PR-KPI-{uuid.uuid4().hex[:8].upper()}',
            'KPI coverage probe',
            int(user['id']),
            'Submitted',
            0,
            stamp,
        ),
    )
    pr_id = int(created.lastrowid)
    conn.execute(
        '''INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost)
           VALUES(?,?,?,?,?)''',
        (pr_id, item_id, 'coverage', 5, 10),
    )
    return pr_id


def test_inventory_kpi_math_is_exact():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            healthy = _seed_item(conn, suffix + '-ok', current_stock=50, reserved_stock=10,
                                 reorder_point=5, unit_price=100)  # available 40
            stockout = _seed_item(conn, suffix + '-out', current_stock=3, reserved_stock=3,
                                  reorder_point=2, unit_price=200)  # available 0
            uncovered = _seed_item(conn, suffix + '-unc', current_stock=1,
                                   reorder_point=4, unit_price=50)  # available 1 <= 4, no PR
            covered = _seed_item(conn, suffix + '-cov', current_stock=2,
                                 reorder_point=6, unit_price=25)  # below point but PR open
            _seed_open_pr(conn, covered)
            # Issue history moves the healthy line out of slow-moving scope.
            user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
            conn.execute(
                '''INSERT INTO inventory_transactions(item_id,tx_type,quantity,user_id,created_at)
                   VALUES(?,?,?,?,?)''',
                (healthy, 'ISSUE', 1, user_id, now()),
            )

        with db() as conn:
            result = compute_inventory_kpis(conn, slow_moving_days=90)

        assert result['stocked_lines'] >= 4
        kpis = result['kpis']
        assert kpis['stockout_lines']['value'] >= 1
        assert kpis['uncovered_reorder_lines']['value'] >= 1
        # Slow-moving: every line except `healthy` has no ISSUE in the window.
        assert kpis['slow_moving_value_pct']['value'] > 0
        assert kpis['open_po_aging_days_avg']['value'] is None

        # The uncovered line appears as a contributor with its exact exposure;
        # seeded catalog lines may outrank it, so match by item.
        top = next(
            c for c in result['contributors']
            if c['item_no'] == f'ITM-KPI-{suffix}-UNC'.upper()
        )
        assert top['on_order'] is False
        # Exposure = (reorder_point - available) x unit_price = 3 x 50 = 150.
        assert top['exposure_value'] == 150.0


def test_open_po_aging_uses_only_pipeline_orders():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            vendor = conn.execute('SELECT id FROM vendors ORDER BY id LIMIT 1').fetchone()
            assert vendor
            stamp = now()
            old_open = conn.execute(
                '''INSERT INTO purchase_orders(po_no,vendor_id,status,order_date,total_cost)
                   VALUES(?,?,?,?,?)''',
                (f'PO-KPI-{uuid.uuid4().hex[:8].upper()}', int(vendor['id']), 'Approved',
                 (datetime.now() - timedelta(days=40)).isoformat(timespec='seconds'), 0),
            )
            received = conn.execute(
                '''INSERT INTO purchase_orders(po_no,vendor_id,status,order_date,total_cost,actual_receipt)
                   VALUES(?,?,?,?,?,?)''',
                (f'PO-KPI-{uuid.uuid4().hex[:8].upper()}', int(vendor['id']), 'Received',
                 (datetime.now() - timedelta(days=300)).isoformat(timespec='seconds'), 0,
                 (datetime.now() - timedelta(days=280)).isoformat(timespec='seconds')),
            )

        with db() as conn:
            result = compute_inventory_kpis(conn)

        assert result['open_purchase_orders'] >= 1
        aging = result['kpis']['open_po_aging_days_avg']['value']
        assert aging is not None and 39 <= aging <= 41


def test_inventory_kpi_api_auth_and_export():
    with TestClient(app) as client:
        anonymous = client.get('/api/kpis/inventory')
        assert anonymous.status_code in (401, 403)

        headers = _auth(client)
        response = client.get(
            '/api/kpis/inventory', headers=headers, params={'slow_moving_days': 30}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['kpi_family'] == 'inventory_procurement'
        assert set(payload['kpis']) == {
            'stock_availability_pct', 'stockout_lines', 'uncovered_reorder_lines',
            'slow_moving_value_pct', 'open_po_aging_days_avg',
        }
        for kpi in payload['kpis'].values():
            assert kpi['definition'] and kpi['formula']

        bounds = client.get(
            '/api/kpis/inventory', headers=headers, params={'slow_moving_days': 6}
        )
        assert bounds.status_code == 422

        export = client.get('/api/kpis/inventory.csv', headers=headers)
        assert export.status_code == 200
        assert 'text/csv' in export.headers.get('content-type', '')
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        assert rows[0][:3] == ['KPI', 'Name', 'Value']
        assert len(rows) == 6
