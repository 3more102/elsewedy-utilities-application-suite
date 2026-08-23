from __future__ import annotations

import threading
import time
import uuid

from app import application as _application
from app import main as main_module
from app.database import db, now
from app.scheduler_store import (
    SCHEDULER_TRIGGER,
    automation_loop_singleton,
    run_scheduled_automation_once,
)


WORKERS = 8


def _new_actor(conn, label: str) -> int:
    role = conn.execute("SELECT id FROM roles WHERE code='admin'").fetchone()
    assert role
    username = f'scheduler-{label}-{uuid.uuid4().hex[:10]}'
    created = conn.execute(
        '''INSERT INTO users(
             username,password_hash,full_name,role_id,active,created_at
           ) VALUES(?,?,?,?,1,?)''',
        (username, 'scheduler-test-only', username, int(role['id']), now()),
    )
    return int(created.lastrowid)


def _seed_run(conn, actor_id: int, status: str) -> str:
    run_no = f'JOB-SCHED-{status.upper()}-{uuid.uuid4().hex[:10]}'
    conn.execute(
        '''INSERT INTO job_runs(
             run_no,trigger_source,status,actor_id,as_of,started_at,finished_at,summary_json
           ) VALUES(?,?,?,?,?,?,?,?)''',
        (
            run_no,
            SCHEDULER_TRIGGER,
            status,
            actor_id,
            '2026-08-23',
            now(),
            now(),
            '{}',
        ),
    )
    return run_no


def test_distributed_scheduler_installer_replaces_legacy_loop():
    assert _application._automation_loop is automation_loop_singleton
    assert main_module._automation_loop is automation_loop_singleton
    assert getattr(_application.app.state, '_euas_distributed_scheduler_singleton', False) is True


def test_recent_successful_scheduler_run_suppresses_duplicate(monkeypatch):
    with db() as conn:
        actor_id = _new_actor(conn, 'recent')
        run_no = _seed_run(conn, actor_id, 'Succeeded')

    def should_not_run(*_args, **_kwargs):
        raise AssertionError('recent successful scheduler run must suppress duplicate execution')

    monkeypatch.setattr(_application, '_execute_automation', should_not_run)
    with db() as conn:
        result = run_scheduled_automation_once(conn, actor_id, interval_minutes=60)

    assert result == {
        'status': 'Skipped',
        'reason': 'recent_scheduler_success',
        'run_no': run_no,
        'as_of': '2026-08-23',
    }


def test_failed_scheduler_run_does_not_block_failover(monkeypatch):
    with db() as conn:
        actor_id = _new_actor(conn, 'failed')
        _seed_run(conn, actor_id, 'Failed')

    calls = []

    def fake_execute(conn, received_actor_id, trigger_source='manual', as_of=None):
        calls.append((received_actor_id, trigger_source, as_of))
        return {'status': 'Succeeded', 'run_no': 'JOB-FAILOVER-TEST'}

    monkeypatch.setattr(_application, '_execute_automation', fake_execute)
    with db() as conn:
        result = run_scheduled_automation_once(conn, actor_id, interval_minutes=60)

    assert result['status'] == 'Succeeded'
    assert calls == [(actor_id, SCHEDULER_TRIGGER, None)]


def test_concurrent_scheduler_replicas_execute_payload_once(monkeypatch):
    with db() as conn:
        actor_id = _new_actor(conn, 'race')

    call_count = 0
    call_lock = threading.Lock()
    errors: list[BaseException] = []
    results: list[dict] = []
    barrier = threading.Barrier(WORKERS)
    run_prefix = f'JOB-SCHED-RACE-{uuid.uuid4().hex[:10]}'

    def fake_execute(conn, received_actor_id, trigger_source='manual', as_of=None):
        nonlocal call_count
        assert received_actor_id == actor_id
        assert trigger_source == SCHEDULER_TRIGGER
        with call_lock:
            call_count += 1
            sequence = call_count
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
        time.sleep(0.08)
        return {'status': 'Succeeded', 'run_no': run_no}

    monkeypatch.setattr(_application, '_execute_automation', fake_execute)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            with db() as conn:
                results.append(
                    run_scheduled_automation_once(conn, actor_id, interval_minutes=60)
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == WORKERS
    assert call_count == 1
    assert sum(result.get('status') == 'Succeeded' for result in results) == 1
    assert sum(result.get('reason') == 'recent_scheduler_success' for result in results) == WORKERS - 1

    with db() as conn:
        persisted = int(
            conn.execute(
                'SELECT COUNT(*) FROM job_runs WHERE run_no LIKE ?',
                (run_prefix + '%',),
            ).fetchone()[0]
        )
    assert persisted == 1
