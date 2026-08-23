from __future__ import annotations

import json
import threading
import time
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app import main as main_module
from app import outbox_store
from app.authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY
from app.database import db, now
from app.main import app
from app.outbox_store import (
    OUTBOX_RETRY_ROLES,
    _defer_outbox,
    execute_automation_postcommit,
    process_outbox_atomic,
    retry_outbox_event_atomic,
)


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_event(conn, suffix: str, status: str = 'Pending', attempts: int = 0) -> tuple[int, str]:
    event_no = f'EVT-OUT-{suffix}'
    created = conn.execute(
        '''INSERT INTO event_outbox(
             event_no,event_type,aggregate_type,aggregate_id,payload_json,
             status,attempts,created_at,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            event_no,
            'test.outbox.atomicity',
            'test',
            suffix,
            '{"ok":true}',
            status,
            attempts,
            now(),
            'seed failure' if status == 'Failed' else '',
        ),
    )
    return int(created.lastrowid), event_no


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=25)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == workers
    return results


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event_header(request) -> str | None:
    return request.get_header('X-euas-event-id') or request.get_header('X-EUAS-Event-ID')


def test_concurrent_processors_send_target_webhook_once(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, event_no = _seed_event(conn, suffix)

        calls: list[str | None] = []
        calls_lock = threading.Lock()

        def fake_urlopen(request, timeout=5):
            with calls_lock:
                calls.append(_event_header(request))
            if _event_header(request) == event_no:
                time.sleep(0.08)
            assert timeout == 5
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)

        def process() -> dict:
            with db() as conn:
                return process_outbox_atomic(conn)

        _race(process)
        assert calls.count(event_no) == 1

        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts,processed_at,last_error FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert event['status'] == 'Delivered'
        assert int(event['attempts']) == 1
        assert event['processed_at']
        assert event['last_error'] == ''


def test_retry_waiting_on_inflight_delivery_does_not_requeue_it(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            event_id, event_no = _seed_event(conn, suffix)

        entered = threading.Event()
        release = threading.Event()
        target_calls = 0
        errors: list[BaseException] = []
        retry_result: list[dict] = []

        def fake_urlopen(request, timeout=5):
            nonlocal target_calls
            if _event_header(request) == event_no:
                target_calls += 1
                entered.set()
                assert release.wait(timeout=10)
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)

        def deliver() -> None:
            try:
                with db() as conn:
                    process_outbox_atomic(conn)
            except BaseException as exc:
                errors.append(exc)

        def retry() -> None:
            try:
                with db() as conn:
                    retry_result.append(retry_outbox_event_atomic(conn, event_id, user))
            except BaseException as exc:
                errors.append(exc)

        delivery_thread = threading.Thread(target=deliver)
        delivery_thread.start()
        assert entered.wait(timeout=10)

        retry_thread = threading.Thread(target=retry)
        retry_thread.start()
        time.sleep(0.08)
        release.set()

        delivery_thread.join(timeout=20)
        retry_thread.join(timeout=20)
        assert not delivery_thread.is_alive()
        assert not retry_thread.is_alive()
        assert errors == []
        assert target_calls == 1
        assert retry_result == [{'ok': True, 'event_no': event_no}]

        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Integration Events' AND action='RETRY'
                         AND record_id=?""",
                    (event_no,),
                ).fetchone()[0]
            )
        assert event['status'] == 'Delivered'
        assert int(event['attempts']) == 1
        assert audits == 0


def test_failed_event_retry_is_idempotent_and_audited_once():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            event_id, event_no = _seed_event(conn, suffix, status='Failed', attempts=2)

        with db() as conn:
            first = retry_outbox_event_atomic(conn, event_id, user)
        with db() as conn:
            second = retry_outbox_event_atomic(conn, event_id, user)

        assert first == {'ok': True, 'event_no': event_no}
        assert second == first
        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts,processed_at,last_error FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Integration Events' AND action='RETRY'
                         AND record_id=?""",
                    (event_no,),
                ).fetchone()[0]
            )
        assert event['status'] == 'Pending'
        assert int(event['attempts']) == 2
        assert event['processed_at'] is None
        assert event['last_error'] == ''
        assert audits == 1


def test_exhausted_event_retry_resets_budget_and_delivers(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            event_id, event_no = _seed_event(
                conn, suffix, status='Failed', attempts=_application.OUTBOX_MAX_ATTEMPTS
            )

        # An attempt-exhausted event is invisible to the automated processor.
        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', '')
        with db() as conn:
            process_outbox_atomic(conn)
        with db() as conn:
            untouched = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?', (event_id,)
            ).fetchone()
        assert untouched['status'] == 'Failed'
        assert int(untouched['attempts']) == _application.OUTBOX_MAX_ATTEMPTS

        with db() as conn:
            result = retry_outbox_event_atomic(conn, event_id, user)
        assert result == {'ok': True, 'event_no': event_no}

        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts,processed_at,last_error FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Integration Events' AND action='RETRY'
                         AND record_id=?""",
                    (event_no,),
                ).fetchone()[0]
            )
        assert event['status'] == 'Pending'
        assert int(event['attempts']) == 0
        assert event['processed_at'] is None
        assert audits == 1

        # The fresh budget makes the event eligible again and it delivers once.
        calls: list[str | None] = []

        def fake_urlopen(request, timeout=5):
            calls.append(_event_header(request))
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)
        with db() as conn:
            processed = process_outbox_atomic(conn)
        assert processed['delivered'] >= 1
        assert calls.count(event_no) == 1

        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert event['status'] == 'Delivered'
        assert int(event['attempts']) == 1


def test_automation_dispatches_event_only_after_business_commit(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        event_no = f'EVT-COMMIT-{suffix}'
        run_no = f'JOB-COMMIT-{suffix}'
        with db() as conn:
            user = _admin(conn)

        def fake_legacy(conn, actor_id, trigger_source='manual', as_of=None):
            run = conn.execute(
                '''INSERT INTO job_runs(
                     run_no,trigger_source,status,actor_id,as_of,started_at,
                     finished_at,summary_json
                   ) VALUES(?,?,'Succeeded',?,?,?, ?,?)''',
                (
                    run_no,
                    trigger_source,
                    actor_id,
                    '2026-08-23',
                    now(),
                    now(),
                    json.dumps({'outbox_delivered': 0, 'outbox_failed': 0, 'outbox_skipped': 0}),
                ),
            )
            conn.execute(
                '''INSERT INTO event_outbox(
                     event_no,event_type,aggregate_type,aggregate_id,payload_json,
                     status,attempts,created_at,last_error
                   ) VALUES(?,?,?,?,?,'Pending',0,?,'')''',
                (event_no, 'test.commit.boundary', 'test', suffix, '{"ok":true}', now()),
            )
            return {
                'id': int(run.lastrowid),
                'run_no': run_no,
                'status': 'Succeeded',
                'as_of': '2026-08-23',
                'summary': {'outbox_delivered': 0, 'outbox_failed': 0, 'outbox_skipped': 0},
            }

        observed_committed = False

        def fake_urlopen(request, timeout=5):
            nonlocal observed_committed
            if _event_header(request) == event_no:
                with db() as observer:
                    visible = observer.execute(
                        'SELECT status FROM event_outbox WHERE event_no=?',
                        (event_no,),
                    ).fetchone()
                observed_committed = bool(visible and visible['status'] == 'Pending')
            return _Response()

        monkeypatch.setattr(outbox_store, '_legacy_execute_automation', fake_legacy)
        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)

        with db() as conn:
            result = execute_automation_postcommit(conn, user['id'])

        assert observed_committed is True
        assert result['status'] == 'Succeeded'
        assert result['summary']['outbox_delivered'] >= 1
        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE event_no=?',
                (event_no,),
            ).fetchone()
            run = conn.execute(
                'SELECT summary_json FROM job_runs WHERE run_no=?',
                (run_no,),
            ).fetchone()
        assert event['status'] == 'Delivered'
        assert int(event['attempts']) == 1
        persisted_summary = json.loads(run['summary_json'])
        assert persisted_summary['outbox_delivered'] >= 1


def test_processor_and_retry_route_are_installed_without_authorization_widening():
    with TestClient(app):
        assert _application._process_outbox is _defer_outbox
        assert _application._execute_automation is execute_automation_postcommit
        assert main_module._process_outbox is _defer_outbox
        assert main_module._execute_automation is execute_automation_postcommit
        route_key = ('POST', '/api/events/outbox/{event_id}/retry')
        assert ROUTE_PERMISSION_OVERLAY[route_key] == 'integrations.outbox.retry'
        assert tuple(PERMISSION_CATALOG['integrations.outbox.retry'][1]) == OUTBOX_RETRY_ROLES
        routes = [
            route
            for route in app.router.routes
            if getattr(route, 'path', None) == route_key[1]
            and route_key[0] in set(getattr(route, 'methods', set()) or set())
        ]
        assert len(routes) == 1
