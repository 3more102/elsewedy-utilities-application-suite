from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app import outbox_store
from app.database import db, now
from app.main import app  # noqa: F401 - installs the production compatibility composition
from app.outbox_store import (
    _claim_delivery,
    execute_automation_postcommit,
    process_outbox_atomic,
    retry_outbox_event_atomic,
)


WORKERS = 12


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event_header(request) -> str | None:
    return request.get_header('X-euas-event-id') or request.get_header('X-EUAS-Event-ID')


def _admin() -> dict:
    with db() as conn:
        row = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not row:
            raise RuntimeError('outbox smoke requires seeded admin user')
        return dict(row)


def _quiesce_existing() -> None:
    # Earlier PostgreSQL gates intentionally create outbox evidence. Isolate this
    # smoke's delivery assertions without deleting any records.
    with db() as conn:
        conn.execute(
            """UPDATE event_outbox
               SET status='Skipped',processed_at=?,last_error='CI outbox smoke isolation'
               WHERE status IN ('Pending','Failed')""",
            (now(),),
        )


def _seed_event(status: str = 'Pending', attempts: int = 0) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:12]
    event_no = f'EVT-OUT-PG-{suffix}'
    with db() as conn:
        created = conn.execute(
            '''INSERT INTO event_outbox(
                 event_no,event_type,aggregate_type,aggregate_id,payload_json,
                 status,attempts,created_at,last_error
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                event_no,
                'test.outbox.postgresql',
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


def _parallel(workers, fn, timeout=45):
    barrier = threading.Barrier(workers)
    results = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(fn(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('outbox worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'outbox concurrency worker failed: {errors!r}')
    return results


def _claim_generation_once() -> None:
    _quiesce_existing()
    event_id, _ = _seed_event()
    with db() as conn:
        row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
        snapshot = dict(row)

    def claim(_index):
        with db() as conn:
            claimed = _claim_delivery(conn, snapshot)
            return claimed is not None

    winners = _parallel(WORKERS, claim)
    if sum(bool(value) for value in winners) != 1:
        raise RuntimeError(f'expected one exact-generation claim, got {winners!r}')
    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()
    if event['status'] != 'Pending' or int(event['attempts']) != 1:
        raise RuntimeError(f'claim generation persisted unexpected state: {dict(event)!r}')


def _one_external_send() -> None:
    _quiesce_existing()
    event_id, event_no = _seed_event()
    calls = 0
    calls_lock = threading.Lock()

    def fake_urlopen(request, timeout=5):
        nonlocal calls
        if _event_header(request) == event_no:
            with calls_lock:
                calls += 1
            time.sleep(0.12)
        return _Response()

    original_url = _application.EVENT_WEBHOOK_URL
    original_open = _application.urllib_request.urlopen
    _application.EVENT_WEBHOOK_URL = 'https://example.invalid/euas'
    _application.urllib_request.urlopen = fake_urlopen
    try:
        def process(_index):
            with db() as conn:
                return process_outbox_atomic(conn)

        _parallel(WORKERS, process)
    finally:
        _application.EVENT_WEBHOOK_URL = original_url
        _application.urllib_request.urlopen = original_open

    if calls != 1:
        raise RuntimeError(f'expected one target external send, got {calls}')
    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts,processed_at FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()
    if event['status'] != 'Delivered' or int(event['attempts']) != 1 or not event['processed_at']:
        raise RuntimeError(f'delivery state invalid: {dict(event)!r}')


def _retry_is_idempotent() -> None:
    _quiesce_existing()
    user = _admin()
    event_id, event_no = _seed_event(status='Failed', attempts=2)

    def retry(_index):
        with db() as conn:
            return retry_outbox_event_atomic(conn, event_id, user)

    results = _parallel(WORKERS, retry)
    if any(result != {'ok': True, 'event_no': event_no} for result in results):
        raise RuntimeError(f'retry response changed: {results!r}')
    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts,last_error FROM event_outbox WHERE id=?',
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
    if event['status'] != 'Pending' or int(event['attempts']) != 2 or event['last_error'] != '':
        raise RuntimeError(f'retry state invalid: {dict(event)!r}')
    if audits != 1:
        raise RuntimeError(f'expected one retry audit, got {audits}')


def _retry_exhausted_resets_budget_once() -> None:
    _quiesce_existing()
    user = _admin()
    max_attempts = int(_application.OUTBOX_MAX_ATTEMPTS)
    event_id, event_no = _seed_event(status='Failed', attempts=max_attempts)

    def retry(_index):
        with db() as conn:
            return retry_outbox_event_atomic(conn, event_id, user)

    results = _parallel(WORKERS, retry)
    if any(result != {'ok': True, 'event_no': event_no} for result in results):
        raise RuntimeError(f'exhausted retry response changed: {results!r}')
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
    if event['status'] != 'Pending' or int(event['attempts']) != 0:
        raise RuntimeError(f'exhausted retry state invalid: {dict(event)!r}')
    if audits != 1:
        raise RuntimeError(f'expected one exhausted-retry audit, got {audits}')

    # The reset budget must make the event deliverable exactly once.
    calls = 0
    calls_lock = threading.Lock()

    def fake_urlopen(request, timeout=5):
        nonlocal calls
        if _event_header(request) == event_no:
            with calls_lock:
                calls += 1
        return _Response()

    original_url = _application.EVENT_WEBHOOK_URL
    original_open = _application.urllib_request.urlopen
    _application.EVENT_WEBHOOK_URL = 'https://example.invalid/euas'
    _application.urllib_request.urlopen = fake_urlopen
    try:
        def process(_index):
            with db() as conn:
                return process_outbox_atomic(conn)

        _parallel(WORKERS, process)
    finally:
        _application.EVENT_WEBHOOK_URL = original_url
        _application.urllib_request.urlopen = original_open

    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()
    if calls != 1 or event['status'] != 'Delivered' or int(event['attempts']) != 1:
        raise RuntimeError(f'reset-budget delivery invalid: sends={calls} state={dict(event)!r}')


def _postcommit_boundary() -> None:
    _quiesce_existing()
    user = _admin()
    suffix = uuid.uuid4().hex[:12]
    event_no = f'EVT-COMMIT-PG-{suffix}'
    run_no = f'JOB-COMMIT-PG-{suffix}'
    observed_committed = False

    def fake_legacy(conn, actor_id, trigger_source='manual', as_of=None):
        run = conn.execute(
            '''INSERT INTO job_runs(
                 run_no,trigger_source,status,actor_id,as_of,started_at,
                 finished_at,summary_json
               ) VALUES(?,?,'Succeeded',?,?,?,?,?)''',
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

    original_legacy = outbox_store._legacy_execute_automation
    original_url = _application.EVENT_WEBHOOK_URL
    original_open = _application.urllib_request.urlopen
    outbox_store._legacy_execute_automation = fake_legacy
    _application.EVENT_WEBHOOK_URL = 'https://example.invalid/euas'
    _application.urllib_request.urlopen = fake_urlopen
    try:
        with db() as conn:
            result = execute_automation_postcommit(conn, user['id'])
    finally:
        outbox_store._legacy_execute_automation = original_legacy
        _application.EVENT_WEBHOOK_URL = original_url
        _application.urllib_request.urlopen = original_open

    if not observed_committed:
        raise RuntimeError('webhook was invoked before its outbox event was externally visible')
    if int(result['summary']['outbox_delivered']) != 1:
        raise RuntimeError(f'post-commit delivery summary invalid: {result!r}')
    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts FROM event_outbox WHERE event_no=?',
            (event_no,),
        ).fetchone()
    if event['status'] != 'Delivered' or int(event['attempts']) != 1:
        raise RuntimeError(f'post-commit event state invalid: {dict(event)!r}')


def main() -> None:
    _claim_generation_once()
    _one_external_send()
    _retry_is_idempotent()
    _retry_exhausted_resets_budget_once()
    _postcommit_boundary()
    print(
        'outbox delivery concurrency smoke: PASS '
        'generation_claim=1 send=1 retry_audit=1 exhausted_retry_reset=1 postcommit=visible workers=12'
    )


if __name__ == '__main__':
    main()
