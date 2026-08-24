"""Maintenance trend/WHY adapter coverage for canonical Maximo-style KPIs.

The adapter must only expose values already computed by ``kpi_service``.
No maintenance formula is duplicated in the trend/explanation layer.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_maintenance_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_scope(conn):
    suffix = uuid.uuid4().hex[:8].upper()
    stamp = now()
    site = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (f'MAX-{suffix}', f'Maximo KPI site {suffix}', 'Greater Cairo',
         'Cairo', 'Electrical Substation', 500),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LMAX-{suffix}', f'Maximo KPI bay {suffix}', 'Area', site_id),
    )
    location_id = int(location.lastrowid)
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (f'AST-MAX-{suffix}', f'Maximo KPI asset {suffix}', 'Transformer',
         'Critical', 'Good', 'Operating', location_id, stamp, stamp),
    )
    asset_id = int(asset.lastrowid)

    # One scheduled job completed on time in the current period.
    target = (datetime.now() - timedelta(days=2)).isoformat(timespec='seconds')
    actual = (datetime.now() - timedelta(days=3)).isoformat(timespec='seconds')
    conn.execute(
        '''INSERT INTO work_orders(
               wo_no,title,priority,status,work_type,asset_id,location_id,
               estimated_hours,actual_hours,target_finish,actual_finish,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (f'WO-MAX-DONE-{suffix}', 'Scheduled maintenance probe', 'Medium',
         'Completed', 'Preventive Maintenance', asset_id, location_id, 4.0, 3.0,
         target, actual, actual, stamp),
    )

    # One overdue emergency backlog item supplies a real drill contributor.
    conn.execute(
        '''INSERT INTO work_orders(
               wo_no,title,priority,status,work_type,asset_id,location_id,
               estimated_hours,target_finish,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (f'WO-MAX-OPEN-{suffix}', 'Emergency backlog probe', 'Emergency',
         'Approved', 'Emergency', asset_id, location_id, 8.0,
         (date.today() - timedelta(days=5)).isoformat(), stamp, stamp),
    )

    # Reliability-backed maintenance metrics (MTBF/MTTR) use the same
    # canonical outage data as the executive KPI service.
    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'"
    ).fetchone()[0])
    start = datetime.now() - timedelta(days=1, hours=2)
    end = start + timedelta(hours=2)
    conn.execute(
        '''INSERT INTO asset_outages(
               outage_no,asset_id,site_id,outage_type,status,start_at,end_at,
               reported_by,created_at,updated_at)
           VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (f'OUT-MAX-{suffix}', asset_id, site_id,
         start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'),
         user_id, stamp, stamp),
    )
    return site_id


def test_maintenance_trends_extract_existing_canonical_values_only():
    expected_meta = {
        'emergency_work_orders': ('emergency_wo', 'work orders', 'lower_is_better'),
        'high_risk_overdue_work_orders': (
            'high_risk_overdue_wo', 'work orders', 'lower_is_better'),
        'unassigned_critical_work_orders': (
            'unassigned_critical_wo', 'work orders', 'lower_is_better'),
        'backlog_weeks': ('backlog_weeks', 'weeks', 'lower_is_better'),
        'schedule_compliance_pct': (
            'schedule_compliance_pct', '%', 'higher_is_better'),
        'mtbf_hours': ('mtbf_hours', 'hours', 'higher_is_better'),
        'mttr_hours': ('mttr_hours', 'hours', 'lower_is_better'),
        'repeat_failure_rate_pct': (
            'repeat_failure_rate_pct', '%', 'lower_is_better'),
    }

    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id = _seed_scope(conn)
        with db() as conn:
            canonical = compute_maintenance_kpis(
                conn, ExecutiveFilters(period_days=30, site_id=site_id)
            )

        # Prove the fixture exercises real scoped maintenance and reliability
        # data rather than comparing two accidental zero/empty results.
        assert canonical['emergency_wo'] == 1
        assert canonical['high_risk_overdue_wo'] == 1
        assert canonical['unassigned_critical_wo'] == 1
        assert canonical['schedule_compliance_pct'] == 50.0
        assert canonical['mttr_hours'] == 2.0

        for metric, (canonical_key, unit, direction) in expected_meta.items():
            response = client.get(
                '/api/kpi/trend',
                headers=headers,
                params={
                    'family': 'maintenance',
                    'metric': metric,
                    'site_id': site_id,
                    'period_days': 30,
                    'samples': 2,
                },
            )
            assert response.status_code == 200, (metric, response.text)
            payload = response.json()
            assert payload['unit'] == unit
            assert payload['direction'] == direction
            assert payload['samples'][-1]['value'] == canonical[canonical_key]


def test_maintenance_why_attaches_only_schedule_shortfall_drivers():
    """Schedule compliance explains itself through its own population.

    The seeded open job (target five days ago) is counted in ``scheduled``
    but not in ``met``, so it is exactly the shortfall evidence returned —
    while unrelated backlog/overdue driver kinds stay excluded.
    """
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id = _seed_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'maintenance',
                'metric': 'schedule_compliance_pct',
                'site_id': site_id,
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['unit'] == '%'
        assert payload['direction'] == 'higher_is_better'
        assert payload['drivers'], 'expected shortfall contributor'
        for driver in payload['drivers']:
            assert driver['kind'] == 'schedule_shortfall'
            assert driver['classification'] == 'unfinished'
            assert driver['source_type'] == 'work_order'
        assert not any(d['kind'] == 'overdue_backlog'
                       for d in payload['drivers'])
        assert 'correlation is not asserted as cause' in payload['disclaimer']


def test_new_backlog_metric_keeps_resolvable_overdue_work_driver():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id = _seed_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'maintenance',
                'metric': 'emergency_work_orders',
                'site_id': site_id,
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        drivers = response.json()['drivers']
        assert drivers
        driver = drivers[0]
        assert driver['source_type'] == 'work_order'
        assert driver['attribution'] == 'contributor'
        assert driver['drill']['module'] == 'work'
        with db() as conn:
            assert conn.execute(
                'SELECT 1 FROM work_orders WHERE id=?',
                (driver['source_id'],),
            ).fetchone() is not None
