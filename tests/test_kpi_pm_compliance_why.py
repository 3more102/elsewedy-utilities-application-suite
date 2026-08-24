"""PM compliance WHY regressions: overdue plans as scoped contributors.

``pm_compliance_pct`` must be explainable through the canonical overdue-plan
extraction, whose joins, scope and predicates are identical to the metric's
own overdue count. Contributors carry ``contributor`` attribution and resolve
to real maintenance plan rows inside the requested scope.
"""
from __future__ import annotations

import uuid

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


def _seed_pm_plans(conn):
    suffix = uuid.uuid4().hex[:8].upper()
    sites = {}
    for key, region in (('a', 'Region Alpha'), ('b', 'Region Beta')):
        cur = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (f'PM-{key}-{suffix}', f'PM site {key} {suffix}', region,
             'Cairo', 'Electrical Substation', 400))
        site_id = int(cur.lastrowid)
        location_id = int(conn.execute(
            '''INSERT INTO locations(location_code,name,location_type,site_id)
               VALUES(?,?,?,?)''',
            (f'PML-{key}-{suffix}', f'PM bay {key} {suffix}', 'Area',
             site_id)).lastrowid)
        sites[key] = {'site_id': site_id, 'location_id': location_id}

    stamp = now()
    counter = {'asset': 0, 'plan': 0}

    def asset(site_key):
        counter['asset'] += 1
        return int(conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                  status,location_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'AST-PM{counter["asset"]}-{suffix}',
             f'PM probe asset {counter["asset"]} {suffix}', 'Transformer',
             'Critical', 'Good', 'Operating', sites[site_key]['location_id'],
             stamp, stamp)).lastrowid)

    def plan(asset_id, site_key, *, priority, due_in_days, active=True,
             trigger='Calendar'):
        counter['plan'] += 1
        from datetime import date, timedelta
        next_due = ((date.today() + timedelta(days=due_in_days)).isoformat()
                    if due_in_days is not None else None)
        cur = conn.execute(
            '''INSERT INTO maintenance_plans(pm_no,name,asset_id,trigger_type,
                                             interval_days,next_due,priority,
                                             active)
               VALUES(?,?,?,?,?,?,?,?)''',
            (f'PM-WHY-{suffix}-{counter["plan"]:03d}',
             f'PM why probe {counter["plan"]} {suffix}', asset_id, trigger,
             30, next_due if due_in_days is not None else None, priority,
             1 if active else 0))
        return int(cur.lastrowid)

    overdue_critical_a = plan(asset('a'), 'a', priority='Critical',
                              due_in_days=-5)
    plan(asset('a'), 'a', priority='Low', due_in_days=10)          # future: compliant side
    meter_overdue_a = plan(asset('a'), 'a', priority='Emergency',
                           due_in_days=None, trigger='Meter')      # non-calendar
    inactive_overdue_a = plan(asset('a'), 'a', priority='High',
                              due_in_days=-20, active=False)       # inactive
    overdue_out_of_scope_b = plan(asset('b'), 'b', priority='High',
                                  due_in_days=-3)

    return {
        'site_a': sites['a']['site_id'],
        'overdue_critical_a': overdue_critical_a,
        'meter_overdue_a': meter_overdue_a,
        'inactive_overdue_a': inactive_overdue_a,
        'overdue_out_of_scope_b': overdue_out_of_scope_b,
    }


def test_pm_compliance_explains_scoped_overdue_plans():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_pm_plans(conn)

        with db() as conn:
            canonical = compute_maintenance_kpis(
                conn, ExecutiveFilters(period_days=30, site_id=seed['site_a']))

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'maintenance', 'metric': 'pm_compliance_pct',
                    'site_id': seed['site_a'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        # Value parity with the canonical rate.
        assert payload['value'] == canonical['pm_compliance_pct']

        # Site A holds three active plans (one calendar-overdue), so the
        # scoped rate is deterministic; only that plan may appear as a
        # contributor. Meter-triggered, inactive and out-of-scope plans are
        # all excluded by the metric's own predicates.
        assert canonical['pm_compliance_pct'] == 66.7
        drivers = payload['drivers']
        assert len(drivers) == 1
        driver = drivers[0]
        assert driver['kind'] == 'overdue_pm'
        assert driver['attribution'] == 'contributor'
        assert driver['source_type'] == 'pm_plan'
        assert driver['source_id'] == seed['overdue_critical_a']
        assert driver['magnitude'] >= 1
        assert driver['drill']['module'] == 'maintenance'

        # The drill target resolves to a real plan row.
        with db() as conn:
            row = conn.execute(
                'SELECT pm_no FROM maintenance_plans WHERE id=?',
                (driver['source_id'],)).fetchone()
            assert row is not None
            assert str(row['pm_no']) == str(driver['drill']['record'])
