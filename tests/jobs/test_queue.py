from datetime import datetime, timedelta
from threading import Barrier, Thread

import pytest
from fastapi.testclient import TestClient

from app.main import app
from apps.jobs import (
    JobHandlerRegistry,
    WorkerRuntime,
    cancel_job,
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
    get_job,
    heartbeat_worker,
    recover_expired_leases,
    register_worker,
    renew_lease,
    replay_job,
    start_job,
)
from core.database import db


@pytest.fixture(autouse=True)
def _isolated_job_tables():
    # Jobs are persistent by design; isolate each regression so queue ordering is
    # asserted against only the records created by that test.
    with TestClient(app):
        with db() as conn:
            conn.execute('DELETE FROM job_leases')
            conn.execute('DELETE FROM job_attempts')
            conn.execute('DELETE FROM jobs')
            conn.execute('DELETE FROM workers')
    yield
    with db() as conn:
        conn.execute('DELETE FROM job_leases')
        conn.execute('DELETE FROM job_attempts')
        conn.execute('DELETE FROM jobs')
        conn.execute('DELETE FROM workers')


def _worker(worker_id):
    with db() as conn:
        register_worker(conn, worker_id=worker_id)


def test_enqueue_deduplication_and_priority_ordering():
    with TestClient(app):
        with db() as conn:
            register_worker(conn, worker_id='jobs-priority-worker')
            low = enqueue_job(conn, job_type='test.low', payload={'n': 1}, priority=1, deduplication_key='jobs-low')
            high = enqueue_job(conn, job_type='test.high', payload={'n': 2}, priority=100, deduplication_key='jobs-high')
            replay = enqueue_job(conn, job_type='test.low', payload={'n': 99}, priority=1, deduplication_key='jobs-low')
            assert replay['job_id'] == low['job_id'] and replay['idempotent_replay'] is True
            claimed = claim_next_job(conn, worker_id='jobs-priority-worker')
            assert claimed['job_id'] == high['job_id']


def test_worker_lease_heartbeat_success_and_history():
    with TestClient(app):
        with db() as conn:
            register_worker(conn, worker_id='jobs-success-worker')
            job = enqueue_job(conn, job_type='test.success', payload={'ok': True}, deduplication_key='jobs-success')
            claimed = claim_next_job(conn, worker_id='jobs-success-worker', lease_seconds=30)
            assert claimed['job_id'] == job['job_id'] and claimed['status'] == 'Leased'
            started = start_job(conn, job_id=claimed['id'], worker_id='jobs-success-worker')
            assert started['status'] == 'Running'
            before = started['lease_expires_at']
            renewed = renew_lease(conn, job_id=claimed['id'], worker_id='jobs-success-worker', lease_seconds=120)
            assert renewed['lease_expires_at'] >= before
            heartbeat = heartbeat_worker(conn, 'jobs-success-worker')
            assert heartbeat['status'] == 'Active'
            done = complete_job(conn, job_id=claimed['id'], worker_id='jobs-success-worker')
            assert done['status'] == 'Succeeded' and done['lease_owner'] is None
            attempt = conn.execute('SELECT * FROM job_attempts WHERE job_id=?', (claimed['id'],)).fetchone()
            assert attempt['status'] == 'Succeeded' and attempt['finished_at']


def test_failure_retry_exhaustion_and_manual_replay():
    with TestClient(app):
        with db() as conn:
            register_worker(conn, worker_id='jobs-fail-worker')
            job = enqueue_job(conn, job_type='test.fail', max_attempts=2, deduplication_key='jobs-fail')
            first = claim_next_job(conn, worker_id='jobs-fail-worker')
            start_job(conn, job_id=first['id'], worker_id='jobs-fail-worker')
            retry = fail_job(conn, job_id=first['id'], worker_id='jobs-fail-worker', error='boom', base_backoff_seconds=0)
            assert retry['status'] == 'RetryScheduled' and retry['attempt_count'] == 1
            second = claim_next_job(conn, worker_id='jobs-fail-worker')
            start_job(conn, job_id=second['id'], worker_id='jobs-fail-worker')
            dead = fail_job(conn, job_id=second['id'], worker_id='jobs-fail-worker', error='boom again', base_backoff_seconds=0)
            assert dead['status'] == 'DeadLetter' and dead['attempt_count'] == 2
            replayed = replay_job(conn, dead['id'])
            assert replayed['status'] == 'Pending' and replayed['attempt_count'] == 0 and replayed['last_error'] == ''
            replay_claim = claim_next_job(conn, worker_id='jobs-fail-worker')
            assert replay_claim['id'] == dead['id'] and replay_claim['attempt_count'] == 1
            start_job(conn, job_id=replay_claim['id'], worker_id='jobs-fail-worker')
            replay_done = complete_job(conn, job_id=replay_claim['id'], worker_id='jobs-fail-worker')
            assert replay_done['status'] == 'Succeeded'
            attempts = conn.execute('SELECT attempt_no FROM job_attempts WHERE job_id=? ORDER BY attempt_no', (dead['id'],)).fetchall()
            assert [row['attempt_no'] for row in attempts] == [1, 2, 3]


def test_expired_lease_recovers_and_cancelled_job_never_claims():
    with TestClient(app):
        with db() as conn:
            register_worker(conn, worker_id='jobs-expiry-worker')
            job = enqueue_job(conn, job_type='test.expire', deduplication_key='jobs-expire')
            claimed = claim_next_job(conn, worker_id='jobs-expiry-worker', lease_seconds=30)
            expired = (datetime.now() - timedelta(seconds=1)).isoformat(timespec='seconds')
            conn.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?', (expired, claimed['id']))
            recovered = recover_expired_leases(conn)
            assert recovered['expired'] == 1
            assert get_job(conn, job['id'])['status'] == 'RetryScheduled'

            cancelled = enqueue_job(conn, job_type='test.cancel', priority=999, deduplication_key='jobs-cancel')
            cancel_job(conn, cancelled['id'])
            next_job = claim_next_job(conn, worker_id='jobs-expiry-worker')
            assert next_job is not None and next_job['job_id'] == job['job_id']


def test_two_workers_cannot_claim_same_job():
    with TestClient(app):
        _worker('jobs-race-a')
        _worker('jobs-race-b')
        with db() as conn:
            job = enqueue_job(conn, job_type='test.race', priority=500, deduplication_key='jobs-race')
        barrier = Barrier(2)
        results = []
        errors = []

        def claim(worker_id):
            try:
                barrier.wait()
                with db() as conn:
                    result = claim_next_job(conn, worker_id=worker_id)
                    results.append(result['job_id'] if result else None)
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [Thread(target=claim, args=('jobs-race-a',)), Thread(target=claim, args=('jobs-race-b',))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        assert not errors
        assert results.count(job['job_id']) == 1


def test_worker_runtime_executes_registered_handler():
    with TestClient(app):
        seen = []
        registry = JobHandlerRegistry()
        registry.register('test.runtime', lambda payload, ctx: seen.append((payload['value'], ctx.correlation_id)))
        with db() as conn:
            job = enqueue_job(conn, job_type='test.runtime', payload={'value': 7}, priority=1000, deduplication_key='jobs-runtime')
        result = WorkerRuntime('jobs-runtime-worker', registry).run_once()
        assert result['status'] == 'Succeeded'
        assert seen and seen[0][0] == 7 and seen[0][1]
        with db() as conn:
            assert get_job(conn, job['id'])['status'] == 'Succeeded'
