"""Reliability KPI WHY scope and planned-vs-forced attribution regressions.

Guarantees being pinned down:

1. ``compute_reliability`` outage numerator respects exactly the same
   executive scope (site/region/asset type/criticality) as the availability
   denominator.
2. WHY contributor records returned through ``/api/kpi/explanation`` never
   include an outage outside the requested scope.
3. Explaining the ``planned_outages`` metric uses planned (non-forced)
   outage records only; unplanned metrics never cite planned records.
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


def _insert_asset(conn, suffix, label, site_id, *, criticality, asset_type_id,
                  category='Transformer'):
    stamp = now()
    cur = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,asset_type_id,criticality,
                              condition,status,location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (f'AST-{label}-{suffix}', f'{label} asset {suffix}', category,
         asset_type_id, criticality, 'Good', 'Operating',
         int(conn.execute('SELECT id FROM locations WHERE site_id=? ORDER BY id LIMIT 1',
                          (site_id,)).fetchone()[0]),
         stamp, stamp),
    )
    return int(cur.lastrowid)


def _insert_outage(conn, suffix, tag, asset_id, site_id, outage_type,
                   start_at, end_at):
    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])
    stamp = now()
    conn.execute(
        '''INSERT INTO asset_outages(
               outage_no,asset_id,site_id,outage_type,status,start_at,end_at,
               reported_by,created_at,updated_at)
           VALUES(?,?,?,?,'Closed',?,?,?,?,?)''',
        (f'OUT-{tag}-{suffix}', asset_id, site_id, outage_type,
         start_at.isoformat(timespec='seconds'),
         end_at.isoformat(timespec='seconds'),
         user_id, stamp, stamp),
    )


def _seed_scope(conn):
    """Two sites in two regions; each with a critical transformer plus one
    low-criticality pump. Site A additionally gets a planned outage."""
    suffix = uuid.uuid4().hex[:8].upper()
    cur = conn.execute(
        '''INSERT INTO asset_types(code,name,utility_domain) VALUES(?,?,?)''',
        (f'TT-{suffix}', f'Transformer type {suffix}', 'Electrical'))
    transformer_type_id = int(cur.lastrowid)
    cur = conn.execute(
        '''INSERT INTO asset_types(code,name,utility_domain) VALUES(?,?,?)''',
        (f'PP-{suffix}', f'Pump type {suffix}', 'Water'))
    pump_type_id = int(cur.lastrowid)

    sites = {}
    for key, region in (('a', 'Region Alpha'), ('b', 'Region Beta')):
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'SC-{key}-{suffix}', f'Scope site {key} {suffix}', region,
             'Cairo', 'Electrical Substation', 1000))
        site_id = int(cur.lastrowid)
        conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'LC-{key}-{suffix}', f'Scope bay {key} {suffix}', 'Area', site_id))
        sites[key] = site_id

    assets = {
        'a_crit': _insert_asset(conn, suffix, 'CRA', sites['a'],
                                criticality='Critical',
                                asset_type_id=transformer_type_id),
        'a_low': _insert_asset(conn, suffix, 'LRA', sites['a'],
                               criticality='Low', category='Pump',
                               asset_type_id=pump_type_id),
        'b_crit': _insert_asset(conn, suffix, 'CRB', sites['b'],
                                criticality='Critical',
                                asset_type_id=transformer_type_id),
    }

    forced_start = datetime.now() - timedelta(days=2)
    _insert_outage(conn, suffix, 'FA', assets['a_crit'], sites['a'], 'Forced',
                   forced_start, forced_start + timedelta(hours=2))
    _insert_outage(conn, suffix, 'FB', assets['b_crit'], sites['b'], 'Forced',
                   forced_start, forced_start + timedelta(hours=3))
    _insert_outage(conn, suffix, 'FL', assets['a_low'], sites['a'], 'Forced',
                   forced_start, forced_start + timedelta(hours=5))
    planned_start = datetime.now() - timedelta(days=4)
    _insert_outage(conn, suffix, 'PA', assets['a_crit'], sites['a'], 'Planned',
                   planned_start, planned_start + timedelta(hours=1))

    return {
        'site_a': sites['a'],
        'region_a': 'Region Alpha',
        'asset_type_transformer': transformer_type_id,
        'asset_type_pump': pump_type_id,
    }


# --------------------------------------------------------------------------- #
# 1. Value-level scope: outage numerator matches denominator scope
# --------------------------------------------------------------------------- #

def test_scoped_availability_counts_only_in_scope_outages():
    with TestClient(app) as client:
        _auth(client)
        with db() as conn:
            seed = _seed_scope(conn)

        with db() as conn:
            critical_only = compute_reliability(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a'],
                                       criticality='Critical'))
            transformers_only = compute_reliability(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a'],
                                       asset_type_id=seed['asset_type_transformer']))
            pumps_only = compute_reliability(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a'],
                                       asset_type_id=seed['asset_type_pump']))
            whole_site = compute_reliability(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a']))

        # Site A holds one critical/transformer outage (2h), one low/pump
        # outage (5h) plus a planned outage that never enters Forced metrics.
        assert critical_only['outage_count'] == 1
        assert critical_only['total_downtime_hours'] == 2.0
        assert transformers_only['outage_count'] == 1
        assert transformers_only['total_downtime_hours'] == 2.0
        assert pumps_only['outage_count'] == 1
        assert pumps_only['total_downtime_hours'] == 5.0
        assert whole_site['outage_count'] == 2
        assert whole_site['total_downtime_hours'] == 7.0


# --------------------------------------------------------------------------- #
# 2. WHY contributors stay inside the requested scope
# --------------------------------------------------------------------------- #

def _explanation(client, headers, **params):
    response = client.get('/api/kpi/explanation', headers=headers,
                          params={'family': 'reliability',
                                  'metric': 'total_downtime_hours',
                                  'period_days': 30, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_why_contributors_exclude_outages_outside_requested_site():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_scope(conn)

        payload = _explanation(client, headers, site_id=seed['site_a'])
        assert payload['drivers'], 'expected in-scope contributors'
        with db() as conn:
            for driver in payload['drivers']:
                assert driver['source_type'] == 'asset_outage'
                row = conn.execute(
                    'SELECT site_id,outage_type FROM asset_outages WHERE id=?',
                    (driver['source_id'],)).fetchone()
                assert row is not None, driver
                assert int(row['site_id']) == seed['site_a']


def test_why_contributors_exclude_outages_outside_requested_region():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_scope(conn)

        payload = _explanation(client, headers, region=seed['region_a'])
        with db() as conn:
            for driver in payload['drivers']:
                row = conn.execute(
                    'SELECT o.site_id, s.region FROM asset_outages o'
                    ' JOIN assets a ON a.id=o.asset_id'
                    ' LEFT JOIN locations l ON l.id=a.location_id'
                    ' LEFT JOIN sites s ON s.id=l.site_id'
                    ' WHERE o.id=?', (driver['source_id'],)).fetchone()
                assert row is not None, driver
                assert str(row['region']) == seed['region_a']


# --------------------------------------------------------------------------- #
# 3. Planned-outage WHY uses planned records only
# --------------------------------------------------------------------------- #

def test_planned_outage_why_cites_only_planned_records():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_scope(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'reliability', 'metric': 'planned_outages',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['drivers'], 'expected planned contributors'
        with db() as conn:
            for driver in payload['drivers']:
                assert driver['kind'] == 'planned_outage'
                row = conn.execute(
                    'SELECT outage_type,site_id FROM asset_outages WHERE id=?',
                    (driver['source_id'],)).fetchone()
                assert row is not None, driver
                assert str(row['outage_type']) != 'Forced'
                assert int(row['site_id']) == seed['site_a']

        unplanned = _explanation(client, headers, site_id=seed['site_a'])
        with db() as conn:
            for driver in unplanned['drivers']:
                row = conn.execute(
                    'SELECT outage_type FROM asset_outages WHERE id=?',
                    (driver['source_id'],)).fetchone()
                assert str(row['outage_type']) == 'Forced'
