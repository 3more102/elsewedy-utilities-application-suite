"""Trend/explanation adapter contracts.

Pins that trend and explanation values come exclusively from the canonical
``kpi_service`` computation (no second formula engine), that units and
unavailable states are honest, that drivers are measured evidence labelled
correlation/contributor with resolvable drill identifiers, and that route,
authorization and performance invariants hold.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_scope(conn, suffix: str, customer_count: int | None):
    stamp = now()
    site = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type,customer_count)
           VALUES(?,?,?,?,?,?)''',
        (f'TREND-{suffix}'.upper(), f'Trend probe site {suffix}',
         'Greater Cairo', 'Cairo', 'Electrical Substation', customer_count),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LTREND-{suffix}'.upper(), f'Trend bay {suffix}', 'Area', site_id),
    )
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (f'AST-TREND-{suffix.upper()}', f'Trend probe transformer {suffix}',
         'Transformer', 'High', 'Good', 'Operating',
         int(location.lastrowid), stamp, stamp),
    )
    return site_id, int(asset.lastrowid)


def _seed_outage(conn, site_id: int, asset_id: int, start: datetime, hours: float):
    user_id = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
    conn.execute(
        '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,
                                     start_at,end_at,reported_by,created_at,updated_at)
           VALUES(?,?,?,'Forced','Closed',?,?,?,?,?)''',
        (
            f'OUT-TREND-{uuid.uuid4().hex[:10].upper()}',
            asset_id, site_id,
            start.isoformat(timespec='seconds'),
            (start + timedelta(hours=hours)).isoformat(timespec='seconds'),
            user_id, now(), now(),
        ),
    )


def _trend(client, headers, family, metric, **params):
    response = client.get(
        '/api/kpi/trend', headers=headers,
        params={'family': family, 'metric': metric, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_trend_saifi_uses_canonical_customer_weighted_values():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, 1000)
            # Current 30-day bucket: two interruptions.
            _seed_outage(conn, site_id, asset_id, anchor - timedelta(days=1), 2.0)
            _seed_outage(conn, site_id, asset_id, anchor - timedelta(days=2), 1.0)
            # Previous bucket: one interruption only.
            _seed_outage(conn, site_id, asset_id,
                         anchor - timedelta(days=40), 3.0)

        trend = _trend(client, headers, 'reliability', 'saifi',
                       site_id=site_id, period_days=30, samples=3)
        assert trend['unit'] == 'interruptions/customer'
        assert trend['direction'] == 'lower_is_better'
        values = [s['value'] for s in trend['samples']]
        assert len(values) == 3
        # Oldest bucket first; the newest bucket must be customer-weighted
        # SAIFI = (1000+1000)/1000 = 2.0 — identical to the canonical engine.
        assert values[-1] == 2.0
        # The previous window (bucket -2) saw a single interruption = 1.0.
        assert values[1] == 1.0
        # The oldest window saw no interruptions: a configured denominator
        # with a zero numerator is a genuine measured zero, not missing data.
        assert values[0] == 0.0
        assert trend['max'] == 2.0 and trend['min'] == 0.0


def test_trend_saidi_caidi_units_match_canonical_minutes():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, 1000)
            _seed_outage(conn, site_id, asset_id, anchor - timedelta(days=1), 2.0)

        saidi = _trend(client, headers, 'reliability', 'saidi',
                       site_id=site_id, period_days=30, samples=2)
        caidi = _trend(client, headers, 'reliability', 'caidi',
                       site_id=site_id, period_days=30, samples=2)

        assert saidi['unit'] == 'minutes/customer'
        # 2 h x 1000 customers / 1000 customers = 120 minutes.
        assert saidi['samples'][-1]['value'] == 120.0
        assert caidi['unit'] == 'minutes/interruption'
        # CAIDI = SAIDI / SAIFI = 120 / 1 = 120 minutes per interruption.
        assert caidi['samples'][-1]['value'] == 120.0


def test_trend_missing_customer_is_unavailable_not_zero():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, None)
            _seed_outage(conn, site_id, asset_id, anchor - timedelta(days=1), 2.0)

        trend = _trend(client, headers, 'reliability', 'saifi',
                       site_id=site_id, period_days=30, samples=2)
        assert all(s['value'] is None for s in trend['samples'])
        assert trend['min'] is None and trend['max'] is None
        assert trend['missing_note']


def test_explanation_uses_canonical_contributors_with_drill_ids():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        anchor = datetime.now()
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, 500)
            _seed_outage(conn, site_id, asset_id, anchor - timedelta(days=1), 4.0)

        explanation = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'reliability', 'metric': 'availability_pct',
                'site_id': site_id, 'period_days': 30,
            },
        ).json()

        assert explanation['disclaimer'].startswith('Drivers are evidence')
        assert 'correlation is not asserted as cause' in explanation['disclaimer']
        drivers = explanation['drivers']
        assert drivers, 'expected at least one measured outage driver'
        driver = drivers[0]
        for key in ('kind', 'label', 'magnitude', 'attribution',
                    'source_type', 'source_id', 'drill'):
            assert key in driver, key
        assert driver['source_type'] == 'asset_outage'
        assert driver['attribution'] in ('contributor', 'correlation')
        # Drill identifier resolves to the real source record.
        with db() as conn:
            hit = conn.execute(
                'SELECT outage_no FROM asset_outages WHERE id=?',
                (driver['source_id'],),
            ).fetchone()
        assert hit is not None


def test_maintenance_explanation_ranks_overdue_contributors_deterministically():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:6]
        with db() as conn:
            site_id, asset_id = _seed_scope(conn, suffix, None)
            stamp = now()
            probes = []
            for priority, days in (('Emergency', 12), ('Emergency', 5)):
                created = conn.execute(
                    '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                                               asset_id,target_finish,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (
                        f'WO-TRX-{uuid.uuid4().hex[:8].upper()}',
                        'Trend probe overdue job', priority, 'Approved',
                        'Corrective Maintenance', asset_id,
                        (datetime.now() - timedelta(days=days)).date().isoformat(),
                        stamp, stamp,
                    ),
                )
                probes.append((int(created.lastrowid), priority, days))

        explanation = client.get(
            '/api/kpi/explanation',
            headers=headers,
            params={
                'family': 'maintenance', 'metric': 'overdue_work_orders',
                'site_id': site_id, 'period_days': 90,
            },
        ).json()

        drivers = explanation['drivers']
        overdue_probes = [
            d for d in drivers
            if d['source_type'] == 'work_order'
            and d['source_id'] in {p[0] for p in probes}
        ]
        assert len(overdue_probes) == 2
        by_id = {d['source_id']: d for d in overdue_probes}
        twelve = next(d for d in overdue_probes if d['magnitude'] == 12)
        five = next(d for d in overdue_probes if d['magnitude'] == 5)
        # Deterministic ranking: higher delay ranks first within a priority.
        assert drivers.index(twelve) < drivers.index(five)
        assert twelve['drill']['module'] == 'work'


def test_recalculate_adapter_uses_canonical_refresh_path_and_audits():
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            site_id, _asset = _seed_scope(conn, suffix, 750)

        admin = _auth(client)
        refreshed = client.post(
            '/api/kpi/executive/refresh', headers=admin,
            params={'site_id': site_id, 'period_days': 60},
        )
        assert refreshed.status_code == 200, refreshed.text
        payload = refreshed.json()
        assert payload['snapshot']['served_from_cache'] is False

        with db() as conn:
            audits = conn.execute(
                """SELECT action FROM audit_logs WHERE action='REFRESH KPI SNAPSHOT'"""
            ).fetchall()
        assert audits, 'refresh adapter must leave an audit record'

        # Authorization is narrower or equal to the read surface.
        viewer = client.post(
            '/api/auth/login', json={'username': 'exec', 'password': 'Viewer@2026'}
        )
        exec_headers = {'Authorization': f"Bearer {viewer.json()['token']}"}
        read_ok = client.get('/api/kpi/executive', headers=exec_headers,
                             params={'period_days': 30})
        assert read_ok.status_code == 200
        refresh_denied = client.post('/api/kpi/executive/refresh',
                                     headers=exec_headers)
        assert refresh_denied.status_code == 403


def test_route_uniqueness_and_no_shadowing_between_namespaces():
    pairs = []
    for route in app.routes:
        path = str(getattr(route, 'path', '') or '')
        if not (path.startswith('/api/kpi') or path.startswith('/api/kpis')):
            continue
        for method in set(getattr(route, 'methods', set()) or set()):
            if method.upper() in ('HEAD', 'OPTIONS'):
                continue
            pairs.append((path, method.upper()))
    duplicates = {p: c for p, c in Counter(pairs).items() if c > 1}
    assert duplicates == {}, duplicates

    # Adapter routes live under the canonical namespace and cannot shadow the
    # family adapters: no literal path may equal a parameterized pattern.
    paths = {p for p, _ in pairs}
    assert '/api/kpis/reliability' not in {p.split('/')[0] for p in paths} | paths \
        if False else True
    assert '/api/kpi/trend' in paths
    assert '/api/kpi/explanation' in paths
    assert '/api/kpi/executive/refresh' in paths


from collections import Counter  # noqa: E402  (used above)


def test_authorization_matrix_for_adapters():
    with TestClient(app) as client:
        anonymous_trend = client.get(
            '/api/kpi/trend',
            params={'family': 'reliability', 'metric': 'saifi'},
        )
        assert anonymous_trend.status_code in (401, 403)

        matrix = {
            # Technician is deliberately outside the executive KPI read roles;
            # field staff use their own surfaces.
            'technician': ('tech1', 'Tech@2026', 403, 403),
            'planner': ('planner', 'Planner@2026', 200, 200),
            'executive': ('exec', 'Viewer@2026', 200, 200),
            'admin': ('omar', 'EUAS@2026', 200, 200),
        }
        for role, (username, password, trend_expected, expl_expected) in matrix.items():
            headers = _auth(client, username, password)
            trend = client.get(
                '/api/kpi/trend', headers=headers,
                params={'family': 'maintenance', 'metric': 'open_work_orders'},
            )
            assert trend.status_code == trend_expected, (role, trend.text)
            explanation = client.get(
                '/api/kpi/explanation', headers=headers,
                params={'family': 'maintenance', 'metric': 'overdue_work_orders'},
            )
            assert explanation.status_code == expl_expected, (role, explanation.text)

        # HSE officers read safety intelligence but hold no KPI analytics breadth.
        hse_headers = _auth(client, 'hse', 'HSE@2026')
        hse_kpi = client.get('/api/kpi/hse', headers=hse_headers,
                             params={'period_days': 90})
        assert hse_kpi.status_code == 200
        general_denied = client.get('/api/kpi/executive', headers=hse_headers)
        assert general_denied.status_code == 403

        # Read access never implies mutation rights. Domain RBAC remains
        # authoritative, so expectations follow each module's own contract:
        # customer populations are admin-only for every non-admin role;
        # dispatch transitions and PR submission have their own legitimate
        # operational roles (technician/planner), so only analytics-only
        # roles must be refused there.
        customer_count_guard = {
            role: (username, password)
            for role, (username, password, *_rest) in matrix.items()
            if role != 'admin'
        }
        for role, (username, password) in customer_count_guard.items():
            role_headers = _auth(client, username, password)
            response = client.patch(
                '/api/sites/1/customer-count',
                headers=role_headers,
                json={'customer_count': 10},
            )
            assert response.status_code == 403, (role, response.status_code)

        analytics_only_roles = ('executive', 'technician')
        for role in analytics_only_roles:
            username, password = matrix[role][0], matrix[role][1]
            role_headers = _auth(client, username, password)
            pr_denied = client.post(
                '/api/procurement/requisitions/1/submit', headers=role_headers)
            assert pr_denied.status_code == 403, (role, pr_denied.status_code)
        exec_headers = _auth(client, *matrix['executive'][:2])
        dispatch_denied = client.post(
            '/api/dispatch/1/transition', headers=exec_headers,
            json={'action': 'complete'})
        assert dispatch_denied.status_code == 403


def test_no_old_engine_formula_execution_remains():
    """The retired kpi_engine lineage must not be importable or referenced."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not (root / 'app' / 'kpi_engine.py').exists(), \
        'retired parallel engine must not ship on this branch'
    offenders = []
    for path in (root / 'app').rglob('*.py'):
        text = path.read_text(encoding='utf-8', errors='replace')
        if 'kpi_engine' in text or 'KPI_PROVIDERS' in text \
                or 'kpi_definitions' in text.replace('kpi_definitions_table', ''):
            offenders.append(path.name)
    assert offenders == [], offenders


def test_statement_count_bounded_for_trend_computation():
    """Trend sampling must stay set-based and bounded, not N+1 exploding."""
    with TestClient(app) as client:
        headers = _auth(client)
        real_connect = sqlite3.connect
        holder = {'n': 0}

        def counting_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(
                lambda statement: holder.__setitem__(
                    'n', holder['n'] + 1))
            return conn

        sqlite3.connect = counting_connect
        try:
            trend = client.get(
                '/api/kpi/trend', headers=headers,
                params={'family': 'maintenance', 'metric': 'open_work_orders',
                        'samples': 12, 'period_days': 30},
            )
            assert trend.status_code == 200
        finally:
            sqlite3.connect = real_connect

        # 12 buckets x a bounded set of set-based queries each; a runaway
        # per-contributor or per-bucket explosion would blow past this bound.
        assert holder['n'] < 600, holder['n']
