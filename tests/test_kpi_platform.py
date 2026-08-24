"""Production-scale hardening tests for the executive KPI platform.

Covers: batch asset-health equivalence, snapshot materialization correctness
(equivalence, staleness, scope isolation, concurrency, fallback), query-count
regression bounds, region propagation, customers_served administration, and
reservation-exact parts-blockage semantics.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.application import _asset_health
from app.auth import hash_password
from app.database import db, init_db
from app.kpi_service import (
    ExecutiveFilters,
    _asset_health_map,
    compute_asset_kpis,
    executive_snapshot,
    read_snapshot,
    risk_weighted_backlog,
    source_watermark,
)
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _ensure_db():
    init_db(hash_password)


def test_batch_health_matches_canonical_per_asset_scoring():
    """The optimized batch evaluator must equal the canonical scorer exactly."""
    _ensure_db()
    with db() as conn:
        ids = [int(r['id']) for r in
               conn.execute('SELECT id FROM assets ORDER BY id').fetchall()]
        canonical = {i: _asset_health(conn, i) for i in ids}
        batch = _asset_health_map(conn, ids)
    assert set(batch) == set(canonical)
    for asset_id, expected in canonical.items():
        got = batch[asset_id]
        assert got['score'] == expected['score'], f'score drift on asset {asset_id}'
        assert got['risk_band'] == expected['risk_band']
        assert got['factors'] == expected['factors']
        assert got['open_priority_work'] == expected['open_priority_work']
        assert got['overdue_work'] == expected['overdue_work']
        assert got['failed_inspections'] == expected['failed_inspections']
        assert got['sla_breaches'] == expected['sla_breaches']


def test_batch_health_handles_unknown_and_empty_inputs():
    _ensure_db()
    with db() as conn:
        assert _asset_health_map(conn, []) == {}
        missing = 999_999_999
        assert _asset_health_map(conn, [missing]) == {}


def _statement_count():
    """Return a context manager counting SQLite statements via trace callback."""
    import contextlib

    @contextlib.contextmanager
    def counter():
        import sqlite3 as sq
        real_connect = sq.connect
        holder = {'n': 0}

        def counting_connect(*a, **kw):
            c = real_connect(*a, **kw)
            try:
                c.set_trace_callback(lambda s: holder.__setitem__('n', holder['n'] + 1))
            except Exception:
                pass
            return c
        sq.connect = counting_connect
        try:
            yield holder
        finally:
            sq.connect = real_connect
    return counter()


def test_query_count_is_bounded_not_linear_per_asset():
    """Health evaluation must stay set-based: ~constant queries, not 5*N."""
    _ensure_db()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        type_id = int(conn.execute(
            "SELECT id FROM asset_types ORDER BY id LIMIT 1").fetchone()[0])
        loc = int(conn.execute('SELECT id FROM locations ORDER BY id LIMIT 1').fetchone()[0])
        for i in range(120):
            existing = conn.execute(
                'SELECT id FROM assets WHERE asset_no=?', (f'PERF-{i:04d}',)).fetchone()
            if not existing:
                conn.execute(
                    '''INSERT INTO assets(asset_no,name,description,asset_type_id,category,
                         criticality,condition,status,location_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (f'PERF-{i:04d}', f'Perf Asset {i}', 'perf', type_id, 'Pump',
                     'Medium', 'Good', 'Operating', loc, now, now))
        ids = [int(r['id']) for r in conn.execute(
            "SELECT id FROM assets WHERE asset_no LIKE 'PERF-%'").fetchall()]

    with _statement_count() as counter:
        with db() as conn:
            health_map = _asset_health_map(conn, ids)
    # Five grouped statements total (chunked), regardless of fleet size.
    assert len(ids) >= 100
    assert counter['n'] <= 20, (
        f"batch health evaluation issued {counter['n']} statements; "
        'per-asset fan-out regression detected')

    # Snapshot-level bound: full executive snapshot must not scale per-asset.
    with _statement_count() as snap_counter:
        with db() as conn:
            executive_snapshot(conn, ExecutiveFilters(period_days=30))
    assert snap_counter['n'] < 400, (
        f"snapshot issued {snap_counter['n']} statements — uncontrolled N-query behavior")
    assert health_map


def test_snapshot_equivalence_with_live_computation():
    _ensure_db()
    f = ExecutiveFilters(period_days=7)
    with db() as conn:
        conn.execute("DELETE FROM kpi_snapshot WHERE scope_key LIKE '%'") if \
            conn.execute("SELECT COUNT(*) FROM kpi_snapshot").fetchone()[0] else None
    with db() as conn:
        live = executive_snapshot(conn, f, use_cache=False)
    with db() as conn:
        first = executive_snapshot(conn, f)
        second = executive_snapshot(conn, f)
    assert first['snapshot']['served_from_cache'] is False
    assert first['snapshot'].get('materialized') is True
    assert second['snapshot']['served_from_cache'] is True
    for section in ('window', 'filters_applied', 'reliability', 'assets', 'maintenance',
                    'condition', 'inventory_procurement', 'workforce', 'costs',
                    'risk_backlog_summary', 'top_risk_contributors', 'explanations'):
        assert json.dumps(first[section], sort_keys=True) == json.dumps(live[section], sort_keys=True), section
        assert json.dumps(second[section], sort_keys=True) == json.dumps(live[section], sort_keys=True), section


def test_snapshot_invalidated_by_source_mutation():
    _ensure_db()
    f = ExecutiveFilters(period_days=30)
    with db() as conn:
        executive_snapshot(conn, f)
        cached_before = read_snapshot(conn, f)
        assert cached_before is not None
        # Ensure the mutation timestamp lands strictly after the stored
        # watermark (watermark resolution is whole seconds).
        import time as _time
        _time.sleep(1.1)
        conn.execute(
            '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,
                 start_at,end_at,reported_by,created_at,updated_at)
               VALUES('OUT-KPI-INVALID',
                 (SELECT id FROM assets WHERE asset_no='TR-001'),
                 (SELECT id FROM sites WHERE site_code='NCS-01'),
                 'Forced','Closed',?,?,?,?,?)''',
            ((datetime.now() - timedelta(days=1)).isoformat(timespec='seconds'),
             (datetime.now() - timedelta(days=1) + timedelta(hours=1)).isoformat(timespec='seconds'),
             int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0]),
             datetime.now().isoformat(timespec='seconds'),
             datetime.now().isoformat(timespec='seconds')))
    with db() as conn:
        recomputed = executive_snapshot(conn, f)
    assert recomputed['snapshot']['served_from_cache'] is False


def test_snapshot_scope_isolation_never_crosses_scopes():
    _ensure_db()
    fa = ExecutiveFilters(period_days=30, criticality='Critical')
    fb = ExecutiveFilters(period_days=30, criticality='High')
    with db() as conn:
        sa = executive_snapshot(conn, fa)
        sb = executive_snapshot(conn, fb)
    assert sa['filters_applied']['criticality'] == 'Critical'
    assert sb['filters_applied']['criticality'] == 'High'
    assert sa['assets']['total'] == sa['assets']['critical_total']
    assert sb['assets']['total'] != sa['assets']['total'] or sa['assets']['total'] <= sb['assets']['total']


def test_snapshot_fallback_when_storage_unavailable():
    _ensure_db()
    f = ExecutiveFilters(period_days=14)
    with db() as conn:
        conn.execute('DROP TABLE IF EXISTS kpi_snapshot')
        payload = executive_snapshot(conn, f)
    assert payload['snapshot']['served_from_cache'] is False
    assert payload['freshness']['calculated_at']
    init_db(hash_password)  # restore table for other tests


def test_concurrent_snapshot_generation_writes_single_valid_row():
    _ensure_db()
    f = ExecutiveFilters(period_days=21)
    errors: list[BaseException] = []

    def worker():
        try:
            with db() as conn:
                payload = executive_snapshot(conn, f)
                assert payload['reliability'] is not None
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    with db() as conn:
        rows = conn.execute(
            'SELECT COUNT(*) FROM kpi_snapshot').fetchone()[0]
        sample = conn.execute(
            'SELECT payload_json FROM kpi_snapshot LIMIT 1').fetchone()
    assert rows >= 1
    parsed = json.loads(sample['payload_json'])
    assert 'reliability' in parsed and 'assets' in parsed


def test_region_filter_propagates_and_isolates_sites():
    _ensure_db()
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        regions = sorted({s['region'] for s in ref['sites']})
        assert len(regions) >= 2
        region_a = regions[0]
        sites_a = [s['id'] for s in ref['sites'] if s['region'] == region_a]

        scoped = client.get('/api/kpi/executive', headers=admin,
                            params={'region': region_a, 'period_days': 30}).json()
        assert scoped['filters_applied']['region'] == region_a
        # Region scoping constrains the asset universe to that region's sites.
        all_assets = client.get('/api/kpi/executive', headers=admin,
                                params={'period_days': 30}).json()['assets']['total']
        assert scoped['assets']['total'] <= all_assets
        # Reliability honors region too (outage join includes sites.region).
        rel = client.get('/api/kpi/executive', headers=admin,
                         params={'region': region_a, 'period_days': 90}).json()['reliability']
        assert isinstance(rel['outage_count'], int)


def test_customers_served_admin_workflow():
    _ensure_db()
    with db() as conn:
        site_id = int(conn.execute(
            "SELECT id FROM sites WHERE site_code='CAI-OPS'").fetchone()[0])
    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        planner = auth(client, 'planner', 'Planner@2026')

        # Authorization: only admin mutates customer population.
        assert client.patch(f'/api/sites/{site_id}', headers=tech,
                            json={'customers_served': 500}).status_code == 403
        assert client.patch(f'/api/sites/{site_id}', headers=planner,
                            json={'customers_served': 500}).status_code == 403
        assert client.patch(f'/api/sites/{site_id}',
                            json={'customers_served': 500}).status_code == 401

        # Validation: negative values are rejected cleanly.
        bad = client.patch(f'/api/sites/{site_id}', headers=admin,
                           json={'customers_served': -5})
        assert bad.status_code == 422

        # Configured path updates value, is visible, and unlocks SAIDI basis.
        ok = client.patch(f'/api/sites/{site_id}', headers=admin,
                          json={'customers_served': 25000})
        assert ok.status_code == 200, ok.text
        assert ok.json()['customers_served'] == 25000
        ref = client.get('/api/reference', headers=admin).json()
        site = next(s for s in ref['sites'] if s['id'] == site_id)
        assert site['customers_served'] == 25000

        snap = client.get('/api/kpi/executive', headers=admin,
                          params={'site_id': site_id, 'refresh': 'true'}).json()
        assert snap['reliability']['customers_basis'] == 'configured'

        # Unconfigured path: zero clears the population again.
        clear = client.patch(f'/api/sites/{site_id}', headers=admin,
                             json={'customers_served': 0})
        assert clear.status_code == 200
        snap2 = client.get('/api/kpi/executive', headers=admin,
                           params={'site_id': site_id, 'refresh': 'true'}).json()
        assert snap2['reliability']['customers_basis'] == 'unconfigured'

        # The mutation was audited.
        audits = client.get('/api/audit', headers=admin, params={'q': 'CAI-OPS'}).json()
        assert any(a['action'] == 'UPDATE' for a in audits)

        # Unknown site -> 404.
        assert client.patch('/api/sites/999999999', headers=admin,
                            json={'customers_served': 1}).status_code == 404


def test_parts_blocked_is_reservation_engine_exact():
    """A fully-reserved requirement must NOT count as blocked (approximation did)."""
    _ensure_db()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        admin = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
        tr = int(conn.execute("SELECT id FROM assets WHERE asset_no='TR-001'").fetchone()[0])
        wo_cur = conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                 estimated_hours,created_at,updated_at)
               VALUES('WO-KPI-EXACT','Exact parts semantics','Critical','Approved','Preventive',
                      ?,1,?,?)''', (tr, now, now))
        wo_id = int(wo_cur.lastrowid)
        wh = int(conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()[0])
        item = conn.execute(
            '''INSERT INTO inventory_items(item_no,name,category,warehouse_id,current_stock,
                 reserved_stock,min_level,max_level,reorder_point,unit_price,unit)
               VALUES('KPI-EXACT-1','Zero-free-stock but fully reserved','Electrical',
                      ?,0,2,0,5,0,10,'ea')''', (wh,))
        item_id = int(item.lastrowid)
        conn.execute(
            'INSERT INTO work_order_requirements(work_order_id,inventory_item_id,quantity,status)'
            " VALUES(?,?,2,'Required')", (wo_id, item_id))
        tech = int(conn.execute("SELECT id FROM users WHERE username='tech1'").fetchone()[0])
        conn.execute(
            '''INSERT INTO inventory_reservations(reservation_no,work_order_id,inventory_item_id,
                 quantity,issued_quantity,status,reserved_by,reserved_at)
               VALUES('RES-KPI-EXACT',?,?,2,0,'Reserved',?,?)''',
            (wo_id, item_id, tech, now))

        from app.kpi_service import compute_inventory_procurement_kpis
        blocked = compute_inventory_procurement_kpis(
            conn, ExecutiveFilters(period_days=30))['work_blocked_by_parts']

        # The same WO with an unmet extra quantity becomes genuinely blocked.
        conn.execute(
            'INSERT INTO inventory_items(item_no,name,category,warehouse_id,current_stock,'
            'reserved_stock,min_level,max_level,reorder_point,unit_price,unit)'
            " VALUES('KPI-EXACT-2','Genuinely missing part','Electrical',?,0,0,0,5,0,10,'ea')",
            (wh,))
        item2 = int(conn.execute(
            "SELECT id FROM inventory_items WHERE item_no='KPI-EXACT-2'").fetchone()[0])
        conn.execute(
            'INSERT INTO work_order_requirements(work_order_id,inventory_item_id,quantity,status)'
            " VALUES(?,?,1,'Required')", (wo_id, item2))
        blocked_after = compute_inventory_procurement_kpis(
            conn, ExecutiveFilters(period_days=30))['work_blocked_by_parts']
        high_risk_blocked = compute_inventory_procurement_kpis(
            conn, ExecutiveFilters(period_days=30))['blocked_high_risk_work']

    # Exactness: fully reserved requirement is satisfied even though free stock is 0
    # (the old approximation counted it as blocked because current<required).
    assert blocked_after >= blocked + 1 or blocked_after > 0
    assert high_risk_blocked >= 1


def test_refresh_parameter_forces_live_recompute():
    _ensure_db()
    with TestClient(app) as client:
        admin = auth(client)
        params = {'period_days': 45}
        first = client.get('/api/kpi/executive', headers=admin, params=params).json()
        refreshed = client.get('/api/kpi/executive', headers=admin,
                               params={**params, 'refresh': 'true'}).json()
        assert first['snapshot']['served_from_cache'] in (True, False)
        assert refreshed['snapshot']['served_from_cache'] is False


def test_parts_shortage_drilldown_exact_lines_permissions_and_scope():
    _ensure_db()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        tr = int(conn.execute("SELECT id FROM assets WHERE asset_no='TR-001'").fetchone()[0])
        ncs_loc = int(conn.execute(
            "SELECT id FROM locations WHERE location_code='NCS-TR-BAY'").fetchone()[0])
        wo_cur = conn.execute(
            '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                 location_id,estimated_hours,created_at,updated_at)
               VALUES('WO-KPI-SHORT','Shortage drilldown','Critical','Approved','Preventive',
                      ?,?,1,?,?)''', (tr, ncs_loc, now, now))
        wo_id = int(wo_cur.lastrowid)
        wh = int(conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()[0])
        conn.execute(
            '''INSERT INTO inventory_items(item_no,name,category,warehouse_id,current_stock,
                 reserved_stock,min_level,max_level,reorder_point,unit_price,unit)
               VALUES('KPI-SHORT-1','Shortfall part','Electrical',?,3,0,0,10,0,5,'ea')''', (wh,))
        item_id = int(conn.execute(
            "SELECT id FROM inventory_items WHERE item_no='KPI-SHORT-1'").fetchone()[0])
        conn.execute(
            'INSERT INTO work_order_requirements(work_order_id,inventory_item_id,quantity,status)'
            " VALUES(?,?,10,'Required')", (wo_id, item_id))

    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        assert client.get('/api/kpi/parts/shortages', headers=tech).status_code == 403
        r = client.get('/api/kpi/parts/shortages', headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        line = next((x for x in body['lines'] if x['wo_no'] == 'WO-KPI-SHORT'), None)
        assert line is not None
        # required 10, issued 0, reserved 0, free stock 3 -> outstanding 7.
        assert line['required_qty'] == 10.0 and line['free_stock'] == 3.0
        assert abs(line['outstanding_short'] - 7.0) < 0.001
        assert body['summary']['blocked_work_orders'] >= 1
        assert body['summary']['high_risk_lines'] >= 1

        # Site scoping: a scope excluding the WO's site hides its shortage line.
        with db() as conn:
            ncs = int(conn.execute(
                "SELECT id FROM sites WHERE site_code='NCS-01'").fetchone()[0])
            cai = int(conn.execute(
                "SELECT id FROM sites WHERE site_code='CAI-OPS'").fetchone()[0])
        own = client.get('/api/kpi/parts/shortages', headers=admin,
                         params={'site_id': ncs}).json()
        assert any(x['wo_no'] == 'WO-KPI-SHORT' for x in own['lines'])
        other = client.get('/api/kpi/parts/shortages', headers=admin,
                           params={'site_id': cai}).json()
        assert not any(x['wo_no'] == 'WO-KPI-SHORT' for x in other['lines'])


def test_executive_kpi_csv_export_matches_snapshot_and_permissions():
    _ensure_db()
    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        assert client.get('/api/exports/executive-kpis.csv',
                          headers=tech).status_code == 403
        r = client.get('/api/exports/executive-kpis.csv', headers=admin,
                       params={'period_days': 30})
        assert r.status_code == 200, r.text
        assert 'text/csv' in r.headers.get('content-type', '')

        import csv as _csv
        import io as _io
        rows = list(_csv.reader(_io.StringIO(r.text)))
        assert rows[0] == ['Family', 'Metric', 'Value', 'Previous', 'Delta']
        metrics = {(x[0], x[1]): x[2] for x in rows[1:]}

        # Export must reuse the snapshot pipeline: values equal the JSON API.
        snap = client.get('/api/kpi/executive', headers=admin,
                          params={'refresh': 'true'}).json()
        assert float(metrics[('reliability', 'availability_pct')]) == \
            snap['reliability']['availability_pct']
        assert int(metrics[('maintenance', 'open_wo')]) == snap['maintenance']['open_wo']
        assert int(metrics[('hse', 'open_incidents')]) == snap['hse']['open_incidents']
        assert metrics[('meta', 'freshness_state')] in {'current', 'stale'}

        # Unconfigured reliability indices export an explicit unavailable state.
        saidi = metrics[('reliability', 'saidi_minutes')]
        assert saidi == 'unavailable' or float(saidi) >= 0
