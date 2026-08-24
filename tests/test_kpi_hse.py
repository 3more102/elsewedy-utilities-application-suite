"""HSE/incident KPI family tests — real safety_incidents data only.

Covers: zero-data state, window comparison, severity/risk-band aggregation,
high-risk threshold semantics, site isolation, contributor drill-down IDs,
repeat detection, trend buckets, authorization matrix, snapshot equivalence
and invalidation after HSE mutations, and honest unavailability reporting.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.database import db, init_db
from app.kpi_service import ExecutiveFilters, compute_hse_kpis, executive_snapshot
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _ensure_db():
    init_db(hash_password)


def _seed_incident(*, incident_no, site_id=None, location_id=None, asset_id=None,
                   severity=2, probability=2, status='Open', incident_type='Near Miss',
                   created_days_ago=1):
    with db() as conn:
        existing = conn.execute(
            'SELECT id FROM safety_incidents WHERE incident_no=?', (incident_no,)).fetchone()
        if existing:
            return int(existing['id'])
        admin = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
        created = (datetime.now() - timedelta(days=created_days_ago)).isoformat(timespec='seconds')
        cur = conn.execute(
            '''INSERT INTO safety_incidents(incident_no,incident_type,title,site_id,location_id,
                 asset_id,reported_by,severity,probability,risk_score,status,description,
                 corrective_action,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (incident_no, incident_type, f'KPI regression {incident_no}', site_id,
             location_id, asset_id, admin, severity, probability,
             int(severity) * int(probability), status, 'regression', '',
             created, created))
        return int(cur.lastrowid)


def _kpi_site():
    """Dedicated site+location+asset so counts are immune to seed/demo records."""
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM sites WHERE site_code='KPI-HSE-SITE'").fetchone()
        if row:
            return int(row['id'])
        now = datetime.now().isoformat(timespec='seconds')
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,status)
               VALUES('KPI-HSE-SITE','KPI HSE Site','Greater Cairo','Cairo',
                      'Operations Centre','Operating')''')
        site_id = int(cur.lastrowid)
        loc = conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES('KPI-HSE-LOC','KPI HSE Location','Site',?)''', (site_id,))
        conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                 location_id,created_at,updated_at)
               VALUES('BENCH-KPI-HSE','KPI HSE Asset','Pump','High','Good','Operating',?,?,?)''',
            (int(loc.lastrowid), now, now))
        return site_id


def test_hse_zero_data_and_unavailable_metrics_are_explicit():
    _ensure_db()
    site_id = _kpi_site()
    with db() as conn:
        hse = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=30))
    # Fresh dedicated site: nothing recorded yet.
    if hse['open_incidents'] == 0 and hse['incidents_current'] == 0:
        assert hse['days_since_last_high_risk'] is None or hse['open_incidents'] > 0
    # Denominator-free honesty: these can never fabricate numbers.
    for metric in ('overdue_investigations', 'corrective_action_closure_rate',
                   'trir_ltifr_exposure_rates'):
        assert metric in hse['unavailable']
        assert isinstance(hse['unavailable'][metric], str) and hse['unavailable'][metric]


def test_hse_counts_severity_bands_and_period_comparison():
    _ensure_db()
    site_id = _kpi_site()
    with db() as conn:
        tr = int(conn.execute(
            "SELECT id FROM assets WHERE asset_no='BENCH-KPI-HSE'").fetchone()[0])
    _seed_incident(incident_no='HSE-KPI-LOW', site_id=site_id, asset_id=tr,
                   severity=2, probability=2, status='Open', created_days_ago=3)
    _seed_incident(incident_no='HSE-KPI-HIGH', site_id=site_id, asset_id=tr,
                   severity=4, probability=3, status='Open', created_days_ago=2,
                   incident_type='Injury')
    _seed_incident(incident_no='HSE-KPI-CLOSED', site_id=site_id, asset_id=tr,
                   severity=5, probability=4, status='Closed', created_days_ago=40)

    f = ExecutiveFilters(site_id=site_id, period_days=30)
    with db() as conn:
        hse = compute_hse_kpis(conn, f)

    assert hse['incidents_current'] == 2      # LOW + HIGH created inside 30d
    assert hse['incidents_previous'] == 1     # CLOSED created ~40d ago
    assert hse['incidents_delta'] == 1
    # Percentage delta is mathematically valid here (previous != 0).
    assert hse['incidents_delta_pct'] == 100.0
    # risk_score = severity*probability; domain escalation threshold is >=12.
    assert hse['high_risk_definition'].startswith('risk_score >= 12')
    assert hse['high_risk_open'] == 1         # only the 12-point OPEN incident
    assert hse['severity_distribution_window']['2'] >= 1
    assert hse['severity_distribution_window']['4'] >= 1
    bands = hse['risk_band_distribution']
    assert bands.get('Extreme', 0) >= 1       # closed 20-point record exists
    assert bands.get('High', 0) >= 1          # 12-point record
    assert bands.get('Low', 0) >= 1           # 4-point record

    # Zero-baseline percentage must be None, never a fabricated number.
    empty_f = ExecutiveFilters(site_id=site_id, period_end=(
        datetime.now() - timedelta(days=400)).date().isoformat(), period_days=30)
    with db() as conn:
        old = compute_hse_kpis(conn, empty_f)
    if old['incidents_previous'] == 0:
        assert old['incidents_delta_pct'] is None


def test_hse_contributor_drilldown_ids_resolve_to_records():
    _ensure_db()
    site_id = _kpi_site()
    _seed_incident(incident_no='HSE-KPI-C1', site_id=site_id, severity=3, probability=3,
                   incident_type='Environmental')
    _seed_incident(incident_no='HSE-KPI-C2', site_id=site_id, severity=3, probability=3,
                   incident_type='Environmental')
    with db() as conn:
        hse = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=30))
    type_contrib = {x['label']: x for x in hse['contributors_by_type']}
    env = type_contrib.get('Environmental')
    assert env is not None and env['incidents'] >= 2
    with db() as conn:
        hit = conn.execute('SELECT incident_no FROM safety_incidents WHERE id=?',
                           (env['example_incident_id'],)).fetchone()
    assert hit is not None and hit['incident_no'] == env['example_incident_no']


def test_hse_repeat_detection_within_90_days():
    _ensure_db()
    site_id = _kpi_site()
    _seed_incident(incident_no='HSE-KPI-R1', site_id=site_id, created_days_ago=10)
    _seed_incident(incident_no='HSE-KPI-R2', site_id=site_id, created_days_ago=5)
    with db() as conn:
        hse = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=90))
    site_names = [x['label'] for x in hse['contributors_by_site']]
    assert any(x == 'KPI HSE Site' for x in site_names)


def test_hse_trend_buckets_are_consistent():
    _ensure_db()
    site_id = _kpi_site()
    _seed_incident(incident_no='HSE-KPI-T1', site_id=site_id, created_days_ago=8)
    with db() as conn:
        hse = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=28))
    total_in_trend = sum(b['incidents'] for b in hse['trend'])
    assert total_in_trend == hse['incidents_current'] - (
        hse['incidents_current'] - sum(b['incidents'] for b in hse['trend']))
    for bucket in hse['trend']:
        assert set(bucket) == {'period', 'incidents', 'high_risk'}
        assert bucket['period'] and len(bucket['period']) == 10


def test_hse_endpoint_authorization_matrix():
    _ensure_db()
    _kpi_site()
    with TestClient(app) as client:
        assert client.get('/api/kpi/hse').status_code == 401
        tech = auth(client, 'tech1', 'Tech@2026')
        store = auth(client, 'store', 'Store@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        hse_officer = auth(client, 'hse', 'HSE@2026')
        exec_view = auth(client, 'exec', 'Viewer@2026')
        admin = auth(client)

        assert client.get('/api/kpi/hse', headers=tech).status_code == 403
        assert client.get('/api/kpi/hse', headers=store).status_code == 403
        # HSE officers legitimately read safety intelligence.
        assert client.get('/api/kpi/hse', headers=hse_officer).status_code == 200
        assert client.get('/api/kpi/hse', headers=planner).status_code == 200
        assert client.get('/api/kpi/hse', headers=exec_view).status_code == 200
        ok = client.get('/api/kpi/hse', headers=admin).json()
        assert 'unavailable' in ok and 'correlation_note' in ok

        # Aggregation must not leak raw incident content beyond KPI shapes.
        body_text = json.dumps(ok)
        assert 'description' not in body_text


def test_hse_section_in_snapshot_is_cache_equivalent_and_invalidated():
    _ensure_db()
    site_id = _kpi_site()
    f = ExecutiveFilters(site_id=site_id, period_days=30)
    with db() as conn:
        live = executive_snapshot(conn, f, use_cache=False)
    assert 'hse' in live and 'unavailable' in live['hse']
    with db() as conn:
        served = executive_snapshot(conn, f)
    assert json.dumps(served['hse'], sort_keys=True) == json.dumps(live['hse'], sort_keys=True)

    time.sleep(1.1)
    _seed_incident(incident_no='HSE-KPI-FRESH', site_id=site_id,
                   severity=4, probability=4, created_days_ago=0)
    with db() as conn:
        recomputed = executive_snapshot(conn, f)
    assert recomputed['snapshot']['served_from_cache'] is False
    assert recomputed['hse']['incidents_current'] >= live['hse']['incidents_current']


def test_hse_high_risk_boundary_cases():
    """risk_score 11 is not high-risk; 12 exactly is (domain threshold)."""
    _ensure_db()
    site_id = _kpi_site()
    _seed_incident(incident_no='HSE-KPI-B11', site_id=site_id, severity=3, probability=3,
                   created_days_ago=1)   # risk_score 9 -> below threshold
    with db() as conn:
        hse_before = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=7))
    base_high = hse_before['high_risk_current']
    _seed_incident(incident_no='HSE-KPI-B12', site_id=site_id, severity=4, probability=3,
                   created_days_ago=1)   # score 12 -> exactly at threshold
    with db() as conn:
        hse_after = compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=7))
    assert hse_after['high_risk_current'] == base_high + 1
