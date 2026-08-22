from __future__ import annotations

import threading
import time
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY
from app.database import db, now
from app.main import app
from app.outbox_store import (
    OUTBOX_RETRY_ROLES,
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


def test_concurrent_processors_send_one_webhook_for_one_event(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, event_no = _seed_event(conn, suffix)

        calls = 0
        calls_lock = threading.Lock()

        def fake_urlopen(request, timeout=5):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.08)
            assert request.headers.get('X-euas-event-id') == event_no
            assert timeout == 5
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)

        def process() -> dict:
            with db() as conn:
                return process_outbox_atomic(conn)

        results = _race(process)
        assert calls == 1
        assert sum(int(result['delivered']) for result in results) == 1
        assert sum(int(result['failed']) for result in results) == 0

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
        calls = 0
        errors: list[BaseException] = []
        retry_result: list[dict] = []

        def fake_urlopen(request, timeout=5):
            nonlocal calls
            calls += 1
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
        assert calls == 1
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


def test_processor_and_retry_route_are_installed_without_authorization_widening():
    with TestClient(app):
        assert _application._process_outbox is process_outbox_atomic
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
