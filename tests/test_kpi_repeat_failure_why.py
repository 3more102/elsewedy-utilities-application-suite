"""Repeat-failure WHY regressions: chronic bad actors as contributors.

The ``repeat_failure_rate_pct`` metric must be explainable through the
canonical chronic bad-actor extraction (assets with >= 2 corrective
completions in 90 days, executive-scoped). Contributors are the literal
records composing the numerator, so they carry ``contributor`` attribution
and resolve to real asset rows inside the requested scope.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

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
    locations = {}
    for key, region in (('a', 'Region Alpha'), ('b', 'Region Beta')):
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'RF-{key}-{suffix}', f'Repeat site {key} {suffix}', region,
             'Cairo', 'Electrical Substation', 500))
        site_id = int(cur.lastrowid)
        loc = conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'RFL-{key}-{suffix}', f'Repeat bay {key} {suffix}', 'Area',
             site_id))
        locations[key] = int(loc.lastrowid)
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
        'cbm_only': asset('CBM', 'a'),
    }

    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])
    stamp = now()
    counter = {'n': 0}

    def wo(asset_id, *, finished_days_ago, site_key='a', with_location=True):
        counter['n'] += 1
        finished = (datetime.now() - timedelta(days=finished_days_ago)).isoformat(timespec='seconds')
        conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                                       asset_id,location_id,actual_finish,
                                       actual_hours,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'WO-RF-{suffix}-{counter["n"]:03d}',
             f'Repeat failure probe {counter["n"]} {suffix}',
             'High', 'Completed', 'Corrective', asset_id,
             locations[site_key] if with_location else None,
             finished, 3.0, stamp, stamp),
        )

    for _ in range(2):
        wo(assets['chronic_a'], finished_days_ago=10)
    wo(assets['single_a'], finished_days_ago=12)
    for _ in range(5):
        wo(assets['worse_b'], finished_days_ago=8, site_key='b')
    # Outside the 90-day cutoff: must not count toward the chronic actor.
    wo(assets['chronic_a'], finished_days_ago=100)
    # CBM-style corrective completions carry an asset but no work-order
    # location: they are outside every scoped rate and must therefore never
    # surface as scoped contributors.
    for _ in range(2):
        wo(assets['cbm_only'], finished_days_ago=5, with_location=False)

    return {
        'site_a': sites['a'],
        'chronic_a': assets['chronic_a'],
        'cbm_only': assets['cbm_only'],
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
        # completions inside the 90-day cutoff, counted through the same
        # work-order-location scope as the rate. The single-failure asset,
        # out-of-scope offenders, stale completions and location-less CBM
        # completions are all excluded.
        assert len(drivers) == 1
        assert drivers[0]['source_id'] == seed['chronic_a']
        assert drivers[0]['magnitude'] == 2
        assert 'correlation is not asserted as cause' in payload['disclaimer']


def test_location_less_corrective_work_never_becomes_scoped_contributor():
    """Contributor scoping must match the displayed rate's scoping.

    Corrective work orders created from condition monitoring carry an asset
    but no ``work_orders.location_id``. Under a site-scoped view they are
    excluded from ``repeat_failure_rate_pct``; their asset must therefore be
    excluded from the chronic bad-actor driver list as well — otherwise a
    zero-rate dimension could still report contributors.
    """
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_repeat_failures(conn)

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'maintenance',
                    'metric': 'repeat_failure_rate_pct',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        # The CBM-only repeat offender is invisible to the located rate and
        # must stay invisible to the driver list; only the located chronic
        # actor qualifies.
        source_ids = {driver['source_id'] for driver in payload['drivers']}
        assert seed['cbm_only'] not in source_ids
        assert source_ids == {seed['chronic_a']}

        # Invariant: every reported contributor holds at least two corrective
        # completions that the scoped rate itself can see.
        with db() as conn:
            for source_id in source_ids:
                located = conn.execute(
                    '''SELECT COUNT(*) FROM work_orders w
                       JOIN locations l ON l.id=w.location_id
                       JOIN sites s ON s.id=l.site_id
                       WHERE w.asset_id=?
                         AND w.status IN ('Completed','Closed')
                         AND w.work_type LIKE 'Corrective%'
                         AND COALESCE(w.actual_finish,w.created_at)>=?
                         AND s.id=?''',
                    (source_id,
                     (date.today() - timedelta(days=90)).isoformat(),
                     seed['site_a'])).fetchone()[0]
                assert located >= 2, (source_id, located)
