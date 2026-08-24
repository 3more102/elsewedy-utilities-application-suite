"""Maintenance WHY coverage: MTBF/MTTR and schedule compliance drivers.

``mtbf_hours``/``mttr_hours`` are derived from the scoped forced-outage set,
so their drivers are those outage records. ``schedule_compliance_pct``
drivers come from the canonical late-completion extraction whose joins,
scope and window predicates are identical to the rate's own counts.
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


def _seed_site_and_asset(conn):
    suffix = uuid.uuid4().hex[:8].upper()
    cur = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (f'MW-{suffix}', f'Maint why site {suffix}', 'Greater Cairo',
         'Cairo', 'Electrical Substation', 300))
    site_id = int(cur.lastrowid)
    location_id = int(conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'MWL-{suffix}', f'Maint why bay {suffix}', 'Area',
         site_id)).lastrowid)
    stamp = now()
    asset_id = int(conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                              status,location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (f'AST-MW-{suffix}', f'Maint why asset {suffix}', 'Transformer',
         'Critical', 'Good', 'Operating', location_id, stamp, stamp),
    ).lastrowid)
    return {'suffix': suffix, 'site_id': site_id,
            'location_id': location_id, 'asset_id': asset_id}


def _insert_forced_outage(conn, seed, hours=2):
    user_id = int(conn.execute(
        "SELECT id FROM users WHERE username='omar'").fetchone()[0])
    stamp = now()
    start = datetime.now() - timedelta(days=1)
    conn.execute(
        '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,
                                     status,start_at,end_at,reported_by,
                                     created_at,updated_at)
           VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (f"OUT-MW-{seed['suffix']}", seed['asset_id'], seed['site_id'],
         start.isoformat(timespec='seconds'),
         (start + timedelta(hours=hours)).isoformat(timespec='seconds'),
         user_id, stamp, stamp))


def _insert_work_order(conn, seed, *, title, target_days_ago,
                       actual_days_ago, status='Completed'):
    stamp = now()
    target = (date.today() - timedelta(days=target_days_ago)).isoformat()
    actual = ((date.today() - timedelta(days=actual_days_ago)).isoformat()
              if actual_days_ago is not None else None)
    conn.execute(
        '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                                   asset_id,location_id,target_finish,
                                   actual_finish,actual_hours,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (f"WO-MW-{seed['suffix']}-{title[:6].upper()}", title, 'High', status,
         'Preventive', seed['asset_id'], seed['location_id'], target, actual,
         2.0, stamp, stamp))


def test_mtbf_mttr_explanations_cite_scoped_outage_records():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_site_and_asset(conn)
            _insert_forced_outage(conn, seed)

        for metric in ('mttr_hours', 'mtbf_hours'):
            response = client.get(
                '/api/kpi/explanation',
                headers=headers,
                params={'family': 'maintenance', 'metric': metric,
                        'site_id': seed['site_id'], 'period_days': 30},
            )
            assert response.status_code == 200, (metric, response.text)
            payload = response.json()
            assert payload['drivers'], metric
            with db() as conn:
                for driver in payload['drivers']:
                    row = conn.execute(
                        '''SELECT outage_type,site_id FROM asset_outages
                           WHERE id=?''',
                        (driver['source_id'],)).fetchone()
                    assert row is not None, (metric, driver)
                    assert str(row['outage_type']) == 'Forced'
                    assert int(row['site_id']) == seed['site_id']


def test_schedule_compliance_explains_late_completions():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seed = _seed_site_and_asset(conn)

        late_title = f"LATE-{seed['suffix']}"
        ontime_title = f"ONTIME-{seed['suffix']}"
        open_title = f"OPEN-{seed['suffix']}"
        with db() as conn:
            # Met on time.
            _insert_work_order(conn, seed, title=ontime_title,
                               target_days_ago=3, actual_days_ago=4)
            # Completed but two days late; still inside the window.
            _insert_work_order(conn, seed, title=late_title,
                               target_days_ago=5, actual_days_ago=3)
            # Scheduled inside the window but still unfinished: counted in
            # `scheduled`, not in `met`.
            _insert_work_order(conn, seed, title=open_title,
                               target_days_ago=2, actual_days_ago=None,
                               status='In Progress')

        with db() as conn:
            canonical = compute_maintenance_kpis(
                conn, ExecutiveFilters(period_days=30,
                                       site_id=seed['site_id']))

        response = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={'family': 'maintenance',
                    'metric': 'schedule_compliance_pct',
                    'site_id': seed['site_id'], 'period_days': 30},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        # One of three scheduled jobs met its target; the rate is 33.3% and
        # both shortfall classes must appear as contributors.
        assert payload['value'] == canonical['schedule_compliance_pct']
        assert canonical['schedule_compliance_pct'] == 33.3
        classifications = {}
        for driver in payload['drivers']:
            assert driver['kind'] == 'schedule_shortfall'
            assert driver['attribution'] == 'contributor'
            assert driver['source_type'] == 'work_order'
            classifications[driver['classification']] = driver
        assert set(classifications) == {'late', 'unfinished'}

        titles = set()
        with db() as conn:
            for driver in payload['drivers']:
                row = conn.execute(
                    'SELECT wo_no,title FROM work_orders WHERE id=?',
                    (driver['source_id'],)).fetchone()
                assert row is not None
                titles.add(str(row['title']))
                assert driver['drill']['record'] == str(row['wo_no'])
        assert any(late_title in t for t in titles), titles
        assert any(open_title in t for t in titles), titles
        assert not any(ontime_title in t for t in titles), titles
