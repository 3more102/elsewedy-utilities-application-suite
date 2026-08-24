"""Reliability trend/WHY adapter coverage for canonical utility KPIs.

These tests prove the trend/explanation layer only exposes values already
computed by ``kpi_service.compute_reliability``. No outage formula belongs in
the adapter registry.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_reliability
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
        (f'REL-{suffix}', f'Reliability KPI site {suffix}', 'Greater Cairo',
         'Cairo', 'Electrical Substation', 1000),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LREL-{suffix}', f'Reliability KPI bay {suffix}', 'Area', site_id),
    )
    location_id = int(location.lastrowid)
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (f'AST-REL-{suffix}', f'Reliability KPI asset {suffix}', 'Transformer',
         'Critical', 'Good', 'Operating', location_id, stamp, stamp),
    )
    asset_id = int(asset.lastrowid)
    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'"
    ).fetchone()[0])

    forced_start = datetime.now() - timedelta(days=2, hours=2)
    forced_end = forced_start + timedelta(hours=2)
    conn.execute(
        '''INSERT INTO asset_outages(
               outage_no,asset_id,site_id,outage_type,status,start_at,end_at,
               reported_by,created_at,updated_at)
           VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (f'OUT-F-{suffix}', asset_id, site_id,
         forced_start.isoformat(timespec='seconds'),
         forced_end.isoformat(timespec='seconds'), user_id, stamp, stamp),
    )

    planned_start = datetime.now() - timedelta(days=4, hours=1)
    planned_end = planned_start + timedelta(hours=1)
    conn.execute(
        '''INSERT INTO asset_outages(
               outage_no,asset_id,site_id,outage_type,status,start_at,end_at,
               reported_by,created_at,updated_at)
           VALUES(?,?,?,'Planned','Closed',?,?,?,?,?)''',
        (f'OUT-P-{suffix}', asset_id, site_id,
         planned_start.isoformat(timespec='seconds'),
         planned_end.isoformat(timespec='seconds'), user_id, stamp, stamp),
    )
    return site_id


def test_reliability_trends_extract_existing_canonical_values_only():
    expected_meta = {
        'total_downtime_hours': ('total_downtime_hours', 'hours', 'lower_is_better'),
        'outage_count': ('outage_count', 'outages', 'lower_is_better'),
        'avg_outage_duration_hours': (
            'avg_outage_duration_hours', 'hours', 'lower_is_better'),
        'planned_outages': ('planned_outages', 'outages', 'lower_is_better'),
        'unplanned_outages': ('unplanned_outages', 'outages', 'lower_is_better'),
    }

    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id = _seed_scope(conn)
        with db() as conn:
            canonical = compute_reliability(
                conn, ExecutiveFilters(period_days=30, site_id=site_id)
            )

        assert canonical['total_downtime_hours'] == 2.0
        assert canonical['outage_count'] == 1
        assert canonical['avg_outage_duration_hours'] == 2.0
        assert canonical['planned_outages'] == 1
        assert canonical['unplanned_outages'] == 1

        for metric, (canonical_key, unit, direction) in expected_meta.items():
            response = client.get(
                '/api/kpi/trend',
                headers=headers,
                params={
                    'family': 'reliability',
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


def test_reliability_operational_why_reuses_resolvable_outage_driver():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            site_id = _seed_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'reliability',
                'metric': 'total_downtime_hours',
                'site_id': site_id,
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['unit'] == 'hours'
        assert payload['direction'] == 'lower_is_better'
        assert payload['drivers']
        driver = payload['drivers'][0]
        assert driver['source_type'] == 'asset_outage'
        assert driver['attribution'] == 'correlation'
        assert driver['source_id'] is not None
        with db() as conn:
            assert conn.execute(
                'SELECT 1 FROM asset_outages WHERE id=?',
                (driver['source_id'],),
            ).fetchone() is not None
        assert 'correlation is not asserted as cause' in payload['disclaimer']
