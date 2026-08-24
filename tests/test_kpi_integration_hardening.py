"""PR #135 integration hardening contracts.

Pins the guarantees the executive dashboard depends on when integrated
against current main: single route installation, read/mutation authorization
separation, and real filter propagation into the KPI families that accept it.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_every_api_route_installs_exactly_once():
    pairs = []
    for route in app.routes:
        path = str(getattr(route, 'path', '') or '')
        if not path.startswith('/api/'):
            continue
        for method in set(getattr(route, 'methods', set()) or set()):
            method = str(method).upper()
            if method in ('HEAD', 'OPTIONS'):
                continue
            pairs.append((path, method))
    duplicates = {pair: count for pair, count in Counter(pairs).items() if count > 1}
    assert duplicates == {}, duplicates

    # The dashboard-consumed surfaces must all be present exactly once.
    expected = {
        ('/api/kpis/reliability', 'GET'),
        ('/api/kpis/inventory', 'GET'),
        ('/api/kpis/maintenance', 'GET'),
        ('/api/kpis/workforce', 'GET'),
        ('/api/sites/{site_id}/customer-count', 'PATCH'),
    }
    assert expected <= set(pairs)


def test_analytics_only_role_can_read_all_families_but_cannot_mutate():
    with TestClient(app) as client:
        headers = _auth(client, 'exec', 'Viewer@2026')
        for path in ('/api/kpis/reliability', '/api/kpis/inventory',
                     '/api/kpis/maintenance', '/api/kpis/workforce'):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)

        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            site = conn.execute(
                '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
                   VALUES(?,?,?,?,?,NULL)''',
                (f'HARD-{suffix}'.upper(), f'Hardening probe {suffix}',
                 'Greater Cairo', 'Cairo', 'Operations Centre'),
            )
            site_id = int(site.lastrowid)

        denied = client.patch(
            f'/api/sites/{site_id}/customer-count',
            headers=headers,
            json={'customer_count': 900},
        )
        assert denied.status_code == 403

        # Dashboard navigation bridges expose modules only; domain RBAC stays
        # authoritative: an analytics-only user cannot mutate dispatch or
        # procurement state through any surfaced module.
        dispatch_denied = client.post(
            '/api/dispatch/999999/transition',
            headers=headers,
            json={'action': 'complete', 'notes': ''},
        )
        assert dispatch_denied.status_code in (401, 403), dispatch_denial_text(dispatch_denied)
        pr_denied = client.post(
            '/api/procurement/requisitions/999999/submit', headers=headers
        )
        assert pr_denied.status_code in (401, 403)


def dispatch_denial_text(response):
    try:
        return response.json()
    except Exception:
        return response.text


def _seed_scoped_site_asset(conn, suffix: str):
    site = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (
            f'SCOPE-{suffix}'.upper(),
            f'Scope probe site {suffix}',
            'Greater Cairo',
            'Cairo',
            'Electrical Substation',
            400,
        ),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LOC-{suffix}'.upper(), f'Scope bay {suffix}', 'Area', site_id),
    )
    stamp = now()
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            f'AST-SCOPE-{suffix.upper()}',
            f'Scope probe transformer {suffix}',
            'Transformer',
            'High',
            'Good',
            'Operating',
            int(location.lastrowid),
            stamp,
            stamp,
        ),
    )
    return site_id, int(asset.lastrowid)


def test_site_filter_propagates_into_reliability_indices():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
            site_a, asset_a = _seed_scoped_site_asset(conn, suffix + 'a')
            site_b, asset_b = _seed_scoped_site_asset(conn, suffix + 'b')
            for site_id, asset_id in ((site_a, asset_a), (site_b, asset_b)):
                conn.execute(
                    '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,end_at,reported_by,created_at,updated_at)
                       VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
                    (
                        f'OUT-SCOPE-{uuid.uuid4().hex[:8].upper()}',
                        asset_id,
                        site_id,
                        (anchor - timedelta(days=2)).isoformat(timespec='seconds'),
                        (anchor - timedelta(days=2, hours=-2)).isoformat(timespec='seconds'),
                        user_id,
                        stamp := now(),
                        stamp,
                    ),
                )

        scoped_a = client.get(
            '/api/kpis/reliability', headers=headers, params={'site_id': site_a}
        ).json()
        scoped_b = client.get(
            '/api/kpis/reliability', headers=headers, params={'site_id': site_b}
        ).json()

        assert scoped_a['customers_served'] == 400
        assert scoped_b['customers_served'] == 400
        # Each site sees exactly its own interruption; nothing bleeds across.
        for scoped in (scoped_a, scoped_b):
            assert scoped['counts']['sustained_interruptions'] == 1
            assert len(scoped['contributors']) == 1


def test_maintenance_scope_follows_asset_location_site():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            site_id, asset_id = _seed_scoped_site_asset(conn, suffix)
            stamp = now()
            conn.execute(
                '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                                           asset_id,target_finish,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)''',
                (
                    f'WO-SCOPE-{uuid.uuid4().hex[:8].upper()}',
                    'Scope probe overdue job',
                    'Critical',
                    'Approved',
                    'Corrective Maintenance',
                    asset_id,
                    (datetime.now() - timedelta(days=1)).date().isoformat(),
                    stamp,
                    stamp,
                ),
            )

        scoped = client.get(
            '/api/kpis/maintenance', headers=headers, params={'site_id': site_id}
        ).json()
        elsewhere = client.get(
            '/api/kpis/maintenance', headers=headers, params={'site_id': site_id + 999999}
        ).json()

        assert scoped['kpis']['overdue_work_orders']['value'] >= 1
        contributor = next(
            (c for c in scoped['contributors'] if c['asset_no'] == f'AST-SCOPE-{suffix.upper()}'),
            None,
        )
        assert contributor is not None
        assert contributor['days_overdue'] >= 1
        # An unrelated site scope must not inherit the probe backlog.
        assert elsewhere['kpis']['open_work_orders']['value'] == 0
        assert elsewhere['contributors'] == []


def test_as_of_window_moves_reliability_indices():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
            site_id, asset_id = _seed_scoped_site_asset(conn, suffix)
            conn.execute(
                '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,end_at,reported_by,created_at,updated_at)
                   VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
                (
                    f'OUT-ASOF-{uuid.uuid4().hex[:8].upper()}',
                    asset_id,
                    site_id,
                    (anchor - timedelta(days=2)).isoformat(timespec='seconds'),
                    (anchor - timedelta(days=2, hours=-2)).isoformat(timespec='seconds'),
                    user_id,
                    now(),
                    now(),
                ),
            )
            # Restore the declared customer count the scope helper seeds.
            conn.execute(
                'UPDATE sites SET customer_count=400 WHERE id=?', (site_id,)
            )

        today_view = client.get('/api/kpis/reliability', headers=headers).json()
        past_view = client.get(
            '/api/kpis/reliability',
            headers=headers,
            params={'as_of': (anchor - timedelta(days=40)).date().isoformat()},
        ).json()
        # The interruption sits inside today's 30-day window...
        assert today_view['kpis']['saifi']['value'] >= 0
        contributors = today_view['contributors']
        assert contributors and contributors[0]['duration_hours'] == 2.0
        # ...and strictly outside a window ending 40 days ago.
        assert past_view['counts']['sustained_interruptions'] == 0
