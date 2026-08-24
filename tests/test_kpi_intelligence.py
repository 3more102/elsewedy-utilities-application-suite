"""KPI intelligence layer: trend samples, staleness, variance surfacing and
period-over-period causal explanation. Also proves aggregate KPI endpoints
stay behind their capability overlays."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def insert_work_order(wo_no, *, asset_id=None, location_id=None, status='Assigned',
                      work_type='Corrective Maintenance', target_finish=None):
    with db() as conn:
        conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,location_id,
               target_finish,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (wo_no, f'Intelligence regression {wo_no}', 'Medium', status, work_type,
             asset_id, location_id, target_finish, now(), now()))


def _overdue_kpi(client, headers):
    kpis = {k['code']: k for k in client.get('/api/kpis', headers=headers).json()['kpis']}
    return kpis['KPI-OVERDUE-WO']


def test_trend_returns_chronological_samples_with_bounds():
    with TestClient(app) as client:
        admin = auth(client)
        kpi = _overdue_kpi(client, admin)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-INTEL-1', target_finish=yesterday)
        client.post(f"/api/kpis/{kpi['id']}/recalculate", headers=admin)
        insert_work_order('WO-INTEL-2', target_finish=yesterday)
        client.post(f"/api/kpis/{kpi['id']}/recalculate", headers=admin)

        trend = client.get(f"/api/kpis/{kpi['id']}/trend", headers=admin,
                           params={'samples': 10}).json()
        values = [s['value'] for s in trend['samples']]
        assert len(values) >= 2 and values == sorted(values)
        stamps = [s['calculated_at'] for s in trend['samples']]
        assert stamps == sorted(stamps)
        assert trend['min'] == min(values) and trend['max'] == max(values)
        assert trend['unit'] == 'count'


def test_staleness_flag_reflects_snapshot_age():
    with TestClient(app) as client:
        admin = auth(client)
        kpi = _overdue_kpi(client, admin)
        fresh = client.get('/api/kpis', headers=admin).json()['kpis']
        me = next(k for k in fresh if k['id'] == kpi['id'])
        # Never recalculated within this test run and seed snapshots are absent
        # until a recalculation or automation run happens: stale must be True.
        if me['latest'] is None:
            assert me['stale'] is True
        else:
            assert me['stale'] is False

        client.post(f"/api/kpis/{kpi['id']}/recalculate", headers=admin)
        me = next(k for k in client.get('/api/kpis', headers=admin).json()['kpis']
                  if k['id'] == kpi['id'])
        assert me['stale'] is False

        # Backdate beyond twice the refresh interval -> stale.
        with db() as conn:
            old = (datetime.now() - timedelta(hours=12)).isoformat(timespec='seconds')
            conn.execute('UPDATE kpi_snapshots SET calculated_at=? WHERE kpi_id=?', (old, kpi['id']))
        me = next(k for k in client.get('/api/kpis', headers=admin).json()['kpis']
                  if k['id'] == kpi['id'])
        assert me['stale'] is True


def test_variance_fields_surface_previous_period_comparison():
    with TestClient(app) as client:
        admin = auth(client)
        kpi = _overdue_kpi(client, admin)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        client.post(f"/api/kpis/{kpi['id']}/recalculate", headers=admin)
        insert_work_order('WO-INTEL-3', target_finish=yesterday)
        second = client.post(f"/api/kpis/{kpi['id']}/recalculate", headers=admin).json()
        snap = second['snapshot']
        me = next(k for k in client.get('/api/kpis', headers=admin).json()['kpis']
                  if k['id'] == kpi['id'])
        assert me['variance_absolute'] == round(snap['value'] - snap['previous_value'], 4)
        assert me['variance_pct'] == snap['change_pct']
        assert me['latest']['previous_value'] == snap['previous_value']


def test_explanation_reports_new_contributors_as_evidence():
    with TestClient(app) as client:
        admin = auth(client)
        kpi = _overdue_kpi(client, admin)
        # Baseline: nothing overdue as of 30 days ago (target finish yesterday
        # only becomes overdue relative to the current window end).
        explanation = client.get(f"/api/kpis/{kpi['id']}/explanation",
                                 headers=admin).json()
        assert explanation['windows']['current']['end'] >= explanation['windows']['previous']['end']
        assert explanation['disclaimer']
        assert 'correlation' in explanation['disclaimer'].lower()

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-INTEL-EXP', target_finish=yesterday)
        after = client.get(f"/api/kpis/{kpi['id']}/explanation", headers=admin).json()
        codes = {c['record_code'] for c in after['new_contributors']}
        assert 'WO-INTEL-EXP' in codes
        assert after['delta'] and after['delta'] > 0
        assert after['improved'] is False  # lower-is-better metric moved up


def test_explanations_and_trend_respect_role_gating():
    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        store = auth(client, 'store', 'Store@2026')
        kpi = _overdue_kpi(client, admin)
        for path in ('explanation', 'trend'):
            assert client.get(f"/api/kpis/{kpi['id']}/{path}", headers=tech).status_code == 403
            assert client.get(f"/api/kpis/{kpi['id']}/{path}", headers=store).status_code == 403
            assert client.get(f"/api/kpis/{kpi['id']}/{path}", headers=admin).status_code == 200


def test_aggregate_endpoints_do_not_leak_across_scopes():
    """Site-scoped aggregation must only contain that site's evidence."""
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        alx = next(s for s in ref['sites'] if s['site_code'] == 'ALX-OPS')
        ncs = next(s for s in ref['sites'] if s['site_code'] == 'NCS-01')
        alx_loc = next(l for l in ref['locations'] if l['location_code'] == 'ALX-MEP')

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-INTEL-SITE', location_id=alx_loc['id'], target_finish=yesterday)

        scoped = client.post('/api/kpis', headers=admin, json={
            'code': 'KPI-INTEL-SCOPE', 'name': 'Scoped overdue probe',
            'source_key': 'overdue_work_orders', 'scope': {'site_id': alx['id']},
            'caution_value': 0, 'alert_value': 5}).json()
        snap = client.get(f"/api/kpis/{scoped['id']}/drilldown", headers=admin).json()
        codes = [c['record_code'] for c in snap['drilldown']['contributors']]
        assert 'WO-INTEL-SITE' in codes
        # A different site's overdue evidence must not appear under this scope.
        board_ncs = client.get('/api/backlog/risk-weighted', headers=admin,
                               params={'site_id': ncs['id']}).json()
        assert all(i['wo_no'] != 'WO-INTEL-SITE' for i in board_ncs['items'])


def test_unknown_kpi_ids_return_404_not_leaked_data():
    with TestClient(app) as client:
        admin = auth(client)
        for path in ('history', 'trend', 'explanation', 'drilldown'):
            r = client.get(f'/api/kpis/999999/{path}', headers=admin)
            assert r.status_code == 404
