"""Cost trend/WHY adapter coverage.

``compute_cost_kpis`` sums immutable ``maintenance_cost_ledger`` entries
inside each requested window, so historical buckets are genuine as-of
evaluations — not snapshot echoes. These tests pin that property end to end:
a snapshot echo would repeat the current total into every past bucket.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_cost_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_cost_site(conn, *, amounts_current, amount_previous=None,
                    other_site_amount=None):
    """Seed scoped cost evidence: current-window entries, one previous-window
    entry and (optionally) an out-of-scope site with its own expensive asset.
    Returns (site_id, other_site_id_or_None)."""
    suffix = uuid.uuid4().hex[:8].upper()
    stamp = now()
    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])

    def _site(tag: str) -> int:
        site = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'CST-{tag}-{suffix}', f'Cost KPI site {tag} {suffix}',
             'Greater Cairo', 'Cairo', 'Electrical Substation', 300),
        )
        site_id = int(site.lastrowid)
        location = conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'LCST-{tag}-{suffix}', f'Cost KPI bay {tag} {suffix}',
             'Area', site_id),
        )
        return site_id, int(location.lastrowid)

    def _asset(tag: str, location_id: int) -> int:
        asset = conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                  status,location_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'AST-CST-{tag}-{suffix}', f'Cost KPI asset {tag} {suffix}',
             'Transformer', 'Critical', 'Good', 'Operating', location_id,
             stamp, stamp),
        )
        return int(asset.lastrowid)

    site_a, location_a = _site('A')
    asset_ids = {
        tag: _asset(f'{tag}{index}', location_a)
        for index, tag in enumerate(('HI', 'LO'), start=1)
    }

    posted_current = (
        datetime.now() - timedelta(days=3)).isoformat(timespec='seconds')
    for tag, amount in amounts_current.items():
        conn.execute(
            '''INSERT INTO maintenance_cost_ledger(
                   entry_no,asset_id,cost_type,amount,quantity,reference,
                   posted_by,posted_at)
               VALUES(?,?,?,?,?,?,?,?)''',
            (f'COST-CST-{suffix}-{tag}', asset_ids[tag], 'Repair', float(amount),
             1.0, f'cost probe {tag}', user_id, posted_current),
        )

    if amount_previous is not None:
        posted_previous = (
            datetime.now() - timedelta(days=45)).isoformat(timespec='seconds')
        conn.execute(
            '''INSERT INTO maintenance_cost_ledger(
                   entry_no,asset_id,cost_type,amount,quantity,reference,
                   posted_by,posted_at)
               VALUES(?,?,?,?,?,?,?,?)''',
            (f'COST-CST-{suffix}-PREV', asset_ids['LO'], 'Repair',
             float(amount_previous), 1.0, 'cost probe previous',
             user_id, posted_previous),
        )

    other_site_id = None
    if other_site_amount is not None:
        other_site_id, location_b = _site('B')
        other_asset = _asset('OTH', location_b)
        conn.execute(
            '''INSERT INTO maintenance_cost_ledger(
                   entry_no,asset_id,cost_type,amount,quantity,reference,
                   posted_by,posted_at)
               VALUES(?,?,?,?,?,?,?,?)''',
            (f'COST-CST-{suffix}-OTH', other_asset, 'Repair',
             float(other_site_amount), 1.0, 'cost probe other site',
             user_id, posted_current),
        )
    return site_a, other_site_id


def test_cost_trend_is_window_based_not_snapshot():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id, _ = _seed_cost_site(
                conn, amounts_current={'HI': 1200.0, 'LO': 800.0},
                amount_previous=500.0)
        with db() as conn:
            canonical = compute_cost_kpis(
                conn, ExecutiveFilters(period_days=30, site_id=site_id))

        # Prove the fixture exercises real windowed cost math.
        assert canonical['maintenance_cost_window'] == 2000.0
        assert canonical['maintenance_cost_previous'] == 500.0

        response = client.get(
            '/api/kpi/trend',
            headers=headers,
            params={
                'family': 'cost',
                'metric': 'maintenance_cost_window',
                'site_id': site_id,
                'period_days': 30,
                'samples': 2,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['unit'] == 'currency'
        assert payload['direction'] == 'lower_is_better'
        values = [sample['value'] for sample in payload['samples']]
        # Oldest bucket sees only the 45-day-old entry; a snapshot echo would
        # repeat the current 2000.0 total into that historical bucket.
        assert values[-1] == 2000.0
        assert values[0] == 500.0


def test_cost_why_cites_top_cost_assets_as_contributors():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id, _ = _seed_cost_site(
                conn, amounts_current={'HI': 1200.0, 'LO': 800.0})

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'cost',
                'metric': 'maintenance_cost_window',
                'site_id': site_id,
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 2000.0
        assert payload['previous_value'] == 0.0
        assert payload['delta'] == 2000.0
        # Cost increased against a lower-is-better direction.
        assert payload['improved'] is False

        drivers = payload['drivers']
        assert drivers, 'expected the seeded ledger assets to be cited'
        top = drivers[0]
        assert top['attribution'] == 'contributor'
        assert top['source_type'] == 'asset'
        assert top['magnitude'] == 1200.0
        assert top['drill']['module'] == 'assets'
        with db() as conn:
            asset = conn.execute(
                'SELECT asset_no FROM assets WHERE id=?',
                (top['source_id'],),
            ).fetchone()
        assert asset is not None, 'drill target must resolve to a real asset'
        assert asset['asset_no'] in top['label']
        assert sum(d['magnitude'] for d in drivers) <= 2000.0
        assert 'correlation is not asserted as cause' in payload['disclaimer']


def test_cost_why_never_cites_out_of_scope_assets():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id, other_site_id = _seed_cost_site(
                conn, amounts_current={'HI': 1200.0},
                other_site_amount=99000.0)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'cost',
                'metric': 'maintenance_cost_window',
                'site_id': site_id,
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 1200.0
        cited_ids = {driver['source_id'] for driver in payload['drivers']}
        with db() as conn:
            other_assets = {
                int(row['id'])
                for row in conn.execute(
                    '''SELECT a.id FROM assets a
                       JOIN locations l ON l.id=a.location_id
                       JOIN sites s ON s.id=l.site_id
                       WHERE s.id=?''', (other_site_id,)).fetchall()
            }
        assert cited_ids and cited_ids.isdisjoint(other_assets)


def test_unregistered_cost_metric_stays_rejected():
    with TestClient(app) as client:
        headers = _auth(client)
        for params in (
            {'family': 'cost', 'metric': 'work_blocked_by_parts'},
            {'family': 'cost', 'metric': 'overdue_purchase_orders'},
        ):
            response = client.get(
                '/api/kpi/trend', headers=headers,
                params={'samples': 2, **params})
            assert response.status_code == 404, (params, response.text)
