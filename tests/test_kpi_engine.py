"""Regression coverage for the configurable KPI engine (definitions, snapshots,
threshold/status evaluation, history, drill-down and permissions)."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_engine import evaluate_status
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def insert_work_order(wo_no, *, asset_id=None, location_id=None, priority='Medium',
                      status='Assigned', work_type='Corrective Maintenance', pm_plan_id=None,
                      target_finish=None, actual_start=None, actual_finish=None,
                      created_at=None):
    stamp = created_at or now()
    with db() as conn:
        cur = conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,location_id,pm_plan_id,
               target_finish,actual_start,actual_finish,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (wo_no, f'KPI regression {wo_no}', priority, status, work_type, asset_id, location_id,
             pm_plan_id, target_finish, actual_start, actual_finish, stamp, stamp))
        return cur.lastrowid


# ---------- unit: threshold / status engine ----------

def test_status_higher_is_better_boundaries():
    # caution=85 alert=75: equality must land on the better band.
    assert evaluate_status(85, 85, 75, 'higher_is_better') == 'GREEN'
    assert evaluate_status(84.9, 85, 75, 'higher_is_better') == 'AMBER'
    assert evaluate_status(75, 85, 75, 'higher_is_better') == 'AMBER'
    assert evaluate_status(74.9, 85, 75, 'higher_is_better') == 'RED'
    assert evaluate_status(None, 85, 75, 'higher_is_better') == 'UNKNOWN'
    assert evaluate_status(50, None, None, 'higher_is_better') == 'UNKNOWN'


def test_status_lower_is_better_boundaries():
    # caution=5 alert=10 for e.g. overdue counts.
    assert evaluate_status(5, 5, 10, 'lower_is_better') == 'GREEN'
    assert evaluate_status(6, 5, 10, 'lower_is_better') == 'AMBER'
    assert evaluate_status(10, 5, 10, 'lower_is_better') == 'AMBER'
    assert evaluate_status(10.1, 5, 10, 'lower_is_better') == 'RED'
    # With only an alert bound the best achievable band is AMBER (symmetry with
    # higher_is_better): GREEN requires an explicit caution/target bound.
    assert evaluate_status(0, None, 10, 'lower_is_better') == 'AMBER'
    assert evaluate_status(11, None, 10, 'lower_is_better') == 'RED'


def test_status_zero_value_is_not_unknown():
    assert evaluate_status(0, 0, 2, 'lower_is_better') == 'GREEN'
    assert evaluate_status(0.0, 85, 75, 'higher_is_better') == 'RED'


# ---------- API: catalog seed and permissions ----------

def test_seed_catalog_and_role_gating():
    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        store = auth(client, 'store', 'Store@2026')

        listing = client.get('/api/kpis', headers=admin)
        assert listing.status_code == 200, listing.text
        kpis = listing.json()['kpis']
        codes = {k['code'] for k in kpis}
        assert {'KPI-PM-COMP', 'KPI-OVERDUE-WO', 'KPI-MTTR', 'KPI-AVAIL', 'KPI-ALARM-CRIT'} <= codes
        assert all(k['source_supported'] for k in kpis)

        # Technicians and storekeepers are outside the KPI read surface.
        assert client.get('/api/kpis', headers=tech).status_code == 403
        assert client.get('/api/kpis', headers=store).status_code == 403
        # Manage capability is narrower than read.
        assert client.post('/api/kpis', headers=tech, json={}).status_code in (401, 403, 422)


def test_create_validation_and_duplicate_code():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        bad_source = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-BAD-SRC', 'name': 'Bad source', 'source_key': 'not_a_provider'})
        assert bad_source.status_code == 422
        bad_direction = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-BAD-DIR', 'name': 'Bad direction', 'source_key': 'backlog_open',
            'direction': 'sideways'})
        assert bad_direction.status_code == 422
        ok = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-DUP', 'name': 'Duplicate probe', 'source_key': 'backlog_open'})
        assert ok.status_code == 201, ok.text
        dup = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-DUP', 'name': 'Duplicate probe again', 'source_key': 'backlog_open'})
        assert dup.status_code == 409


# ---------- API: recalculation, persistence, history, trend ----------

def test_recalculate_persists_snapshot_history_and_trend():
    with TestClient(app) as client:
        admin = auth(client)
        overdue = next(k for k in client.get('/api/kpis', headers=admin).json()['kpis']
                       if k['code'] == 'KPI-OVERDUE-WO')

        first = client.post(f"/api/kpis/{overdue['id']}/recalculate", headers=admin)
        assert first.status_code == 200, first.text
        body = first.json()
        snap1 = body['snapshot']
        assert snap1['kpi_id'] == overdue['id']
        assert snap1['status'] in ('GREEN', 'AMBER', 'RED', 'UNKNOWN')
        assert snap1['provenance_json']
        assert snap1['period_start'] <= snap1['period_end']

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-KPI-OVD-1', target_finish=yesterday)
        second = client.post(f"/api/kpis/{overdue['id']}/recalculate", headers=admin).json()
        snap2 = second['snapshot']
        assert snap2['value'] == snap1['value'] + 1
        assert snap2['previous_value'] == snap1['value']
        assert snap2['trend'] == 'up'

        hist = client.get(f"/api/kpis/{overdue['id']}/history", headers=admin).json()
        values = [h['value'] for h in hist['history']]
        assert len(values) >= 2 and values[0] == snap2['value'] and values[1] == snap1['value']


def test_drilldown_returns_contributing_source_records():
    with TestClient(app) as client:
        admin = auth(client)
        overdue = next(k for k in client.get('/api/kpis', headers=admin).json()['kpis']
                       if k['code'] == 'KPI-OVERDUE-WO')
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-KPI-DRILL-1', target_finish=yesterday)

        drill = client.get(f"/api/kpis/{overdue['id']}/drilldown", headers=admin).json()
        assert drill['drilldown']['formula']
        codes = [c['record_code'] for c in drill['drilldown']['contributors']]
        assert 'WO-KPI-DRILL-1' in codes
        assert drill['drilldown']['record_types']['work_order']


def test_recalculate_all_covers_active_catalog():
    with TestClient(app) as client:
        admin = auth(client)
        result = client.post('/api/kpis/recalculate-all', headers=admin)
        assert result.status_code == 200, result.text
        payload = result.json()
        assert not payload['skipped']
        recalculated = {r['code'] for r in payload['recalculated']}
        assert {'KPI-PM-COMP', 'KPI-MTBF', 'KPI-AVAIL'} <= recalculated
        for row in payload['recalculated']:
            assert row['status'] in ('GREEN', 'AMBER', 'RED', 'UNKNOWN')


# ---------- calculation semantics ----------

def test_no_data_yields_unknown_status():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        ref = client.get('/api/reference', headers=planner).json()
        alexandria = next(s for s in ref['sites'] if s['site_code'] == 'ALX-OPS')
        made = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-NODATA', 'name': 'No data MTTR', 'source_key': 'mttr_hours',
            'scope': {'site_id': alexandria['id']}, 'caution_value': 24, 'alert_value': 48})
        assert made.status_code == 201, made.text
        kpi_id = made.json()['id']
        snap = client.post(f'/api/kpis/{kpi_id}/recalculate', headers=planner).json()['snapshot']
        # No corrective completions at this site => value None, status UNKNOWN, never a fabricated number.
        assert snap['value'] is None
        assert snap['status'] == 'UNKNOWN'
        assert snap['denominator'] in (0, None)


def test_window_boundary_excludes_out_of_period_records():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        today = date.today()
        made = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-WINDOW', 'name': 'Window boundary schedule compliance',
            'source_key': 'schedule_compliance', 'time_window_days': 30,
            'target_value': 100, 'caution_value': 90, 'alert_value': 50})
        assert made.status_code == 201, made.text
        kpi_id = made.json()['id']
        inside_day = (today - timedelta(days=2)).isoformat()
        outside_day = (today - timedelta(days=90)).isoformat()
        insert_work_order('WO-KPI-WIN-IN', status='Completed', work_type='Corrective Maintenance',
                          target_finish=inside_day, actual_start=f'{inside_day}T08:00:00',
                          actual_finish=f'{inside_day}T12:00:00')
        insert_work_order('WO-KPI-WIN-OUT', status='Completed', work_type='Corrective Maintenance',
                          target_finish=outside_day, actual_start=f'{outside_day}T08:00:00',
                          actual_finish=f'{outside_day}T20:00:00')
        snap = client.post(f'/api/kpis/{kpi_id}/recalculate', headers=planner).json()['snapshot']
        # Only the on-time in-window completion counts; the 90-day-old late job is outside the window.
        assert snap['numerator'] >= 1
        assert snap['denominator'] >= 1
        assert snap['numerator'] == snap['denominator']
        assert snap['status'] == 'GREEN'

        late_inside = (today - timedelta(days=1)).isoformat()
        insert_work_order('WO-KPI-WIN-LATE', status='Completed', work_type='Corrective Maintenance',
                          target_finish=(today - timedelta(days=3)).isoformat(),
                          actual_start=f'{late_inside}T08:00:00', actual_finish=f'{late_inside}T18:00:00')
        after = client.post(f'/api/kpis/{kpi_id}/recalculate', headers=planner).json()['snapshot']
        assert after['denominator'] == snap['denominator'] + 1
        assert after['numerator'] == snap['numerator']
        assert after['previous_value'] == snap['value']
        assert after['status'] in ('AMBER', 'RED')


def test_site_scope_isolation():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        ref = client.get('/api/reference', headers=planner).json()
        ncs = next(s for s in ref['sites'] if s['site_code'] == 'NCS-01')
        iwp = next(s for s in ref['sites'] if s['site_code'] == 'IWP-01')
        locs = [l for l in ref['locations'] if l['site_id'] == ncs['id']]
        ncs_loc = locs[0]['id']

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        insert_work_order('WO-KPI-SCOPE-NCS', location_id=ncs_loc, target_finish=yesterday)

        scoped_ncs = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-SCOPE-NCS', 'name': 'Overdue NCS only', 'source_key': 'overdue_work_orders',
            'scope': {'site_id': ncs['id']}}).json()
        scoped_iwp = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-SCOPE-IWP', 'name': 'Overdue IWP only', 'source_key': 'overdue_work_orders',
            'scope': {'site_id': iwp['id']}}).json()
        v_ncs = client.post(f"/api/kpis/{scoped_ncs['id']}/recalculate", headers=planner).json()['snapshot']['value']
        v_iwp = client.post(f"/api/kpis/{scoped_iwp['id']}/recalculate", headers=planner).json()['snapshot']['value']
        assert v_ncs >= 1
        assert v_iwp == 0


def test_direction_changes_status_for_same_data():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        # Guarantee enough open backlog to sit above both alert bounds deterministically.
        for i in range(4):
            insert_work_order(f'WO-KPI-DIR-{i}', status='Assigned')
        lower = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-DIR-LOW', 'name': 'Backlog lower better', 'source_key': 'backlog_open',
            'direction': 'lower_is_better', 'caution_value': 1, 'alert_value': 3}).json()
        higher = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-DIR-HIGH', 'name': 'Backlog higher better', 'source_key': 'backlog_open',
            'direction': 'higher_is_better', 'caution_value': 10, 'alert_value': 5}).json()
        s_low = client.post(f"/api/kpis/{lower['id']}/recalculate", headers=planner).json()['snapshot']
        s_high = client.post(f"/api/kpis/{higher['id']}/recalculate", headers=planner).json()['snapshot']
        assert s_low['value'] == s_high['value']
        assert s_low['value'] >= 6
        # Same underlying value, opposite directions: a high backlog is RED when
        # lower-is-better and at least AMBER (never worse) when higher-is-better.
        assert s_low['status'] == 'RED'
        assert s_high['status'] in ('AMBER', 'GREEN')


# ---------- lifecycle ----------

def test_deactivation_blocks_recalculation_and_hides_from_default_list():
    with TestClient(app) as client:
        planner = auth(client, 'planner', 'Planner@2026')
        made = client.post('/api/kpis', headers=planner, json={
            'code': 'KPI-TEST-LIFECYCLE', 'name': 'Lifecycle probe', 'source_key': 'backlog_open'}).json()
        kpi_id = made['id']
        patched = client.patch(f'/api/kpis/{kpi_id}', headers=planner, json={
            'active': False, 'caution_value': 1, 'alert_value': 500, 'description': 'paused'})
        assert patched.status_code == 200, patched.text
        assert patched.json()['active'] is False
        assert patched.json()['version'] == made['version'] + 1

        blocked = client.post(f'/api/kpis/{kpi_id}/recalculate', headers=planner)
        assert blocked.status_code == 409

        default_list = client.get('/api/kpis', headers=planner).json()['kpis']
        assert all(k['id'] != kpi_id for k in default_list)
        full_list = client.get('/api/kpis', headers=planner, params={'include_inactive': True}).json()['kpis']
        assert any(k['id'] == kpi_id and k['description'] == 'paused' for k in full_list)

        reactivated = client.patch(f'/api/kpis/{kpi_id}', headers=planner, json={'active': True})
        assert reactivated.status_code == 200 and reactivated.json()['active'] is True
