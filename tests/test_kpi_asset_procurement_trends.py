"""Asset and inventory/procurement trend adapter coverage.

Trend samples must equal the canonical family computation for every bucket;
the registry only extracts already-computed values, so these tests pin
value parity, labels, units and directions through ``/api/kpi/trend``.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import (
    ExecutiveFilters,
    compute_asset_kpis,
    compute_inventory_procurement_kpis,
)
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_asset_fleet(conn):
    suffix = uuid.uuid4().hex[:8].upper()
    cur = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (f'ATR-{suffix}', f'Asset trend site {suffix}', 'Greater Cairo',
         'Cairo', 'Electrical Substation', 800))
    site_id = int(cur.lastrowid)
    location_id = int(conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'ATRL-{suffix}', f'Asset trend bay {suffix}', 'Area', site_id),
    ).lastrowid)
    stamp = now()

    fleet = [
        # (criticality, condition, status)
        ('Critical', 'Good', 'Operating'),
        ('High', 'Good', 'Operating'),
        ('Medium', 'Warning', 'Standby'),
        ('Low', 'Poor', 'Maintenance'),
        ('Critical', 'Critical', 'Maintenance'),
    ]
    for index, (criticality, condition, status) in enumerate(fleet):
        conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                  status,location_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'AST-ATR-{suffix}-{index:02d}',
             f'Asset trend probe {index} {suffix}', 'Transformer',
             criticality, condition, status, location_id, stamp, stamp),
        )
    return {'site_id': site_id}


def _trend(client, headers, *, family, metric, **params):
    response = client.get(
        '/api/kpi/trend', headers=headers,
        params={'family': family, 'metric': metric, 'samples': 2,
                'period_days': 30, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_asset_family_trends_match_canonical_computation():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_asset_fleet(conn)

        expected_meta = {
            'unavailable_assets': ('down', 'assets', 'lower_is_better',
                                   'Unavailable Assets'),
            'critical_unavailable_assets': (
                'critical_down', 'assets', 'lower_is_better',
                'Critical Unavailable Assets'),
            'assets_in_attention_condition': (
                'condition_attention', 'assets', 'lower_is_better',
                'Assets In Attention Condition'),
        }
        with db() as conn:
            canonical = compute_asset_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seed['site_id']))

        for metric, (canonical_key, unit, direction, label) in \
                expected_meta.items():
            payload = _trend(client, headers, family='assets', metric=metric,
                             site_id=seed['site_id'])
            assert payload['label'] == label, metric
            assert payload['unit'] == unit, metric
            assert payload['direction'] == direction, metric
            assert payload['samples'], metric
            assert payload['samples'][-1]['value'] == canonical[canonical_key]


def test_inventory_procurement_trends_match_canonical_computation():
    with TestClient(app) as client:
        headers = _auth(client)
        from tests.test_inventory_kpis import _seed_item

        with db() as conn:
            item_id = _seed_item(
                conn, uuid.uuid4().hex[:8],
                current_stock=0, reorder_point=4, unit_price=25.0)

        expected_meta = {
            'stockout_lines': ('stockout_items', 'lines',
                               'lower_is_better', 'Stockout Lines'),
            'work_blocked_by_parts': ('work_blocked_by_parts',
                                      'work orders', 'lower_is_better',
                                      'Work Blocked By Parts'),
            'overdue_purchase_orders': ('overdue_purchase_orders',
                                        'purchase orders',
                                        'lower_is_better',
                                        'Overdue Purchase Orders'),
        }
        with db() as conn:
            canonical = compute_inventory_procurement_kpis(
                conn, ExecutiveFilters(period_days=30))

        assert canonical['stockout_items'] >= 1, 'seeded stockout missing'
        for metric, (canonical_key, unit, direction, label) in \
                expected_meta.items():
            payload = _trend(client, headers, family='inventory',
                             metric=metric)
            assert payload['label'] == label, metric
            assert payload['unit'] == unit, metric
            assert payload['direction'] == direction, metric
            assert payload['samples'][-1]['value'] == canonical[canonical_key]
