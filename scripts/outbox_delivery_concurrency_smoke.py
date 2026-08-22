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
from app.main import app  # noqa: F401 - installs the production compatibility composition
from app.outbox_store import _claim_delivery, process_outbox_atomic, retry_outbox_event_atomic


WORKERS = 12


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _admin() -> dict:
    with db() as conn:
        row = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not row:
            raise RuntimeError('outbox smoke requires seeded admin user')
        return dict(row)


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
    event_id, event_no = _seed_event()
    calls = 0
    calls_lock = threading.Lock()

    def fake_urlopen(request, timeout=5):
        nonlocal calls
        with calls_lock:
            calls += 1
        if request.headers.get('X-euas-event-id') != event_no:
            raise RuntimeError('outbox event ID header changed')
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

        results = _parallel(WORKERS, process)
    finally:
        _application.EVENT_WEBHOOK_URL = original_url
        _application.urllib_request.urlopen = original_open

    if calls != 1:
        raise RuntimeError(f'expected one external send, got {calls}')
    if sum(int(result['delivered']) for result in results) != 1:
        raise RuntimeError(f'expected one delivered result, got {results!r}')
    with db() as conn:
        event = conn.execute(
            'SELECT status,attempts,processed_at FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()
    if event['status'] != 'Delivered' or int(event['attempts']) != 1 or not event['processed_at']:
        raise RuntimeError(f'delivery state invalid: {dict(event)!r}')


def _retry_is_idempotent() -> None:
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


def main() -> None:
    _claim_generation_once()
    _one_external_send()
    _retry_is_idempotent()
    print(
        'outbox delivery concurrency smoke: PASS '
        'generation_claim=1 send=1 retry_audit=1 workers=12'
    )


if __name__ == '__main__':
    main()
