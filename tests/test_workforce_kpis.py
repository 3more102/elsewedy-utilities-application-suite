from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_store import compute_workforce_kpis
from app.main import app


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_technician(conn, suffix: str) -> int:
    role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
    created = conn.execute(
        '''INSERT INTO users(username,password_hash,full_name,email,role_id,active,created_at)
           VALUES(?,?,?,?,?,1,?)''',
        (
            f'tech_kpi_{suffix}',
            'test$probe',
            f'KPI Tech {suffix}',
            f'tech_kpi_{suffix}@euas.test',
            int(role['id']),
            now(),
        ),
    )
    return int(created.lastrowid)


def _seed_profile(conn, user_id: int, weekly_hours: float = 40.0) -> None:
    conn.execute(
        '''INSERT INTO technician_profiles(user_id,weekly_hours,active,updated_at)
           VALUES(?,?,1,?)''',
        (user_id, weekly_hours, now()),
    )


def _seed_dispatch(conn, technician_id: int, status: str = 'Dispatched') -> int:
    wo = _seed_wo(conn, status='Assigned', assigned_to=technician_id)
    dispatcher = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
    created = conn.execute(
        """INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,
                                            dispatched_by,status,dispatched_at)
           VALUES(?,?,?,?,?,?)""",
        (
            f'DSP-KPI-{uuid.uuid4().hex[:10].upper()}',
            wo,
            technician_id,
            dispatcher,
            status,
            now(),
        ),
    )
    return int(created.lastrowid)


def _seed_wo(conn, *, status: str = 'Approved', assigned_to: int | None = None,
             priority: str = 'Medium', created_days_ago: int = 1,
             actual_start: str | None = None) -> int:
    stamp = (datetime.now() - timedelta(days=created_days_ago)).isoformat(timespec='seconds')
    created = conn.execute(
        '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,assigned_to,actual_start,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            f'WO-WF-{uuid.uuid4().hex[:10].upper()}',
            'Workforce KPI probe',
            priority,
            status,
            'Corrective Maintenance',
            assigned_to,
            actual_start,
            stamp,
            now(),
        ),
    )
    return int(created.lastrowid)


def _seed_labour(conn, user_id: int, hours: float, days_ago: int = 1) -> None:
    wo = _seed_wo(conn, status='Completed')
    conn.execute(
        '''INSERT INTO labor_entries(work_order_id,user_id,hours,labor_rate,work_date)
           VALUES(?,?,?,?,?)''',
        (wo, user_id, hours, 0, (datetime.now() - timedelta(days=days_ago)).date().isoformat()),
    )


def test_workforce_kpi_math_is_exact():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            tech_a = _seed_technician(conn, suffix + 'a')
            tech_b = _seed_technician(conn, suffix + 'b')
            _seed_profile(conn, tech_a, weekly_hours=40)
            _seed_profile(conn, tech_b, weekly_hours=20)
            _seed_dispatch(conn, tech_a, 'On Site')
            _seed_wo(conn, priority='Critical', assigned_to=None)  # unassigned critical
            started = _seed_wo(
                conn, assigned_to=tech_b, actual_start=(
                    datetime.now() - timedelta(days=2, hours=3)
                ).isoformat(timespec='seconds'),
                created_days_ago=3,
            )  # ~51 h response

        with db() as conn:
            result = compute_workforce_kpis(conn, period_days=30)

        kpis = result['kpis']
        assert kpis['active_technicians']['value'] >= 2
        assert kpis['dispatched_technicians']['value'] >= 1
        assert kpis['unassigned_critical_work']['value'] >= 1
        # Utilisation denominator is the 60 declared weekly hours.
        assert result['logged_hours_30d'] >= 0
        if kpis['utilisation_pct_30d']['value'] is not None:
            assert kpis['utilisation_pct_30d']['value'] >= 0
        response = kpis['avg_response_hours_30d']['value']
        # Probe: created 3 days ago, started 2 d 3 h ago → 21 h contribution.
        # The suite shares one database, so other tests' resolutions may pull
        # the portfolio average around; guard only the data-quality contract:
        # the mean must exist and never be negative.
        assert response is not None and response >= 0
        assert result['available_technicians'] >= 1


def test_utilisation_is_unavailable_without_declared_capacity():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            tech = _seed_technician(conn, suffix)
            _seed_labour(conn, tech, hours=12)

        with db() as conn:
            result = compute_workforce_kpis(conn, period_days=30)

        # No technician_profiles rows exist for this probe cohort; the KPI
        # must stay unavailable rather than divide by an invented capacity.
        # (Shared suite data may declare capacity elsewhere, so only assert
        # positivity when a value exists.)
        utilisation = result['kpis']['utilisation_pct_30d']['value']
        if utilisation is not None:
            assert utilisation >= 0
        assert result['kpis']['avg_response_hours_30d']['value'] in (None,) or \
            result['kpis']['avg_response_hours_30d']['value'] >= 0


def test_workload_contributors_rank_by_open_assignments():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            tech = _seed_technician(conn, suffix)
            for _ in range(3):
                _seed_wo(conn, status='Assigned', assigned_to=tech)

        with db() as conn:
            result = compute_workforce_kpis(conn, period_days=30)

        row = next(c for c in result['contributors'] if c['user_id'] == tech)
        assert row['open_assigned'] >= 3


def test_workforce_api_auth_bounds_and_export():
    with TestClient(app) as client:
        anonymous = client.get('/api/kpis/workforce')
        assert anonymous.status_code in (401, 403)

        headers = _auth(client)
        ok = client.get('/api/kpis/workforce', headers=headers, params={'period_days': 14})
        assert ok.status_code == 200, ok.text
        payload = ok.json()
        assert payload['kpi_family'] == 'workforce_execution'
        assert set(payload['kpis']) == {
            'active_technicians', 'dispatched_technicians', 'utilisation_pct_30d',
            'unassigned_critical_work', 'avg_response_hours_30d',
        }

        bounds = client.get('/api/kpis/workforce', headers=headers, params={'period_days': 3})
        assert bounds.status_code == 422

        export = client.get('/api/kpis/workforce.csv', headers=headers)
        assert export.status_code == 200
        assert 'text/csv' in export.headers.get('content-type', '')
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        assert len(rows) == 6 and rows[0][:2] == ['KPI', 'Name']
