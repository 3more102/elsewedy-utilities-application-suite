"""Bad-actor detection: assets disproportionately responsible for failures,
downtime and maintenance cost, with transparent evidence shares."""
from datetime import date, datetime, timedelta

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


def insert_completed_failure(wo_no, asset_no, *, failure_code, days_ago, hours=3.0, cost=400.0):
    finish = f'{(date.today()-timedelta(days=days_ago)).isoformat()}T16:00:00'
    start = f'{(date.today()-timedelta(days=days_ago)).isoformat()}T13:00:00'
    with db() as conn:
        cur = conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,failure_code,
               actual_start,actual_finish,actual_hours,actual_cost,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (wo_no, f'Bad actor regression {wo_no}', 'High', 'Completed',
             'Corrective Maintenance', _asset_id(asset_no), failure_code,
             start, finish, hours, cost, now(), now()))
        wo_id = cur.lastrowid
        if cost > 0:
            poster = conn.execute("SELECT id FROM users WHERE username='system'").fetchone()
            conn.execute('''INSERT INTO maintenance_cost_ledger(entry_no,work_order_id,asset_id,cost_type,amount,quantity,reference,posted_by,posted_at)
                            VALUES(?,?,?,?,?,?,?,?)'''.replace('VALUES(?,?,?,?,?,?,?,?)', 'VALUES(?,?,?,?,?,?,?,?,?)'),
                         (f'COST-BA-{wo_no}', wo_id, _asset_id(asset_no), 'Corrective', cost, 1, wo_no,
                          poster['id'] if poster else 1, finish))
        return wo_id


def test_bad_actor_flagging_and_evidence():
    with TestClient(app) as client:
        admin = auth(client)
        # HVAC-401 has no outage record, so work-order failure evidence is used.
        # Three filter failures inside the window (repeat failure code) + cost.
        for i in range(3):
            insert_completed_failure(f'WO-BA-HVAC-{i}', 'HVAC-401', failure_code='FILTER',
                                     days_ago=10 + i * 5, cost=500)
        # CB-101: a single minor event.
        insert_completed_failure('WO-BA-CB-0', 'CB-101', failure_code='MECH',
                                 days_ago=20, cost=50)

        report = client.get('/api/reliability/bad-actors', headers=admin,
                            params={'period_days': 365, 'limit': 100}).json()
        assert report['summary']['methodology']
        assets = {a['asset_no']: a for a in report['assets']}
        hvac, cb = assets['HVAC-401'], assets['CB-101']
        assert hvac['failures'] >= 3
        assert hvac['evidence_share'] > cb['evidence_share']
        assert hvac['bad_actor'] is True
        assert any('failure share' in r or 'composite' in r for r in hvac['reasons'])
        assert hvac['repeat_failure_codes'] >= 2


def test_bad_actor_site_scope_excludes_other_sites():
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        alx = next(s for s in ref['sites'] if s['site_code'] == 'ALX-OPS')
        report = client.get('/api/reliability/bad-actors', headers=admin,
                            params={'site_id': alx['id'], 'period_days': 365}).json()
        nos = {a['asset_no'] for a in report['assets']}
        assert 'CB-101' not in nos  # NCS asset must not leak into Alexandria scope
        assert 'HVAC-401' in nos


def test_inactive_asset_never_flagged_without_evidence():
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        alx_loc = next(l for l in ref['locations'] if l['location_code'] == 'ALX-MEP')
        made = client.post('/api/assets', headers=admin, json={
            'name': 'Bad Actor Control Asset', 'category': 'HVAC',
            'location_id': alx_loc['id']})
        assert made.status_code in (200, 201), made.text
        report = client.get('/api/reliability/bad-actors', headers=admin,
                            params={'period_days': 90}).json()
        # Assets with zero failure/downtime/cost evidence are excluded from the
        # ranking rather than shown with a fabricated score.
        assert made.json()['asset_no'] not in {a['asset_no'] for a in report['assets']}
        assert all(a['bad_actor'] == (len(a['reasons']) > 0) for a in report['assets'])
