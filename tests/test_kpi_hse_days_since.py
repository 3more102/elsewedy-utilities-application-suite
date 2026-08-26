"""HSE days-since trend/WHY coverage.

``days_since_last_high_risk`` is anchored at the requested period end and
bounded to incidents occurring on or before that day, so historical trend
buckets report the gap that genuinely stood on each day (a later incident
must not leak into an earlier bucket). The WHY view cites the determining
incident as a contributor plus recent high-risk context as correlations.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_hse_kpis
from app.main import app

HIGH_RISK_SCORE = 12


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_incident_site(conn):
    """Site A: high-risk incident 10 days ago + one 40 days ago.
    Site B: its own high-risk incident 3 days ago."""
    suffix = uuid.uuid4().hex[:8].upper()
    stamp = now()
    reporter = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])

    def _site(tag: str) -> int:
        site = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'DSH-{tag}-{suffix}', f'Days-since site {tag} {suffix}',
             'Greater Cairo', 'Cairo', 'Electrical Substation', 120),
        )
        return int(site.lastrowid)

    def _incident(tag: str, site_id: int, *, days_ago: int) -> int:
        occurred = (datetime.now() - timedelta(days=days_ago)).isoformat(
            timespec='seconds')
        cur = conn.execute(
            '''INSERT INTO safety_incidents(
                   incident_no,incident_type,title,site_id,reported_by,
                   severity,probability,risk_score,status,description,
                   corrective_action,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (f'INC-DSH-{suffix}-{tag}', 'Injury',
             f'High-risk probe {tag}', site_id, reporter,
             4, 4, HIGH_RISK_SCORE, 'Open', 'days-since probe', '',
             occurred, stamp),
        )
        return int(cur.lastrowid)

    site_a = _site('A')
    incident_10d = _incident('R10', site_a, days_ago=10)
    _incident('R45', site_a, days_ago=45)
    site_b = _site('B')
    _incident('OTH', site_b, days_ago=3)
    return {
        'site_a': site_a,
        'site_b': site_b,
        'incident_10d': incident_10d,
    }


def test_days_since_is_genuinely_as_of_per_window():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_incident_site(conn)
        with db() as conn:
            canonical = compute_hse_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seeded['site_a']))

        assert canonical['days_since_last_high_risk'] == 10
        assert canonical['last_high_risk_at'] is not None

        # The previous 30-day bucket ends 30 days ago. The 10-day-old
        # incident had not happened yet, so the gap is measured from the
        # 45-day-old incident: 45 - 30 = 15 days. A snapshot echo would
        # repeat today's 10 into that bucket.
        response = client.get(
            '/api/kpi/trend',
            headers=headers,
            params={
                'family': 'hse',
                'metric': 'days_since_last_high_risk',
                'site_id': seeded['site_a'],
                'period_days': 30,
                'samples': 2,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['unit'] == 'days'
        assert payload['direction'] == 'higher_is_better'
        values = [sample['value'] for sample in payload['samples']]
        assert values[-1] == 10
        assert values[0] == 15


def test_days_since_why_cites_the_determining_incident():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_incident_site(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'hse',
                'metric': 'days_since_last_high_risk',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 10

        drivers = payload['drivers']
        assert drivers, 'the determining incident must be cited'
        first = drivers[0]
        assert first['attribution'] == 'contributor'
        assert first['magnitude'] == 10
        assert first['source_id'] == seeded['incident_10d']
        assert first['drill']['module'] == 'hse'
        # Older high-risk records appear only as correlation context.
        assert all(d['attribution'] in {'contributor', 'correlation'}
                   for d in drivers[1:])
        assert all(d['magnitude'] >= first['magnitude']
                   for d in drivers[1:])


def test_days_since_respects_site_scope():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = _seed_incident_site(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'hse',
                'metric': 'days_since_last_high_risk',
                'site_id': seeded['site_a'],
                'period_days': 30,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['value'] == 10, (
            "another site's fresher incident must not shrink this gap")
        cited = {d['source_id'] for d in payload['drivers']}
        with db() as conn:
            other_site_incidents = {
                int(row['id'])
                for row in conn.execute(
                    'SELECT id FROM safety_incidents WHERE site_id=?',
                    (seeded['site_b'],)).fetchall()
            }
        assert cited.isdisjoint(other_site_incidents)
