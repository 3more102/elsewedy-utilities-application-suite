"""Inventory WHY coverage: stockout line contributors.

``inventory/stockout_lines`` counts stocked lines whose available-to-issue
quantity is exhausted; the WHY view must cite those exact lines with
contributor attribution and resolvable inventory drill targets instead of
returning an empty driver list.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_inventory_procurement_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_stockout_scope(conn, *, other_site_stockout: bool = True):
    """Site A: two stocked-out lines (one below reorder point, one at zero
    stock). Site B (optionally): its own stockout that must never be cited
    for site A. Returns dict of ids."""
    suffix = uuid.uuid4().hex[:8].upper()
    stamp = now()
    site_a = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (f'STO-A-{suffix}', f'Stockout site A {suffix}', 'Greater Cairo',
         'Cairo', 'Electrical Substation', 100),
    )
    site_a_id = int(site_a.lastrowid)
    warehouse = conn.execute(
        '''INSERT INTO warehouses(warehouse_code,name,site_id)
           VALUES(?,?,?)''',
        (f'WH-A-{suffix}', f'Stockout warehouse A {suffix}', site_a_id),
    )
    warehouse_a_id = int(warehouse.lastrowid)

    items = []
    for tag, current_stock, reserved, reorder in (
        ('SHORT', 2.0, 2.0, 10.0),   # available 0, shortfall vs reorder = 8
        ('ZERO', 0.0, 0.0, 5.0),     # available 0, shortfall = 5
    ):
        item = conn.execute(
            '''INSERT INTO inventory_items(
                   item_no,name,category,unit,current_stock,reserved_stock,
                   reorder_point,max_level,unit_price,warehouse_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (f'ITM-STO-{suffix}-{tag}', f'Stockout probe {tag}', 'Spares',
             'ea', current_stock, reserved, reorder, reorder * 3, 25.0,
             warehouse_a_id),
        )
        items.append(int(item.lastrowid))

    site_b_id = None
    if other_site_stockout:
        site_b = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'STO-B-{suffix}', f'Stockout site B {suffix}', 'Delta',
             'Tanta', 'Substation', 80),
        )
        site_b_id = int(site_b.lastrowid)
        warehouse_b = conn.execute(
            '''INSERT INTO warehouses(warehouse_code,name,site_id)
               VALUES(?,?,?)''',
            (f'WH-B-{suffix}', f'Stockout warehouse B {suffix}', site_b_id),
        )
        warehouse_b_id = int(warehouse_b.lastrowid)
        other_item = conn.execute(
            '''INSERT INTO inventory_items(
                   item_no,name,category,unit,current_stock,reserved_stock,
                   reorder_point,max_level,unit_price,warehouse_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (f'ITM-STO-{suffix}-OTH', 'Stockout probe other site', 'Spares',
             'ea', 0.0, 0.0, 7.0, 21.0, 15.0, warehouse_b_id),
        )
        items.append(int(other_item.lastrowid))

    return {
        'site_a': site_a_id,
        'site_b': site_b_id,
        'item_ids_site_a': set(items[:2]),
        'all_item_ids': set(items),
    }


def test_stockout_why_cites_the_counted_lines():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_stockout_scope(conn, other_site_stockout=False)
        with db() as conn:
            canonical = compute_inventory_procurement_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seeded['site_a']))

        assert canonical['stockout_items'] == 2

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'inventory',
                'metric': 'stockout_lines',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 2
        drivers = payload['drivers']
        assert len(drivers) == 2
        assert {d['source_id'] for d in drivers} == seeded['item_ids_site_a']
        for driver in drivers:
            assert driver['attribution'] == 'contributor'
            assert driver['source_type'] == 'inventory_item'
            assert driver['drill']['module'] == 'inventory'
            assert driver['magnitude'] > 0
            assert driver['unit'] == 'units short of reorder point'
        # Deterministic ranking: larger shortfall first.
        assert drivers[0]['magnitude'] >= drivers[1]['magnitude']


def test_stockout_why_respects_site_scope():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_stockout_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'inventory',
                'metric': 'stockout_lines',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        cited = {d['source_id'] for d in payload['drivers']}
        assert cited == seeded['item_ids_site_a']
        assert len(cited) == len(payload['drivers'])
