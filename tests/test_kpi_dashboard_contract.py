"""Executive-dashboard integration contracts.

These tests pin the exact response shapes the dashboard JavaScript consumes
(``static/app.js`` renders KPI families straight from these payloads and must
never recompute indices client-side) plus source-level guards on the frontend
wiring.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / 'static' / 'app.js'


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_reliability_fixtures(suffix: str, customer_count: int | None):
    anchor = datetime.now()
    with db() as conn:
        site = conn.execute(
            '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
               VALUES(?,?,?,?,?,?)''',
            (
                f'DASH-{suffix}'.upper(),
                f'Dashboard probe site {suffix}',
                'Greater Cairo',
                'Cairo',
                'Electrical Substation',
                customer_count,
            ),
        )
        site_id = int(site.lastrowid)
        stamp = now()
        asset = conn.execute(
            '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)''',
            (
                f'AST-DASH-{suffix.upper()}',
                f'Dashboard probe transformer {suffix}',
                'Transformer',
                'High',
                'Good',
                'Operating',
                stamp,
                stamp,
            ),
        )
        asset_id = int(asset.lastrowid)
        user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
        conn.execute(
            '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,end_at,reported_by,created_at,updated_at)
               VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
            (
                f'OUT-DASH-{uuid.uuid4().hex[:8].upper()}',
                asset_id,
                site_id,
                (anchor - timedelta(days=2, hours=3)).isoformat(timespec='seconds'),
                (anchor - timedelta(days=2, hours=1)).isoformat(timespec='seconds'),
                user_id,
                stamp,
                stamp,
            ),
        )
    return site_id, asset_id


def test_reliability_payload_exposes_dashboard_rendering_contract():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        _seed_reliability_fixtures(suffix, 1000)

        response = client.get(
            '/api/kpis/reliability', headers=headers, params={'period_days': 30}
        )
        assert response.status_code == 200
        payload = response.json()

        # Card rendering inputs.
        for kpi_id in ('saifi', 'saidi', 'caidi', 'asai'):
            kpi = payload['kpis'][kpi_id]
            for key in ('id', 'value', 'previous_value', 'change_pct', 'unit',
                        'direction', 'definition', 'formula', 'missing_inputs'):
                assert key in kpi, (kpi_id, key)
        # Contributor rows feed the ranked table and drill-down modal.
        contributors = payload['contributors']
        assert contributors, 'expected at least one contributor'
        contributor = next(
            c for c in contributors if c['asset_no'] == f'AST-DASH-{suffix}'.upper()
        )
        for key in ('outage_no', 'asset_id', 'asset_no', 'asset_name',
                    'duration_hours', 'customer_hours', 'share_pct'):
            assert key in contributor, key
        assert contributor['customer_hours'] == 2000.0
        # Header strip inputs.
        assert 'ongoing_outages' in payload['counts']
        assert payload['window_start'] and payload['window_end']


def test_missing_customer_count_flags_drive_unavailable_ui_state():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        _seed_reliability_fixtures(suffix, None)

        response = client.get(
            '/api/kpis/reliability',
            headers=headers,
            params={'site_id': None},
        )
        # Portfolio view may include other declared sites; query our site only.
        with db() as conn:
            site_id = int(conn.execute(
                "SELECT id FROM sites WHERE site_code=?",
                (f'DASH-{suffix}'.upper(),),
            ).fetchone()[0])
        response = client.get(
            '/api/kpis/reliability', headers=headers, params={'site_id': site_id}
        )
        assert response.status_code == 200
        payload = response.json()
        for kpi_id in ('saifi', 'saidi', 'caidi', 'asai'):
            kpi = payload['kpis'][kpi_id]
            assert kpi['value'] is None
            assert kpi['missing_inputs'] == ['sites.customer_count']
        # Contributors stay visible with an explicit exclusion reason so the
        # dashboard can explain WHY the indices are unavailable.
        assert payload['contributors']
        assert payload['contributors'][0]['excluded_reason'] == 'site has no customer_count'


def test_inventory_payload_exposes_drilldown_contract():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            from tests.test_inventory_kpis import _seed_item

            item_id = _seed_item(
                conn, suffix + '-unc',
                current_stock=1, reorder_point=5, unit_price=40,
            )

        response = client.get('/api/kpis/inventory', headers=headers)
        assert response.status_code == 200
        payload = response.json()
        for kpi_id in ('stock_availability_pct', 'stockout_lines',
                       'uncovered_reorder_lines', 'slow_moving_value_pct',
                       'open_po_aging_days_avg'):
            assert kpi_id in payload['kpis']
        contributor = next(
            (c for c in payload['contributors'] if c.get('item_id') == item_id),
            None,
        )
        assert contributor is not None
        for key in ('item_id', 'item_no', 'name', 'available',
                    'reorder_point', 'exposure_value', 'on_order'):
            assert key in contributor, key


def test_app_js_consumes_backend_families_without_client_recomputation():
    source = APP_JS.read_text(encoding='utf-8')
    # The dashboard must call the real families...
    assert '/api/kpis/reliability' in source
    assert '/api/kpis/inventory' in source
    assert '/api/kpis/maintenance' in source
    assert '/api/kpis/workforce' in source
    assert '/api/sites/' in source
    # ...expose drill-downs backed by contributor data...
    for handler in ('kpiContributorDetail', 'kpiItemDetail', 'kpiWoDetail', 'kpiWfDetail',
                    'openCustomerCounts', 'saveCustomerCount',
                    'reliabilityPanel', 'inventoryPanel', 'maintenancePanel',
                    'workforcePanel', 'materialBlockedPanel'):
        assert handler in source, handler
    # ...surface the safe KPI-to-action navigation bridges...
    for action in ('data-action-dispatch', 'data-action-procurement',
                   'data-action-inventory', 'material_blocked_work'):
        assert action in source, action
    # ...and never recompute business formulas in the browser.
    lowered = source.lower()
    for forbidden in ('saidi=', 'saifi=', 'caidi=', 'function computesaidi',
                      'function computemtb', 'function computeavailability',
                      'utilisation_pct_30d=', 'utilization_pct_30d='):
        assert forbidden not in lowered, forbidden


def test_material_blocked_dashboard_payload_supports_action_bridge():
    """The material-blocked panel consumes /api/dashboard fields directly."""
    with TestClient(app) as client:
        headers = _auth(client)
        response = client.get('/api/dashboard', headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert 'material_blocked_work' in payload
        assert 'material_blocked_work_orders' in payload['kpis']
        assert isinstance(payload['material_blocked_work'], list)
        for entry in payload['material_blocked_work']:
            # Drill-down needs the real record id; ranking needs priority.
            for key in ('id', 'wo_no', 'title', 'priority', 'status', 'shortage_items'):
                assert key in entry, key


def test_admin_customer_count_endpoint_stays_audited_for_dashboard():
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            site = conn.execute(
                '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
                   VALUES(?,?,?,?,?,NULL)''',
                (f'DASHCC-{suffix}'.upper(), f'CC probe {suffix}', 'Greater Cairo', 'Cairo', 'Operations Centre'),
            )
            site_id = int(site.lastrowid)

        viewer = client.post('/api/auth/login', json={'username': 'exec', 'password': 'Viewer@2026'})
        assert viewer.status_code == 200
        denied = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers={'Authorization': f"Bearer {viewer.json()['token']}"},
            json={'customer_count': 750},
        )
        assert denied.status_code == 403

        admin = _auth(client)
        allowed = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers=admin,
            json={'customer_count': 750},
        )
        assert allowed.status_code == 200

        with db() as conn:
            audits = conn.execute(
                """SELECT old_value,new_value FROM audit_logs
                   WHERE module='Sites' AND record_id=? AND action='UPDATE'""",
                (f'DASHCC-{suffix}'.upper(),),
            ).fetchall()
        assert len(audits) == 1
        assert 'null' in audits[0]['old_value'] and '750' in audits[0]['new_value']
