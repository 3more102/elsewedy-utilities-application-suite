from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.kpi_store import compute_reliability_kpis
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_site(conn, suffix: str, customer_count: int | None) -> int:
    created = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (
            f'KPI-{suffix}'.upper(),
            f'KPI probe site {suffix}',
            'Greater Cairo',
            'Cairo',
            'Electrical Substation',
            customer_count,
        ),
    )
    return int(created.lastrowid)


def _seed_asset(conn, suffix: str) -> int:
    stamp = now()
    created = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)''',
        (
            f'AST-KPI-{suffix.upper()}',
            f'KPI probe transformer {suffix}',
            'Transformer',
            'High',
            'Good',
            'Operating',
            stamp,
            stamp,
        ),
    )
    return int(created.lastrowid)


def _seed_outage(conn, site_id: int, asset_id: int, start: datetime, end: datetime | None) -> int:
    user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    stamp = now()
    created = conn.execute(
        '''INSERT INTO asset_outages(
             outage_no,asset_id,site_id,outage_type,status,start_at,end_at,
             reported_by,created_at,updated_at
           ) VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (
            f'OUT-KPI-{uuid.uuid4().hex[:10].upper()}',
            asset_id,
            site_id,
            start.isoformat(timespec='seconds'),
            end.isoformat(timespec='seconds') if end else None,
            int(user['id']),
            stamp,
            stamp,
        ),
    )
    return int(created.lastrowid)


def test_saifi_saidi_caidi_asai_math_is_exact():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        # Anchor outages relative to now: the KPI window always ends at the
        # current instant, so absolute dates would be fragile.
        anchor = datetime.now()
        with db() as conn:
            site_id = _seed_site(conn, suffix, 1000)
            asset_id = _seed_asset(conn, suffix)
            # Two sustained interruptions in the current 30-day window.
            _seed_outage(
                conn, site_id, asset_id,
                anchor - timedelta(days=2, hours=3),
                anchor - timedelta(days=2, hours=1),
            )  # 2.0 h
            _seed_outage(
                conn, site_id, asset_id,
                anchor - timedelta(days=1, hours=2),
                anchor - timedelta(days=1),
            )  # 2.0 h

        result = None
        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days=30)

        assert result['customers_served'] == 1000
        saifi = result['kpis']['saifi']
        saidi = result['kpis']['saidi']
        caidi = result['kpis']['caidi']
        asai = result['kpis']['asai']
        # SAIFI = (1000 + 1000) / 1000 = 2 interruptions/customer.
        assert abs(saifi['value'] - 2.0) < 1e-6
        # SAIDI = (2000 + 2000 customer-hours) / 1000 = 4 h/customer.
        assert abs(saidi['value'] - 4.0) < 1e-6
        # CAIDI = SAIDI / SAIFI = 2 h/interruption.
        assert abs(caidi['value'] - 2.0) < 1e-6
        # ASAI over a 30-day window: (720 - 4)/720 x 100.
        expected_asai = 100.0 * (30 * 24 - 4.0) / (30 * 24)
        assert abs(asai['value'] - expected_asai) < 0.01
        assert result['counts']['sustained_interruptions'] == 2

        # Contributors are ranked by customer impact and link the records.
        top = result['contributors'][0]
        assert top['customer_hours'] == 2000.0
        assert top['share_pct'] == 50.0
        assert top['outage_no']


def test_missing_customer_count_never_fabricates_indices():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id = _seed_site(conn, suffix, None)
            asset_id = _seed_asset(conn, suffix)
            _seed_outage(
                conn, site_id, asset_id,
                anchor - timedelta(hours=4),
                anchor - timedelta(hours=2),
            )

        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days=30)

        for kpi_id in ('saifi', 'saidi', 'caidi', 'asai'):
            kpi = result['kpis'][kpi_id]
            assert kpi['value'] is None
            assert 'sites.customer_count' in kpi['missing_inputs']
        # Raw counts remain computable and explainable without inventing data.
        assert result['counts']['sustained_interruptions'] == 0
        assert result['counts']['unattributed_interruptions'] == 1
        contributor = result['contributors'][0]
        assert contributor['excluded_reason'] == 'site has no customer_count'


