"""Risk-weighted backlog: transparent scoring, explainability and KPI wiring."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _asset_id(asset_no):
    with db() as conn:
        return conn.execute('SELECT id FROM assets WHERE asset_no=?', (asset_no,)).fetchone()['id']


def insert_work_order(wo_no, *, asset_id=None, priority='Medium', status='Assigned',
                      work_type='Corrective Maintenance', target_finish=None,
                      safety_requirements='', created_at=None):
    stamp = created_at or now()
    with db() as conn:
        conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,target_finish,
               safety_requirements,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (wo_no, f'Risk regression {wo_no}', priority, status, work_type, asset_id,
             target_finish, safety_requirements, stamp, stamp))


def test_risk_ranking_is_explainable_and_ordered():
    with TestClient(app) as client:
        admin = auth(client)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Overdue emergency job on a Critical asset must outrank a fresh low job.
        insert_work_order('WO-RISK-HIGH', asset_id=_asset_id('TR-001'), priority='Emergency',
                          work_type='Emergency', target_finish=yesterday,
                          safety_requirements='LOTO required')
        insert_work_order('WO-RISK-LOW', asset_id=_asset_id('HVAC-401'), priority='Low')

        board = client.get('/api/backlog/risk-weighted', headers=admin).json()
        assert board['count'] >= 2 and board['model']
        items = {i['wo_no']: i for i in board['items']}
        high, low = items['WO-RISK-HIGH'], items['WO-RISK-LOW']
        assert high['risk_score'] > low['risk_score']
        assert high['high_risk'] is True
        assert high['days_overdue'] == 1
        factor_names = {f['factor'] for f in high['factors']}
        assert {'asset_criticality', 'priority', 'delay_exposure', 'queue_aging',
                'safety', 'operational_alarms'} <= factor_names
        assert all('contribution' in f and 'detail' in f for f in high['factors'])
        # Contributions add up to the score.
        assert abs(sum(f['contribution'] for f in high['factors']) - min(100.0, sum(f['contribution'] for f in high['factors']))) < 0.01
        assert board['total_risk_exposure'] >= sum(i['risk_score'] for i in board['items']) - 0.5


def test_high_risk_backlog_kpi_matches_board():
    with TestClient(app) as client:
        admin = auth(client)
        kpis = {k['code']: k for k in client.get('/api/kpis', headers=admin).json()['kpis']}
        assert 'KPI-RISK-BACKLOG' in kpis
        snap = client.post(f"/api/kpis/{kpis['KPI-RISK-BACKLOG']['id']}/recalculate",
                           headers=admin).json()['snapshot']
        board = client.get('/api/backlog/risk-weighted', headers=admin).json()
        assert snap['value'] == board['high_risk_count']
        codes = [c['record_code'] for c in
                 client.get(f"/api/kpis/{kpis['KPI-RISK-BACKLOG']['id']}/drilldown",
                            headers=admin).json()['drilldown']['contributors']]
        assert all(code in [i['wo_no'] for i in board['items']] for code in codes)


def test_site_scope_limits_backlog_board():
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        alx = next(s for s in ref['sites'] if s['site_code'] == 'ALX-OPS')
        insert_work_order('WO-RISK-ALX', asset_id=_asset_id('HVAC-401'))
        scoped = client.get('/api/backlog/risk-weighted', headers=admin,
                            params={'site_id': alx['id']}).json()
        assert scoped['items'], 'expected at least the Alexandria work order'
        assert 'WO-RISK-HIGH' not in {i['wo_no'] for i in scoped['items']}
        assert any(i['wo_no'] == 'WO-RISK-ALX' for i in scoped['items'])
