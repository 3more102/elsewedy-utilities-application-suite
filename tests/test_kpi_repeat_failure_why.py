"""Repeat-failure WHY regressions: chronic bad actors as contributors.

The ``repeat_failure_rate_pct`` metric must be explainable through the
canonical chronic bad-actor extraction (assets with >= 2 corrective
completions in 90 days, executive-scoped). Contributors are the literal
records composing the numerator, so they carry ``contributor`` attribution
and resolve to real asset rows inside the requested scope.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_service import ExecutiveFilters, compute_maintenance_kpis
from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _seed_repeat_failures(conn):
    """Site A: one chronic bad actor (2 corrective completions in 90d) plus a
    single-failure asset; site B (out of scope): a worse offender. One old
    completion outside the 90-day cutoff proves the window is respected."""
    suffix = uuid.uuid4().hex[:8].upper()
    sites = {}
    for key, region in (('a', 'Region Alpha'), ('b', 'Region Beta')):
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'RF-{key}-{suffix}', f'Repeat site {key} {suffix}', region,
             'Cairo', 'Electrical Substation', 500))
        site_id = int(cur.lastrowid)
        conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'RFL-{key}-{suffix}', f'Repeat bay {key} {suffix}', 'Area',
             site_id))
        sites[key] = site_id

    def asset(label, site_key):
        stamp = now()
        location_id = int(conn.execute(
            'SELECT id FROM locations WHERE site_id=? ORDER BY id LIMIT 1',
            (sites[site_key],)).fetchone()[0])
        return int(conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                  status,location_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'AST-{label}-{suffix}', f'{label} asset {suffix}', 'Transformer',
             'High', 'Good', 'Operating', location_id, stamp, stamp),
        ).lastrowid)

    assets = {
        'chronic_a': asset('RFA', 'a'),
        'single_a': asset('SFA', 'a'),
        'worse_b': asset('WFB', 'b'),
    }

    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])
    stamp = now()
    counter = {'n': 0}

    def wo(asset_id, *, finished_days_ago):
        counter['n'] += 1
        finished = (datetime.now() - timedelta(days=finished_days_ago)).isoformat(timespec='seconds')
        conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                                       asset_id,actual_finish,actual_hours,
                                       created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (f'WO-RF-{suffix}-{counter["n"]:03d}',
             f'Repeat failure probe {counter["n"]} {suffix}',
             'High', 'Completed', 'Corrective', asset_id,
             finished, 3.0, stamp, stamp),
        )

    for _ in range(2):
        wo(assets['chronic_a'], finished_days_ago=10)
    wo(assets['single_a'], finished_days_ago=12)
    for _ in range(5):
        wo(assets['worse_b'], finished_days_ago=8)
    # Outside the 90-day cutoff: must not count toward the chronic actor.
    wo(assets['chronic_a'], finished_days_ago=100)

    return {
        'site_a': sites['a'],
        'chronic_a': assets['chronic_a'],
        'asset_no_prefix': f'AST-RFA-{suffix}',
    }


def test_repeat_failure_rate_explains_scoped_chronic_bad_actors():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_repeat_failures(conn)

        with db() as conn:
            canonical = compute_maintenance_kpis(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a']))

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'maintenance',
                    'metric': 'repeat_failure_rate_pct',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        # The explanation value is the canonical rate, never recomputed.
        assert payload['value'] == canonical['repeat_failure_rate_pct']

        drivers = payload['drivers']
        assert drivers, 'expected chronic bad-actor contributors'
        with db() as conn:
            for driver in drivers:
                assert driver['kind'] == 'repeat_failure'
                assert driver['attribution'] == 'contributor'
                assert driver['source_type'] == 'asset'
                row = conn.execute(
                    'SELECT asset_no FROM assets WHERE id=?',
                    (driver['source_id'],)).fetchone()
                assert row is not None, driver
                assert str(row['asset_no']) == str(driver['drill']['record'])

        # Only the in-scope chronic actor qualifies: two corrective
        # completions inside the 90-day cutoff. The single-failure asset,
        # out-of-scope offenders and stale completions are all excluded.
        assert len(drivers) == 1
        assert drivers[0]['source_id'] == seed['chronic_a']
        assert drivers[0]['magnitude'] == 2
        assert 'correlation is not asserted as cause' in payload['disclaimer']
