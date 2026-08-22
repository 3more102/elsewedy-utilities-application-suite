from __future__ import annotations

import threading
import uuid
from datetime import date

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db, now
from app.main import app
from app.work_order_number_store import _sequence_key


WORKERS = 8


def _admin_and_asset(conn):
    user = conn.execute(
        """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
           WHERE u.username='omar'"""
    ).fetchone()
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    assert user and asset
    return int(user['id']), int(asset['id'])


def _run_workers(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    values: list[str] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            values.append(operation(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=25)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(values) == workers
    return values


def test_concurrent_job_run_number_allocation_commits_unique_real_records():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user_id, _ = _admin_and_asset(conn)
            conn.execute(
                'DELETE FROM business_number_lock WHERE sequence_key=?',
                (_sequence_key('job_runs', 'run_no', 'JOB-'),),
            )

        def create(index: int) -> str:
            with db() as conn:
                number = _application.next_no(
                    conn, 'job_runs', 'run_no', 'JOB-', 1
                )
                stamp = now()
                conn.execute(
                    '''INSERT INTO job_runs(
                         run_no,trigger_source,status,actor_id,as_of,started_at
                       ) VALUES(?,?,'Running',?,?,?)''',
                    (
                        number,
                        f'number-race-{suffix}-{index}',
                        user_id,
                        date.today().isoformat(),
                        stamp,
                    ),
                )
                return number

        numbers = _run_workers(create)
        assert len(set(numbers)) == WORKERS
        with db() as conn:
            persisted = int(
                conn.execute(
                    'SELECT COUNT(*) FROM job_runs WHERE trigger_source LIKE ?',
                    (f'number-race-{suffix}-%',),
                ).fetchone()[0]
            )
        assert persisted == WORKERS


def test_concurrent_pm_number_allocation_commits_unique_real_records():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            _, asset_id = _admin_and_asset(conn)
            conn.execute(
                'DELETE FROM business_number_lock WHERE sequence_key=?',
                (_sequence_key('maintenance_plans', 'pm_no', 'PM-'),),
            )

        def create(index: int) -> str:
            with db() as conn:
                number = _application.next_no(
                    conn, 'maintenance_plans', 'pm_no', 'PM-', 1000
                )
                conn.execute(
                    '''INSERT INTO maintenance_plans(
                         pm_no,name,asset_id,trigger_type,interval_days,next_due,
                         priority,job_plan
                       ) VALUES(?,?,?,'Calendar',30,?,'Medium',?)''',
                    (
                        number,
                        f'PM number race {suffix}-{index}',
                        asset_id,
                        date.today().isoformat(),
                        'number allocator regression',
                    ),
                )
                return number

        numbers = _run_workers(create)
        assert len(set(numbers)) == WORKERS
        with db() as conn:
            persisted = int(
                conn.execute(
                    'SELECT COUNT(*) FROM maintenance_plans WHERE name LIKE ?',
                    (f'PM number race {suffix}-%',),
                ).fetchone()[0]
            )
        assert persisted == WORKERS


def test_different_number_families_use_distinct_lock_rows():
    with TestClient(app):
        job_key = _sequence_key('job_runs', 'run_no', 'JOB-')
        pm_key = _sequence_key('maintenance_plans', 'pm_no', 'PM-')
        with db() as conn:
            user_id, asset_id = _admin_and_asset(conn)
            conn.execute(
                'DELETE FROM business_number_lock WHERE sequence_key IN (?,?)',
                (job_key, pm_key),
            )
            job_no = _application.next_no(conn, 'job_runs', 'run_no', 'JOB-', 1)
            conn.execute(
                '''INSERT INTO job_runs(
                     run_no,trigger_source,status,actor_id,as_of,started_at
                   ) VALUES(?,?,'Running',?,?,?)''',
                (
                    job_no,
                    'distinct-lock-row-test',
                    user_id,
                    date.today().isoformat(),
                    now(),
                ),
            )
            pm_no = _application.next_no(
                conn, 'maintenance_plans', 'pm_no', 'PM-', 1000
            )
            conn.execute(
                '''INSERT INTO maintenance_plans(
                     pm_no,name,asset_id,trigger_type,interval_days,next_due,
                     priority,job_plan
                   ) VALUES(?,?,?,'Calendar',30,?,'Medium','lock-row-test')''',
                (
                    pm_no,
                    f'Distinct sequence {uuid.uuid4().hex[:8]}',
                    asset_id,
                    date.today().isoformat(),
                ),
            )
            rows = conn.execute(
                '''SELECT sequence_key FROM business_number_lock
                   WHERE sequence_key IN (?,?) ORDER BY sequence_key''',
                (job_key, pm_key),
            ).fetchall()
        assert {row['sequence_key'] for row in rows} == {job_key, pm_key}


def test_legacy_work_order_bootstrap_contract_remains_available():
    with TestClient(app):
        with db() as conn:
            legacy = conn.execute(
                'SELECT id,guard FROM work_order_number_lock WHERE id=1'
            ).fetchone()
            business_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='business_number_lock'"
            ).fetchone()
        assert legacy and int(legacy['id']) == 1
        assert business_table
