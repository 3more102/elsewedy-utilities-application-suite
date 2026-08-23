from datetime import datetime, timedelta
from threading import Barrier, Thread

from fastapi.testclient import TestClient

from app.main import app
from apps.events import (
    claim_outbox_event,
    emit_event,
    enqueue_outbox_dispatch_job,
    enqueue_outbox_dispatch_jobs,
    make_event_dispatch_handler,
    recover_stuck_processing,
)
from apps.jobs import JobHandlerRegistry, WorkerRuntime
from core.database import db


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ok_urlopen(*args, **kwargs):
    return _Response()


def _create_event(conn, suffix: str):
    return emit_event(conn, 'test.worker.dispatch', 'test', suffix, {'suffix': suffix}, correlation_id=f'corr-{suffix}')


def test_outbox_jobs_are_deduplicated_and_worker_delivers_with_history():
    with TestClient(app):
        with db() as conn:
            event = _create_event(conn, 'jobs-delivery')
            first = enqueue_outbox_dispatch_job(conn, event['id'], max_attempts=3)
            second = enqueue_outbox_dispatch_job(conn, event['id'], max_attempts=3)
            assert first and first['idempotent_replay'] is False
            assert second and second['job_id'] == first['job_id'] and second['idempotent_replay'] is True

        registry = JobHandlerRegistry()
        registry.register(
            'event.dispatch',
            make_event_dispatch_handler(
                webhook_url='https://integration.example.test/events',
                webhook_secret='secret',
                max_attempts=3,
                app_version='test',
                urlopen=_ok_urlopen,
            ),
        )
        result = WorkerRuntime('event-worker-success', registry).run_once()
        assert result and result['status'] == 'Succeeded'

        with db() as conn:
            row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (event['id'],)).fetchone()
            assert row['status'] == 'Delivered' and row['attempts'] == 1
            assert row['lease_owner'] is None and row['correlation_id'] == 'corr-jobs-delivery'
            attempt = conn.execute(
                'SELECT * FROM event_delivery_attempts WHERE event_id=? ORDER BY attempt_no',
                (event['id'],),
            ).fetchone()
            assert attempt['status'] == 'Delivered' and attempt['finished_at']

        # Re-running the same handler payload after delivery is a no-op: the
        # remote side effect is not executed twice and attempts stay stable.
        handler = registry.resolve('event.dispatch')
        class Context:
            worker_id = 'event-worker-success'
        replay = handler({'event_id': event['id'], 'event_no': event['event_no']}, Context())
        assert replay['status'] == 'Delivered' and replay['idempotent'] is True
        with db() as conn:
            row = conn.execute('SELECT attempts FROM event_outbox WHERE id=?', (event['id'],)).fetchone()
            assert row['attempts'] == 1


def test_two_dispatchers_cannot_claim_the_same_event():
    with TestClient(app):
        with db() as conn:
            event = _create_event(conn, 'dispatch-race')
        barrier = Barrier(2)
        claimed = []
        errors = []

        def claim(worker_id):
            try:
                barrier.wait()
                with db() as conn:
                    row = claim_outbox_event(conn, event['id'], worker_id=worker_id, max_attempts=3, lease_seconds=20)
                    claimed.append(row['id'] if row else None)
            except Exception as exc:
                errors.append(exc)

        threads = [Thread(target=claim, args=('dispatcher-a',)), Thread(target=claim, args=('dispatcher-b',))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert claimed.count(event['id']) == 1


def test_stuck_processing_lease_recovers_with_evidence():
    with TestClient(app):
        with db() as conn:
            event = _create_event(conn, 'dispatcher-crash')
            claimed = claim_outbox_event(conn, event['id'], worker_id='crashed-dispatcher', max_attempts=3, lease_seconds=20)
            assert claimed and claimed['status'] == 'Processing'
            expired = (datetime.now() - timedelta(seconds=1)).isoformat(timespec='seconds')
            conn.execute('UPDATE event_outbox SET lease_expires_at=? WHERE id=?', (expired, event['id']))
            recovered = recover_stuck_processing(conn, max_attempts=3)
            assert recovered == {'recovered': 1, 'failed': 1, 'dead_lettered': 0}
            row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (event['id'],)).fetchone()
            assert row['status'] == 'Failed' and row['lease_owner'] is None
            attempt = conn.execute(
                'SELECT * FROM event_delivery_attempts WHERE event_id=? AND attempt_no=1',
                (event['id'],),
            ).fetchone()
            assert attempt['status'] == 'LeaseExpired' and attempt['finished_at']
