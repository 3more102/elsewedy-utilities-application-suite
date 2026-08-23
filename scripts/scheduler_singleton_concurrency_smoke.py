from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.database import db, now
from app.main import app  # noqa: F401 - installs production hardening composition
from app.scheduler_store import SCHEDULER_TRIGGER, run_scheduled_automation_once


WORKERS = 12


def _new_actor(label: str) -> int:
    with db() as conn:
        role = conn.execute("SELECT id FROM roles WHERE code='admin'").fetchone()
        if not role:
            raise RuntimeError('scheduler smoke requires seeded admin role')
        username = f'scheduler-pg-{label}-{uuid.uuid4().hex[:10]}'
        created = conn.execute(
            '''INSERT INTO users(
                 username,password_hash,full_name,role_id,active,created_at
               ) VALUES(?,?,?,?,1,?)''',
            (username, 'scheduler-smoke-only', username, int(role['id']), now()),
        )
        return int(created.lastrowid)


def _parallel(fn):
    barrier = threading.Barrier(WORKERS)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(fn(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('scheduler worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'scheduler concurrency worker failed: {errors!r}')
    return results


def _singleton_race() -> None:
    actor_id = _new_actor('race')
    run_prefix = f'JOB-SCHED-PG-{uuid.uuid4().hex[:10]}'
    calls = 0
    calls_lock = threading.Lock()

    def fake_execute(conn, received_actor_id, trigger_source='manual', as_of=None):
        nonlocal calls
        if received_actor_id != actor_id or trigger_source != SCHEDULER_TRIGGER:
            raise RuntimeError('scheduler singleton forwarded unexpected execution identity')
        with calls_lock:
            calls += 1
            sequence = calls
        run_no = f'{run_prefix}-{sequence}'
        conn.execute(
            '''INSERT INTO job_runs(
                 run_no,trigger_source,status,actor_id,as_of,started_at,finished_at,summary_json
               ) VALUES(?,?,'Succeeded',?,?,?,?,?)''',
            (
                run_no,
                trigger_source,
                received_actor_id,
                '2026-08-23',
                now(),
                now(),
                '{}',
            ),
        )
        time.sleep(0.12)
        return {'status': 'Succeeded', 'run_no': run_no}

    original_execute = _application._execute_automation
    _application._execute_automation = fake_execute
    try:
        def attempt(_index):
            with db() as conn:
                return run_scheduled_automation_once(conn, actor_id, interval_minutes=60)

        results = _parallel(attempt)
    finally:
        _application._execute_automation = original_execute

    if calls != 1:
        raise RuntimeError(f'expected one scheduler payload execution, got {calls}')
    if sum(result.get('status') == 'Succeeded' for result in results) != 1:
        raise RuntimeError(f'expected one scheduler winner, got {results!r}')
    if sum(result.get('reason') == 'recent_scheduler_success' for result in results) != WORKERS - 1:
        raise RuntimeError(f'expected {WORKERS - 1} duplicate suppressions, got {results!r}')

    with db() as conn:
        persisted = int(
            conn.execute(
                'SELECT COUNT(*) FROM job_runs WHERE run_no LIKE ?',
                (run_prefix + '%',),
            ).fetchone()[0]
        )
    if persisted != 1:
        raise RuntimeError(f'expected one persisted scheduler run, got {persisted}')


def _failed_run_allows_failover() -> None:
    actor_id = _new_actor('failover')
    successful_run = f'JOB-SCHED-PG-SUCCESS-{uuid.uuid4().hex[:10]}'
    failed_run = f'JOB-SCHED-PG-FAILED-{uuid.uuid4().hex[:10]}'
    with db() as conn:
        # Reproduce the generation bug: an older recent success must not mask a
        # newer failed attempt. The latest generation controls failover.
        conn.execute(
            '''INSERT INTO job_runs(
                 run_no,trigger_source,status,actor_id,as_of,started_at,finished_at,summary_json
               ) VALUES(?,?,'Succeeded',?,?,?,?,?)''',
            (
                successful_run,
                SCHEDULER_TRIGGER,
                actor_id,
                '2026-08-23',
                now(),
                now(),
                '{}',
            ),
        )
        conn.execute(
            '''INSERT INTO job_runs(
                 run_no,trigger_source,status,actor_id,as_of,started_at,finished_at,summary_json,error_message
               ) VALUES(?,?,'Failed',?,?,?,?,?,?)''',
            (
                failed_run,
                SCHEDULER_TRIGGER,
                actor_id,
                '2026-08-23',
                now(),
                now(),
                '{}',
                'expected failover fixture',
            ),
        )

    calls = 0

    def fake_execute(conn, received_actor_id, trigger_source='manual', as_of=None):
        nonlocal calls
        calls += 1
        return {'status': 'Succeeded', 'run_no': 'JOB-SCHED-PG-FAILOVER'}

    original_execute = _application._execute_automation
    _application._execute_automation = fake_execute
    try:
        with db() as conn:
            result = run_scheduled_automation_once(conn, actor_id, interval_minutes=60)
    finally:
        _application._execute_automation = original_execute

    if calls != 1 or result.get('status') != 'Succeeded':
        raise RuntimeError(
            'newer failed scheduler generation was hidden by older success: '
            f'calls={calls} result={result!r}'
        )


def main() -> None:
    _singleton_race()
    _failed_run_allows_failover()
    print(
        'scheduler singleton concurrency smoke: PASS '
        'payload_executions=1 duplicate_suppressions=11 latest_failed_generation_failover=allowed workers=12'
    )


if __name__ == '__main__':
    main()
