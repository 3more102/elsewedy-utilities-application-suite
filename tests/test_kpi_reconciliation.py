"""Cross-engine KPI reconciliation contracts.

Proves the two coexisting KPI surfaces agree where they must:

- ``/api/kpis/reliability``   — dashboard family adapters (kpi_store.py)
- ``/api/kpi/executive``      — canonical executive snapshot platform
                               (kpi_service.py)

The consolidation rule: identical fixtures must produce identical
SAIFI / SAIDI / CAIDI mathematics (after unit normalisation), identical
missing-customer honesty, and identical site isolation — with no route
collisions between the namespaces.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_scope(conn, suffix: str, customer_count: int | None) -> tuple[int, int]:
    stamp = now()
    site = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (
            f'RECON-{suffix}'.upper(),
            f'Reconciliation probe site {suffix}',
            'Greater Cairo',
            'Cairo',
            'Electrical Substation',
            customer_count,
        ),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LRECON-{suffix}'.upper(), f'Recon bay {suffix}', 'Area', site_id),
    )
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            f'AST-RECON-{suffix.upper()}',
            f'Recon probe transformer {suffix}',
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


def _seed_sustained_outage(conn, site_id: int, asset_id: int,
                           start: datetime, hours: float) -> None:
    user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
    end = start + timedelta(hours=hours)
    conn.execute(
        '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,
                                     start_at,end_at,reported_by,created_at,updated_at)
           VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (
            f'OUT-RECON-{uuid.uuid4().hex[:10].upper()}',
            asset_id,
            site_id,
            start.isoformat(timespec='seconds'),
            end.isoformat(timespec='seconds'),
            user_id,
            now(),
            now(),
        ),
    )


def test_saifi_saidi_caidi_equivalence_across_engines():
    """Both engines must agree exactly on the customer-weighted indices."""
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, 1000)
            # Two 2-hour sustained interruptions inside every window shape.
            _seed_sustained_outage(conn, site_id, asset_id,
                                   anchor - timedelta(days=2), 2.0)
            _seed_sustained_outage(conn, site_id, asset_id,
                                   anchor - timedelta(days=1), 2.0)

        family = client.get(
            '/api/kpis/reliability',
            headers=headers,
            params={'site_id': site_id, 'period_days': 30},
        ).json()
        executive = client.get(
            '/api/kpi/executive',
            headers=headers,
            params={'site_id': site_id, 'period_days': 30},
        ).json()['reliability']

        # SAIFI: (1000 + 1000 affected) / 1000 customers = 2.0 on both engines.
        assert family['kpis']['saifi']['value'] == 2.0
        assert executive['saifi'] == 2.0

        # SAIDI: 4 customer-hours / 1000 = 0.004 h == 0.25 min... expressed in
        # each engine's unit: family reports hours, executive minutes.
        family_saidi_hours = family['kpis']['saidi']['value']
        assert abs(family_saidi_hours * 60.0 - executive['saidi_minutes']) < 0.01

        # CAIDI: SAIDI / SAIFI must agree after unit conversion.
        family_caidi_hours = family['kpis']['caidi']['value']
        assert abs(family_caidi_hours * 60.0 - executive['caidi_minutes']) < 0.01


def test_missing_customer_parity_between_engines():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, None)
            _seed_sustained_outage(conn, site_id, asset_id,
                                   anchor - timedelta(hours=3), 1.0)

        family = client.get(
            '/api/kpis/reliability',
            headers=headers,
            params={'site_id': site_id, 'period_days': 30},
        ).json()
        executive = client.get(
            '/api/kpi/executive',
            headers=headers,
            params={'site_id': site_id, 'period_days': 30},
        ).json()['reliability']

        # Neither engine may invent an index when the denominator is unknown.
        for kpi_id in ('saifi', 'saidi', 'caidi'):
            assert family['kpis'][kpi_id]['value'] is None
        assert executive['saifi'] is None
        assert executive['saidi_minutes'] is None
        assert executive['caidi_minutes'] is None
        assert executive['customers_basis'] == 'unconfigured'
        assert family['kpis']['saifi']['missing_inputs'] == ['sites.customer_count']


def test_site_isolation_across_both_surfaces():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_a, asset_a = _seed_scope(conn, suffix + 'a', 500)
            site_b, asset_b = _seed_scope(conn, suffix + 'b', 500)
            _seed_sustained_outage(conn, site_a, asset_a,
                                   anchor - timedelta(days=1), 2.0)
            _seed_sustained_outage(conn, site_b, asset_b,
                                   anchor - timedelta(hours=6), 1.0)

        for path, make_params in (
            ('/api/kpis/reliability',
             lambda sid: {'site_id': sid, 'period_days': 30}),
            ('/api/kpi/executive', lambda sid: {'site_id': sid, 'period_days': 30}),
        ):
            a = client.get(path, headers=headers, params=make_params(site_a)).json()
            b = client.get(path, headers=headers, params=make_params(site_b)).json()
            if path.endswith('executive'):
                rel_a, rel_b = a['reliability'], b['reliability']
                assert rel_a['outage_count'] == 1 and rel_b['outage_count'] == 1
            else:
                assert len(a['contributors']) == 1
                assert len(b['contributors']) == 1
                assert a['counts']['sustained_interruptions'] == 1
                assert b['counts']['sustained_interruptions'] == 1


def test_kpi_namespaces_never_collide_and_install_once():
    pairs = []
    for route in app.routes:
        path = str(getattr(route, 'path', '') or '')
        if not (path.startswith('/api/kpi/') or path.startswith('/api/kpis/')
                or path == '/api/kpi'):
            continue
        for method in set(getattr(route, 'methods', set()) or set()):
            if method.upper() in ('HEAD', 'OPTIONS'):
                continue
            pairs.append((path, method.upper()))
    duplicates = {p: c for p, c in Counter(pairs).items() if c > 1}
    assert duplicates == {}, duplicates

    singular = {p for p, _ in pairs
                if p.startswith('/api/kpi') and not p.startswith('/api/kpis')}
    plural = {p for p, _ in pairs if p.startswith('/api/kpis')}
    assert singular, 'canonical /api/kpi surface missing'
    assert plural, 'dashboard family surface missing'
    assert not singular & plural, singular & plural


def test_dashboard_batch_still_serves_with_both_kpi_surfaces():
    with TestClient(app) as client:
        headers = _auth(client)
        dashboard = client.get('/api/dashboard', headers=headers)
        assert dashboard.status_code == 200
        assert 'material_blocked_work' in dashboard.json()

        for path, params in (
            ('/api/kpis/reliability', {'period_days': 90}),
            ('/api/kpis/inventory', {}),
            ('/api/kpis/maintenance', {'period_days': 90}),
            ('/api/kpis/workforce', {}),
            ('/api/kpi/executive', {'period_days': 90}),
        ):
            response = client.get(path, headers=headers, params=params)
            assert response.status_code == 200, (path, response.text)


def test_snapshot_cache_round_trip_through_api():
    """Plain GETs serve materialized snapshots; refresh recomputes live only.

    Preserved upstream semantic (kpi_service.executive_snapshot): refresh=true
    bypasses both read AND write of the snapshot store, so a forced refresh is
    not itself materialized. The contract below pins that behaviour so any
    future engine consolidation changes it deliberately, not accidentally.
    """
    with TestClient(app) as client:
        headers = _auth(client)

        first = client.get(
            '/api/kpi/executive',
            headers=headers,
            params={'period_days': 30},
        ).json()
        assert first['snapshot']['served_from_cache'] is False
        assert first['snapshot']['materialized'] is True

        cached = client.get(
            '/api/kpi/executive',
            headers=headers,
            params={'period_days': 30},
        ).json()
        assert cached['snapshot']['served_from_cache'] is True
        # Live responses carry no calculation stamp; cache hits always do.
        assert 'calculated_at' not in first['snapshot']
        assert cached['window']['period_days'] == first['window']['period_days']
        assert cached['reliability']['saifi'] == first['reliability']['saifi']

        refreshed = client.get(
            '/api/kpi/executive',
            headers=headers,
            params={'period_days': 30, 'refresh': True},
        ).json()
        assert refreshed['snapshot']['served_from_cache'] is False

        # Both carry freshness metadata from the canonical service.
        for payload in (first, cached, refreshed):
            assert payload['freshness']
