from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_store import compute_maintenance_kpis
from app.main import app


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_wo(
    conn,
    *,
    priority: str = 'Medium',
    status: str = 'Approved',
    estimated_hours: float = 4.0,
    actual_hours: float = 0.0,
    target_finish: str | None = None,
    created_days_ago: int = 1,
    actual_finish: str | None = None,
    work_type: str = 'Corrective Maintenance',
) -> int:
    stamp = (datetime.now() - timedelta(days=created_days_ago)).isoformat(timespec='seconds')
    created = conn.execute(
        '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,estimated_hours,
                                   actual_hours,target_finish,actual_finish,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (
            f'WO-KPI-{uuid.uuid4().hex[:10].upper()}',
            'Maintenance KPI probe',
            priority,
            status,
            work_type,
            estimated_hours,
            actual_hours,
            target_finish,
            actual_finish,
            stamp,
            now(),
        ),
    )
    return int(created.lastrowid)


def _seed_sla(conn, wo_id: int, resolution_status: str) -> None:
    policy = conn.execute('SELECT id FROM sla_policies ORDER BY id LIMIT 1').fetchone()
    stamp = now()
    conn.execute(
        '''INSERT INTO work_order_sla(work_order_id,policy_id,response_due,resolution_due,
                                      response_status,resolution_status,updated_at)
           VALUES(?,?,?,?,?,?,?)''',
        (
            wo_id,
            int(policy['id']),
            stamp,
            stamp,
            resolution_status,
            resolution_status,
            stamp,
        ),
    )


def test_maintenance_kpi_math_is_exact():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:6]
        with db() as conn:
            # Dedicated site/asset scope: other suites' work orders cannot
            # leak into a fully scoped computation.
            site = conn.execute(
                '''INSERT INTO sites(site_code,name,region,city,site_type)
                   VALUES(?,?,?,?,?)''',
                (f'MKP-{suffix}'.upper(), f'Maint KPI site {suffix}',
                 'Greater Cairo', 'Cairo', 'Electrical Substation'),
            )
            site_id = int(site.lastrowid)
            location = conn.execute(
                '''INSERT INTO locations(location_code,name,location_type,site_id)
                   VALUES(?,?,?,?)''',
                (f'LMKP-{suffix}'.upper(), f'Maint KPI bay {suffix}', 'Area',
                 int(site.lastrowid)),
            )
            stamp = now()
            asset = conn.execute(
                '''INSERT INTO assets(asset_no,name,category,criticality,condition,
                                      status,location_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)''',
                (f'AST-MKP-{suffix.upper()}', f'Maint KPI asset {suffix}',
                 'Transformer', 'Critical', 'Good', 'Operating',
                 int(location.lastrowid), stamp, stamp),
            )
            asset_id = int(asset.lastrowid)

            def _seed_scoped_wo(**kwargs):
                wo_id = _seed_wo(conn, **kwargs)
                conn.execute('UPDATE work_orders SET asset_id=? WHERE id=?',
                             (asset_id, wo_id))
                return wo_id

            _seed_scoped_wo(priority='Medium', estimated_hours=8, created_days_ago=3)
            overdue_emergency = _seed_scoped_wo(
                priority='Emergency', estimated_hours=6,
                target_finish=(date.today() - timedelta(days=5)).isoformat(),
                created_days_ago=20,
            )
            overdue_critical = _seed_scoped_wo(
                priority='Critical', estimated_hours=2,
                target_finish=(date.today() - timedelta(days=2)).isoformat(),
                created_days_ago=10,
            )
            # Completed within the window feeds weekly capacity.
            _seed_scoped_wo(status='Completed', actual_hours=30,
                            actual_finish=now(), created_days_ago=7)

        with db() as conn:
            result = compute_maintenance_kpis(conn, period_days=28, site_id=site_id)

        kpis = result['kpis']
        # Scoped computation sees exactly the four probe work orders.
        assert kpis['open_work_orders']['value'] == 3.0
        assert kpis['emergency_work_orders']['value'] == 1.0
        assert kpis['high_criticality_backlog']['value'] == 2.0
        assert kpis['backlog_hours']['value'] == 16.0

        contributors = {c['wo_no']: c for c in result['contributors']}
        assert len(result['contributors']) == 2  # exactly the two overdue probes
        emergency_row = next(c for c in result['contributors']
                             if c['priority'] == 'Emergency')
        assert emergency_row['days_overdue'] == 5
        with db() as conn:
            probe_wos = {
                conn.execute('SELECT wo_no FROM work_orders WHERE id=?', (i,)).fetchone()[0]
                for i in (overdue_emergency, overdue_critical)
            }
        assert set(contributors) == probe_wos
        # Risk-weighted ranking: emergency (25 weight) outranks critical (8).
        priorities = [c['priority'] for c in result['contributors']]
        assert priorities.index('Emergency') < priorities.index('Critical')

        assert result['weekly_completion_hours'] == 30 / 4
        assert result['resolved_in_window'] == 1


def test_completion_sla_pct_contract():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:6]
        with db() as conn:
            met = _seed_wo(conn, status='Completed', actual_hours=2,
                           actual_finish=now(), created_days_ago=4)
            breached = _seed_wo(conn, status='Completed', actual_hours=9,
                                actual_finish=now(), created_days_ago=4)
            _seed_sla(conn, met, 'Met')
            _seed_sla(conn, breached, 'Breached')

        with db() as conn:
            result = compute_maintenance_kpis(conn, period_days=28)

        sla = result['kpis']['completion_sla_pct']
        # Shared suite data may add resolutions; the index must stay a valid
        # percentage while both outcomes are present.
        assert sla['value'] is not None
        assert 0 < sla['value'] <= 100
        assert result['resolved_in_window'] >= 2


def test_pm_compliance_matches_existing_definition():
    with TestClient(app):
        with db() as conn:
            total = int(conn.execute(
                'SELECT COUNT(*) FROM maintenance_plans WHERE active=1'
            ).fetchone()[0])
            over = int(conn.execute(
                """SELECT COUNT(*) FROM maintenance_plans WHERE active=1
                   AND trigger_type='Calendar' AND next_due IS NOT NULL
                   AND next_due<?""",
                (date.today().isoformat(),),
            ).fetchone()[0])
            expected = round(100 * (total - over) / total, 2) if total else 100.0

            result = compute_maintenance_kpis(conn, period_days=90)
        assert result['kpis']['pm_compliance_pct']['value'] == expected


def test_maintenance_kpi_api_auth_bounds_and_export():
    with TestClient(app) as client:
        anonymous = client.get('/api/kpis/maintenance')
        assert anonymous.status_code in (401, 403)

        headers = _auth(client)
        ok = client.get(
            '/api/kpis/maintenance', headers=headers, params={'period_days': 60}
        )
        assert ok.status_code == 200, ok.text
        payload = ok.json()
        assert payload['kpi_family'] == 'maintenance_execution'
        assert set(payload['kpis']) == {
            'open_work_orders', 'overdue_work_orders', 'emergency_work_orders',
            'high_criticality_backlog', 'backlog_hours', 'wo_aging_days_avg',
            'pm_compliance_pct', 'completion_sla_pct',
        }

        bounds = client.get(
            '/api/kpis/maintenance', headers=headers, params={'period_days': 10}
        )
        assert bounds.status_code == 422

        export = client.get('/api/kpis/maintenance.csv', headers=headers)
        assert export.status_code == 200
        assert 'text/csv' in export.headers.get('content-type', '')
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        assert len(rows) == 9 and rows[0][:3] == ['KPI', 'Name', 'Value']
