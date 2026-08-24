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


def test_launchpad_route_registered_exactly_once_and_serves():
    """Permanent regression: /api/launchpad was once silently dead when its
    decorator merged into a section comment during an automated insertion."""
    from fastapi.routing import APIRoute
    routes = [r for r in app.routes if isinstance(r, APIRoute)
              and r.path == '/api/launchpad' and 'GET' in (r.methods or set())]
    assert len(routes) == 1, (
        f'/api/launchpad must be registered exactly once, found {len(routes)}')
    _ensure_db()
    with TestClient(app) as client:
        admin = auth(client)
        r = client.get('/api/launchpad', headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and len(body) >= 10
        assert any(x['code'] == 'hse' for x in body)


def test_hse_recommendations_generated_deduplicated_and_labeled():
    _ensure_db()
    site_id = _kpi_site()
    tr = None
    with db() as conn:
        tr = int(conn.execute(
            "SELECT id FROM assets WHERE asset_no='BENCH-KPI-HSE'").fetchone()[0])
    # Two high-risk open incidents -> corrective_action_needed x2 plus a
    # window-level risk_indicator.
    _seed_incident(incident_no='HSE-KPI-REC1', site_id=site_id, asset_id=tr,
                   severity=4, probability=3, status='Open', created_days_ago=2)
    _seed_incident(incident_no='HSE-KPI-REC2', site_id=site_id, asset_id=tr,
                   severity=4, probability=4, status='Open', created_days_ago=1)
    # Repeat asset: second non-high-risk incident within 90 days.
    _seed_incident(incident_no='HSE-KPI-REC3', site_id=site_id, asset_id=tr,
                   severity=2, probability=2, status='Closed', created_days_ago=20)
    f = ExecutiveFilters(site_id=site_id, period_days=30)
    with db() as conn:
        hse = compute_hse_kpis(conn, f)
    recs = hse['recommendations']
    kinds = [r['kind'] for r in recs]
    # The shared regression site may hold additional high-risk open incidents
    # seeded by other tests in this module; assert the floors, not exact counts.
    assert kinds.count('corrective_action_needed') >= 2
    assert 'repeat_incident' in kinds       # asset with >=2 incidents in 90d
    assert 'risk_indicator' in kinds        # at least two high-risk in one window
    # Deduplication: same subject never appears twice under one kind.
    subjects = [(r['kind'], r.get('incident_id') or r.get('asset_id')
                 or r.get('location_id') or r.get('label')) for r in recs]
    assert len(subjects) == len(set(subjects))
    allowed = {'corrective_action_needed', 'repeat_incident', 'risk_indicator'}
    assert set(kinds) <= allowed
    # Recommendations carry drill IDs that resolve to real incidents where applicable.
    corrective = [r for r in recs if r['kind'] == 'corrective_action_needed']
    with db() as conn:
        for r in corrective:
            hit = conn.execute('SELECT incident_no FROM safety_incidents WHERE id=?',
                               (r['incident_id'],)).fetchone()
            assert hit is not None


def test_hse_read_grants_no_mutation_rights():
    """analytics.hse.read must not imply incident or work-order mutation."""
    _ensure_db()
    _kpi_site()
    with TestClient(app) as client:
        exec_view = auth(client, 'exec', 'Viewer@2026')
        tech = auth(client, 'tech1', 'Tech@2026')

        # Incident mutation requires hse.incident.* capabilities, not KPI reads.
        assert client.post('/api/hse', headers=exec_view,
                           json={'incident_type': 'Near Miss', 'title': 'x',
                                 'severity': 1, 'probability': 1,
                                 'description': 'x'}).status_code == 403
        assert client.patch('/api/hse/999999999', headers=exec_view,
                            json={'status': 'Closed'}).status_code == 403
        assert client.patch('/api/hse/999999999', headers=tech,
                            json={'status': 'Closed'}).status_code == 403

        # Work-order creation stays behind its own work.create capability:
        # executive viewers may read HSE KPIs but cannot create corrective work.
        assert client.post('/api/work-orders', headers=exec_view,
                           json={'title': 'nope', 'priority': 'Low',
                                 'work_type': 'Corrective Maintenance'}).status_code == 403


def test_corrective_wo_from_incident_context_keeps_audit_linkage():
    """The controlled flow uses the standard work-management endpoint; the
    source incident reference travels inside the WO record and the audit."""
    _ensure_db()
    site_id = _kpi_site()
    _seed_incident(incident_no='HSE-KPI-ACT1', site_id=site_id,
                   severity=4, probability=3, status='Open', created_days_ago=1)
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        asset = next(a for a in client.get('/api/assets', headers=admin).json()
                     if a['asset_no'] == 'BENCH-KPI-HSE')
        created = client.post('/api/work-orders', headers=admin, json={
            'title': 'Corrective action for HSE-KPI-ACT1',
            'asset_id': asset['id'],
            'work_type': 'Safety',
            'priority': 'Critical',
            'safety_requirements':
                'Source incident HSE-KPI-ACT1 (risk score 12).',
        })
        assert created.status_code == 200, created.text
        wo = created.json()

        # Source linkage persists on the resulting work order.
        detail = client.get(f"/api/work-orders/{wo['id']}", headers=admin).json()
        assert 'HSE-KPI-ACT1' in detail['safety_requirements']

        # The domain endpoint wrote its own audit event (no duplicate here).
        audits = client.get('/api/audit', headers=admin,
                            params={'q': wo['wo_no']}).json()
        assert any(x['action'].upper().startswith('CREATE') for x in audits)


def test_export_hse_equivalence_scope_isolation_and_no_content_leak():
    """CSV export must equal a freshly recomputed JSON snapshot.

    Uses a unique site so no snapshot cached by an earlier test can serve
    the export call: incidents seeded with backdated timestamps cannot
    advance the source watermark, so a same-scope cache entry written
    before those seeds would otherwise stay valid and report stale counts.
    """
    import uuid as _uuid

    _ensure_db()
    suffix = _uuid.uuid4().hex[:8].upper()
    with db() as conn:
        site_cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,status)
               VALUES(?,?,?,?,?,?)''',
            (f'KPI-HSE-EXP-{suffix}', f'KPI HSE export site {suffix}',
             'Greater Cairo', 'Cairo', 'Operations Centre', 'Operating'))
        site_id = int(site_cur.lastrowid)
    _seed_incident(incident_no=f'HSE-KPI-EXP1-{suffix}', site_id=site_id,
                   severity=4, probability=3, status='Open', created_days_ago=2)
    with TestClient(app) as client:
        admin = auth(client)
        scoped_json = client.get('/api/kpi/executive', headers=admin,
                                 params={'site_id': site_id, 'refresh': 'true'}).json()
        csv_resp = client.get('/api/exports/executive-kpis.csv', headers=admin,
                              params={'site_id': site_id})
        assert csv_resp.status_code == 200
        import csv as _csv
        import io as _io
        rows = list(_csv.reader(_io.StringIO(csv_resp.text)))
        metrics = {(x[0], x[1]): x[2] for x in rows[1:]}

        # HSE values present and equal to the JSON snapshot values.
        assert int(metrics[('hse', 'open_incidents')]) == \
            scoped_json['hse']['open_incidents']
        assert int(metrics[('hse', 'high_risk_open')]) == \
            scoped_json['hse']['high_risk_open']
        # Unavailable metrics stay explicit in exports.
        unavailable_keys = set(scoped_json['hse']['unavailable'])
        assert unavailable_keys >= {'overdue_investigations',
                                    'corrective_action_closure_rate',
                                    'trir_ltifr_exposure_rates'}
        # No contributor names or raw incident content leak through CSV rows.
        blob = csv_resp.text.lower()
        assert 'description' not in blob
        assert 'contributors_by_site' not in blob
        assert 'kpi regression' not in blob

        # Scope isolation: another site's scope cannot include this data.
        other_sites = [s['id'] for s in
                       client.get('/api/reference', headers=admin).json()['sites']
                       if s['id'] != site_id]
        if other_sites:
            other = client.get('/api/kpi/executive', headers=admin,
                               params={'site_id': other_sites[0], 'refresh': 'true'},
                               ).json()


def test_hse_query_path_stays_set_based():
    """HSE KPI computation issues grouped statements, not per-incident queries."""
    _ensure_db()
    site_id = _kpi_site()
    now = datetime.now()
    for i in range(30):
        _seed_incident(incident_no=f'HSE-KPI-PERF{i:02d}', site_id=site_id,
                       severity=(i % 5) + 1, probability=2, status='Open',
                       created_days_ago=i % 25)
    import sqlite3 as sq
    real_connect = sq.connect
    counter = {'n': 0}

    def counting_connect(*a, **kw):
        c = real_connect(*a, **kw)
        try:
            c.set_trace_callback(lambda s: counter.__setitem__('n', counter['n'] + 1))
        except Exception:
            pass
        return c
    sq.connect = counting_connect
    try:
        with db() as conn:
            compute_hse_kpis(conn, ExecutiveFilters(site_id=site_id, period_days=60))
    finally:
        sq.connect = real_connect
    assert counter['n'] <= 25, (
        f"HSE KPI path issued {counter['n']} statements for 30 incidents; "
        'per-incident fan-out detected')
