"""Deterministic KPI snapshot performance measurement.

Run: python scripts/kpi_benchmark.py [--assets 200 --open-wo 60]
Builds an isolated SQLite fleet, measures executive_snapshot wall time and
statement count before/after optimization work. Never touches production data.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=int, default=200)
    parser.add_argument('--open-wo', type=int, default=80)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='euas-kpi-bench-') as td:
        os.environ['EUAS_DB_PATH'] = str(Path(td) / 'bench.db')
        os.environ.pop('EUAS_DATABASE_URL', None)
        os.environ['EUAS_ENV'] = 'development'
        sys.path.insert(0, str(ROOT))

        from app.auth import hash_password
        from app.database import db, init_db
        init_db(hash_password)

        now = datetime.now().isoformat(timespec='seconds')
        with db() as conn:
            admin = int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0])
            type_id = int(conn.execute("SELECT id FROM asset_types ORDER BY id LIMIT 1").fetchone()[0])
            loc = int(conn.execute('SELECT id FROM locations ORDER BY id LIMIT 1').fetchone()[0])
            for i in range(args.assets):
                conn.execute(
                    '''INSERT INTO assets(asset_no,name,description,asset_type_id,category,
                         manufacturer,model,serial_no,criticality,condition,status,location_id,
                         department,purchase_cost,current_value,maintenance_strategy,
                         created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (f'BENCH-{i:05d}', f'Bench Asset {i}', 'benchmark', type_id, 'Pump', 'X', 'M', f'SN{i}',
                     'Medium', 'Good', 'Operating', loc, 'Bench', 0, 0, 'Preventive', now, now))
            wo_rows = []
            for i in range(args.open_wo):
                asset_id = int(conn.execute('SELECT id FROM assets WHERE asset_no=?',
                                            (f'BENCH-{i % max(1, args.assets):05d}',)).fetchone()[0])
                wo_rows.append((f'WO-BENCH-{i:05d}', f'Bench WO {i}', 'Medium', 'Approved',
                                'Preventive', asset_id, 2.0, now, now))
            for wo_no, title, priority, status, wtype, asset_id, est, created, updated in wo_rows:
                conn.execute(
                    '''INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         estimated_hours,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (wo_no, title, priority, status, wtype, asset_id, est, created, updated))
            # One real outage so reliability math is exercised.
            tr = int(conn.execute("SELECT id FROM assets WHERE asset_no='TR-001'").fetchone()[0])
            ncs = int(conn.execute("SELECT id FROM sites WHERE site_code='NCS-01'").fetchone()[0])
            start = (datetime.now() - timedelta(days=2)).isoformat(timespec='seconds')
            conn.execute(
                '''INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,start_at,
                     end_at,reported_by,created_at,updated_at)
                   VALUES('OUT-BENCH',? ,?,'Forced','Closed',?,?,?, ?, ?)''',
                (tr, ncs, start, (datetime.now() - timedelta(days=2) + timedelta(hours=3)).isoformat(timespec='seconds'),
                 admin, start, start))

        from app.kpi_service import ExecutiveFilters, compute_asset_kpis, executive_snapshot, risk_weighted_backlog

        statement_count = {'n': 0}

        def timed(label, fn):
            import sqlite3
            counts = {'n': 0}

            real_connect = sqlite3.connect

            def counting_connect(*a, **kw):
                conn_ = real_connect(*a, **kw)

                def trace(statement):
                    counts['n'] += 1
                try:
                    conn_.set_trace_callback(trace)
                except Exception:
                    pass
                return conn_
            sqlite3.connect = counting_connect
            t0 = time.perf_counter()
            result = fn()
            elapsed = time.perf_counter() - t0
            sqlite3.connect = real_connect
            print(f'{label}: {elapsed*1000:.0f} ms, statements={counts["n"]}')
            return result

        f = ExecutiveFilters(period_days=30)
        snap = timed(f'executive_snapshot(assets={args.assets}, open_wo={args.open_wo})',
                     lambda: _with_conn(executive_snapshot, f))
        timed(f'compute_asset_kpis only', lambda: _with_conn(compute_asset_kpis, f))
        timed('risk_weighted_backlog only', lambda: _with_conn(risk_weighted_backlog, f))
    return 0


def _with_conn(fn, *a, **kw):
    from app.database import db
    with db() as conn:
        return fn(conn, *a, **kw)


if __name__ == '__main__':
    raise SystemExit(main())
