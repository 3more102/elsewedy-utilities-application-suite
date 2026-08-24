"""HSE WHY regressions: open incidents as scoped contributors.

``open_incidents``/``high_risk_open`` must be explainable through the
canonical open-incident extraction whose base predicates and scope are
identical to the counts themselves. Contributors carry ``contributor``
attribution and resolve to real safety_incidents rows.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_hse_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_incident_fleet(conn):
    """Site A: three open incidents (risk 12, 6, 2) plus one closed high-risk
    record; site B: one open high-risk incident out of scope."""
    suffix = uuid.uuid4().hex[:8].upper()
    sites = {}
    for key in ('a', 'b'):
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'HW-{key}-{suffix}', f'HSE why site {key} {suffix}',
             'Greater Cairo', 'Cairo', 'Operations Centre', 200))
        sites[key] = int(cur.lastrowid)

    admin = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])
    stamp = now()
    counter = {'n': 0}

    def incident(site_key, *, risk, status='Open'):
        counter['n'] += 1
        severity = min(5, max(1, risk // 3 or 1))
        cur = conn.execute(
            '''INSERT INTO safety_incidents(incident_no,incident_type,title,
                 site_id,reported_by,severity,probability,risk_score,status,
                 description,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (f'INC-HW-{suffix}-{counter["n"]:02d}', 'Injury',
             f'HSE why probe {counter["n"]} {suffix}', sites[site_key],
             admin, severity, max(1, risk // severity), risk, status,
             'regression', stamp, stamp))
        return int(cur.lastrowid)

    ids = {
        'a_risk12': incident('a', risk=12),
        'a_risk6': incident('a', risk=6),
        'a_risk2': incident('a', risk=2),
        'a_closed_risk16': incident('a', risk=16, status='Closed'),
        'b_risk15': incident('b', risk=15),
    }
    return {'site_a': sites['a'], 'ids': ids, 'suffix': suffix}


def test_hse_open_incident_why_cites_only_scoped_open_records():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_incident_fleet(conn)

        with db() as conn:
            canonical = compute_hse_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seed['site_a']))

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'hse', 'metric': 'open_incidents',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        # Value parity with the canonical count.
        assert payload['value'] == canonical['open_incidents']
        assert canonical['open_incidents'] == 3

        drivers = payload['drivers']
        assert {d['source_id'] for d in drivers} == {
            seed['ids']['a_risk12'], seed['ids']['a_risk6'],
            seed['ids']['a_risk2']}
        with db() as conn:
            for driver in drivers:
                assert driver['kind'] == 'open_incident'
                assert driver['attribution'] == 'contributor'
                assert driver['source_type'] == 'safety_incident'
                row = conn.execute(
                    '''SELECT status,site_id FROM safety_incidents WHERE id=?''',
                    (driver['source_id'],)).fetchone()
                assert row is not None
                assert str(row['status']) not in ('Closed', 'Cancelled')
                assert int(row['site_id']) == seed['site_a']

        # Ranked by risk, highest first.
        risks = [d['magnitude'] for d in drivers]
        assert risks == sorted(risks, reverse=True)


def test_high_risk_why_excludes_below_threshold_incidents():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_incident_fleet(conn)

        with db() as conn:
            canonical = compute_hse_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seed['site_a']))

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'hse', 'metric': 'high_risk_incidents_open',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload['value'] == canonical['high_risk_open']
        assert canonical['high_risk_open'] == 1
        assert len(payload['drivers']) == 1
        driver = payload['drivers'][0]
        assert driver['kind'] == 'high_risk_incident'
        assert driver['source_id'] == seed['ids']['a_risk12']
        assert driver['magnitude'] >= 12
