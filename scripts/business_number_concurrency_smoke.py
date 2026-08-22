from __future__ import annotations

import sys
import threading
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - install production next_no composition
from app.work_order_number_startup import initialize_work_order_number_support
from app.work_order_number_store import _sequence_key


WORKERS = 12


def _seed_context():
    with db() as conn:
        ensure_audit_chain_lock(conn)
        initialize_work_order_number_support(conn)
        user = conn.execute(
            """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
               WHERE u.username='omar'"""
        ).fetchone()
        asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
        if not user or not asset:
            raise RuntimeError('business-number smoke requires seeded admin and asset')
        conn.execute(
            '''DELETE FROM business_number_lock
               WHERE sequence_key IN (?,?,?)''',
            (
                _sequence_key('job_runs', 'run_no', 'JOB-'),
                _sequence_key('maintenance_plans', 'pm_no', 'PM-'),
                _sequence_key('approval_requests', 'approval_no', 'APR-'),
            ),
        )
        return int(user['id']), int(asset['id'])


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    values: list[str] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            values.append(str(operation(index)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('business-number worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'business-number worker failed: {errors!r}')
    if len(values) != workers:
        raise RuntimeError(
            f'business-number race returned {len(values)} values for {workers} workers'
        )
    if len(set(values)) != workers:
        raise RuntimeError(f'business-number allocator returned duplicates: {values!r}')
    return values


def main() -> None:
    user_id, asset_id = _seed_context()
    suffix = uuid.uuid4().hex[:10]

    def create_job(index: int) -> str:
        with db() as conn:
            number = _application.next_no(conn, 'job_runs', 'run_no', 'JOB-', 1)
            conn.execute(
                '''INSERT INTO job_runs(
                     run_no,trigger_source,status,actor_id,as_of,started_at
                   ) VALUES(?,?,'Running',?,?,?)''',
                (
                    number,
                    f'pg-number-race-{suffix}-{index}',
                    user_id,
                    date.today().isoformat(),
                    now(),
                ),
            )
            return number

    job_numbers = _race(create_job)
    with db() as conn:
        job_count = int(
            conn.execute(
                'SELECT COUNT(*) FROM job_runs WHERE trigger_source LIKE ?',
                (f'pg-number-race-{suffix}-%',),
            ).fetchone()[0]
        )
    if job_count != WORKERS:
        raise RuntimeError(
            f'JOB sequence expected {WORKERS} commits, found {job_count}'
        )

    def create_pm(index: int) -> str:
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
                    f'PG number race {suffix}-{index}',
                    asset_id,
                    date.today().isoformat(),
                    'business-number concurrency smoke',
                ),
            )
            return number

    pm_numbers = _race(create_pm)
    with db() as conn:
        pm_count = int(
            conn.execute(
                'SELECT COUNT(*) FROM maintenance_plans WHERE name LIKE ?',
                (f'PG number race {suffix}-%',),
            ).fetchone()[0]
        )
    if pm_count != WORKERS:
        raise RuntimeError(
            f'PM sequence expected {WORKERS} commits, found {pm_count}'
        )

    def create_approval(index: int) -> str:
        with db() as conn:
            approval = _application.create_approval(
                conn,
                'CI Number Allocation',
                'sequence_test',
                9_000_000 + index,
                f'SEQ-{suffix}-{index}',
                f'Approval number race {suffix}-{index}',
                user_id,
                assigned_role='planner',
            )
            return approval['approval_no']

    approval_numbers = _race(create_approval)
    with db() as conn:
        approval_count = int(
            conn.execute(
                '''SELECT COUNT(*) FROM approval_requests
                   WHERE module='CI Number Allocation' AND record_code LIKE ?''',
                (f'SEQ-{suffix}-%',),
            ).fetchone()[0]
        )
        sequence_rows = {
            row['sequence_key']
            for row in conn.execute(
                '''SELECT sequence_key FROM business_number_lock
                   WHERE sequence_key IN (?,?,?)''',
                (
                    _sequence_key('job_runs', 'run_no', 'JOB-'),
                    _sequence_key('maintenance_plans', 'pm_no', 'PM-'),
                    _sequence_key('approval_requests', 'approval_no', 'APR-'),
                ),
            ).fetchall()
        }
    if approval_count != WORKERS:
        raise RuntimeError(
            f'APR sequence expected {WORKERS} commits, found {approval_count}'
        )
    expected_keys = {
        _sequence_key('job_runs', 'run_no', 'JOB-'),
        _sequence_key('maintenance_plans', 'pm_no', 'PM-'),
        _sequence_key('approval_requests', 'approval_no', 'APR-'),
    }
    if sequence_rows != expected_keys:
        raise RuntimeError(
            f'business-number lock rows are incomplete: {sequence_rows!r}'
        )

    print(
        'business number concurrency smoke: PASS '
        f'job={len(job_numbers)} pm={len(pm_numbers)} approvals={len(approval_numbers)} '
        'per_sequence_rows=3'
    )


if __name__ == '__main__':
    main()
