"""Workforce WHY coverage: unassigned critical work contributors.

The metric counts open critical work orders with no assignee; the WHY view
must cite those exact records (contributor attribution, resolvable drill
targets, scope identical to the count) instead of returning an empty
driver list.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_workforce_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_unassigned_scope(conn):
    """Site A: one overdue + one future unassigned critical WO.
    Site B: its own unassigned critical WO that must never be cited for A."""
    suffix = uuid.uuid4().hex[:8].upper()
    stamp = now()

    def _site(tag: str) -> tuple[int, int]:
        site = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'WFU-{tag}-{suffix}', f'Workforce KPI site {tag} {suffix}',
             'Greater Cairo', 'Cairo', 'Electrical Substation', 250),
        )
        site_id = int(site.lastrowid)
        location = conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'LWFU-{tag}-{suffix}', f'Workforce bay {tag} {suffix}',
             'Area', site_id),
        )
        location_id = int(location.lastrowid)
        asset = conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                  status,location_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'AST-WFU-{tag}-{suffix}', f'Workforce asset {tag} {suffix}',
             'Transformer', 'Critical', 'Good', 'Operating', location_id,
             stamp, stamp),
        )
        return site_id, int(asset.lastrowid)

    def _wo(tag: str, site_id: int, asset_id: int, *, overdue_days: int) -> int:
        target = (date.today() - timedelta(days=overdue_days)).isoformat()
        location_id = int(conn.execute(
            'SELECT location_id FROM assets WHERE id=?', (asset_id,)
        ).fetchone()[0])
        created_wo = conn.execute(
            '''INSERT INTO work_orders(
                   wo_no,title,priority,status,work_type,asset_id,location_id,
                   estimated_hours,target_finish,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'WO-WFU-{suffix}-{tag}', f'Unassigned critical probe {tag}',
             'Critical', 'Approved', 'Corrective', asset_id, location_id,
             4.0, target, stamp, stamp),
        )
        return int(created_wo.lastrowid)

    site_a, asset_a = _site('A')
    wo_overdue = _wo('OD', site_a, asset_a, overdue_days=6)
    wo_future = _wo('FT', site_a, asset_a, overdue_days=-5)
    site_b, asset_b = _site('B')
    _wo('OTH', site_b, asset_b, overdue_days=9)
    return {
        'site_a': site_a,
        'site_b': site_b,
        'wo_ids': [wo_overdue, wo_future],
    }


def test_unassigned_critical_why_cites_the_counted_records():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_unassigned_scope(conn)
        with db() as conn:
            canonical = compute_workforce_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seeded['site_a']))

        assert canonical['unassigned_critical_work'], 'fixture must be live'
        assert len(canonical['unassigned_critical_work']) == 2

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'workforce',
                'metric': 'unassigned_critical_work',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 2

        drivers = payload['drivers']
        assert len(drivers) == 2
        overdue = [d for d in drivers if d['magnitude'] > 0]
        assert overdue and all(
            d['unit'] == 'days past target' for d in overdue)
        not_due = [d for d in drivers if d['magnitude'] == 0]
        assert not_due and all(d['unit'] == 'not yet due'
                               for d in not_due)
        for driver in drivers:
            assert driver['attribution'] == 'contributor'
            assert driver['source_type'] == 'work_order'
            assert driver['drill']['module'] == 'work'
            assert driver['source_id'] in seeded['wo_ids']
        # The overdue record ranks first: larger exposure first is the
        # deterministic ordering of the canonical section.
        assert drivers[0]['magnitude'] >= drivers[1]['magnitude']


def test_unassigned_critical_why_respects_site_scope():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_unassigned_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'workforce',
                'metric': 'unassigned_critical_work',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        cited = {d['source_id'] for d in response.json()['drivers']}
        with db() as conn:
            other_site_wos = {
                int(row['id'])
                for row in conn.execute(
                    '''SELECT w.id FROM work_orders w
                       LEFT JOIN assets a ON a.id=w.asset_id
                       LEFT JOIN locations l ON l.id=a.location_id
                       JOIN sites s ON s.id=l.site_id
                       WHERE s.id=? AND w.priority IN ('Emergency','Critical')
                         AND w.assigned_to IS NULL''',
                    (seeded['site_b'],)).fetchall()
            }
        assert other_site_wos, 'fixture must include out-of-scope candidates'
        assert cited.isdisjoint(other_site_wos)


def test_unassigned_critical_trend_counts_records_instead_of_500():
    """Regression: this metric was registered against a list-valued payload
    key, so /api/kpi/trend failed with TypeError(float(list)). It must return
    the record count like the WHY surface."""
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_unassigned_scope(conn)

        response = client.get(
            '/api/kpi/trend',
            headers=headers,
            params={
                'family': 'workforce',
                'metric': 'unassigned_critical_work',
                'site_id': seeded['site_a'],
                'period_days': 30,
                'samples': 2,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['unit'] == 'work orders'
        assert payload['samples'][-1]['value'] == 2


def test_technicians_available_explanation_removes_no_evidence():
    """The availability metric keeps working through the shared surface even
    though its dispatch/absence composition has no driver section yet; it
    must never inherit unrelated backlog drivers."""
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_unassigned_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'workforce',
                'metric': 'technicians_available',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['drivers'] == []
        assert 'correlation is not asserted as cause' in payload['disclaimer']