def test_previous_period_comparison_and_change_direction():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id = _seed_site(conn, suffix, 500)
            asset_id = _seed_asset(conn, suffix)
            # Previous 30-day window only: days 31..60 before anchor.
            _seed_outage(
                conn, site_id, asset_id,
                anchor - timedelta(days=40, hours=6),
                anchor - timedelta(days=40),
            )  # 6.0 h -> prev SAIDI = 6*500/500 = 6

        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days=30)

        saidi = result['kpis']['saidi']
        assert saidi['value'] == 0.0
        assert saidi['previous_value'] == 6.0
        assert saidi['change_pct'] == -100.0
        saifi = result['kpis']['saifi']
        assert saifi['previous_value'] == 1.0
        assert saifi['value'] == 0.0


def test_ongoing_and_planned_outages_are_reported_not_indexed():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id = _seed_site(conn, suffix, 800)
            asset_id = _seed_asset(conn, suffix)
            user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
            stamp = now()
            # Ongoing forced outage (no end_at yet): excluded from sustained indices.
            conn.execute(
                '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,end_at,reported_by,created_at,updated_at)
                   VALUES(?,?,?,'Forced','Open',?,NULL,?,?,?)''',
                (f'OUT-KPI-{uuid.uuid4().hex[:10].upper()}', asset_id, site_id,
                 (anchor - timedelta(hours=6)).isoformat(timespec='seconds'), user_id, stamp, stamp),
            )
            # Planned outage inside the window: never an interruption event.
            conn.execute(
                '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,end_at,reported_by,created_at,updated_at)
                   VALUES(?,?,?,'Planned','Closed',?,?,?,?,?)''',
                (f'OUT-KPI-{uuid.uuid4().hex[:10].upper()}', asset_id, site_id,
                 (anchor - timedelta(days=2)).isoformat(timespec='seconds'),
                 (anchor - timedelta(days=2) + timedelta(hours=4)).isoformat(timespec='seconds'),
                 user_id, stamp, stamp),
            )

        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days=30)

        assert result['counts']['ongoing_outages'] == 1
        assert len(result['ongoing_outages']) == 1
        assert result['counts']['planned_outages_in_window'] == 1
        assert result['counts']['sustained_interruptions'] == 0
        assert result['kpis']['saifi']['value'] == 0.0
        assert result['kpis']['saidi']['value'] == 0.0


def test_kpi_api_requires_authentication_and_supports_export():
    with TestClient(app) as client:
        anonymous = client.get('/api/kpis/reliability')
        assert anonymous.status_code in (401, 403)

        headers = _auth(client)
        response = client.get(
            '/api/kpis/reliability', headers=headers, params={'period_days': 90}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['kpi_family'] == 'utility_reliability'
        assert set(payload['kpis']) == {'saifi', 'saidi', 'caidi', 'asai'}
        for kpi in payload['kpis'].values():
            assert kpi['definition'] and kpi['formula'] and kpi['unit']

        export = client.get('/api/kpis/reliability.csv', headers=headers)
        assert export.status_code == 200
        assert 'text/csv' in export.headers.get('content-type', '')
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        assert rows[0][:3] == ['KPI', 'Name', 'Value']
        assert {row[0] for row in rows[1:]} == {'saifi', 'saidi', 'caidi', 'asai'}


def test_customer_count_update_is_admin_only_and_audited():
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            site_id = _seed_site(conn, suffix, None)

        # A non-admin (executive viewer) must not reshape regulatory inputs.
        headers = _auth(client, 'exec', 'Viewer@2026')
        denied = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers=headers,
            json={'customer_count': 1200},
        )
        assert denied.status_code == 403

        admin_headers = _auth(client, 'omar')
        allowed = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers=admin_headers,
            json={'customer_count': 1200},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()['customer_count'] == 1200

        invalid = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers=admin_headers,
            json={'customer_count': -5},
        )
        assert invalid.status_code == 422

        with db() as conn:
            audit_rows = conn.execute(
                """SELECT old_value,new_value FROM audit_logs
                   WHERE module='Sites' AND action='UPDATE' AND record_id=?""",
                (f'KPI-{suffix}'.upper(),),
            ).fetchall()
        assert len(audit_rows) == 1
        assert "'customer_count'" in audit_rows[0]['old_value']
        assert '1200' in audit_rows[0]['new_value']

        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days=365)
        assert result['customers_served'] == 1200
