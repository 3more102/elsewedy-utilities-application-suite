from __future__ import annotations

import uuid

from app.database import db, now
from app.outbox_store import process_outbox_atomic


def _seed_outbox(conn, status='Pending'):
    event_no = f'EVT-LIFECYCLE-{uuid.uuid4().hex[:10]}'
    row = conn.execute(
        '''INSERT INTO event_outbox(
             event_no,event_type,aggregate_type,aggregate_id,payload_json,
             status,attempts,created_at,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            event_no,
            'lifecycle.test',
            'test',
            event_no,
            '{"ok":true}',
            status,
            0,
            now(),
            '',
        ),
    )
    return int(row.lastrowid), event_no


def test_pending_event_can_enter_delivery_lifecycle(monkeypatch):
    from app import application as application_module

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(application_module, 'EVENT_WEBHOOK_URL', 'https://example.invalid/events')
    monkeypatch.setattr(application_module.urllib_request, 'urlopen', lambda *args, **kwargs: Response())

    with db() as conn:
        event_id, _ = _seed_outbox(conn)

    with db() as conn:
        result = process_outbox_atomic(conn)
        assert result['delivered'] == 1

    with db() as conn:
        event = conn.execute(
            'SELECT status, attempts FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()

    assert event['status'] == 'Delivered'
    assert int(event['attempts']) == 1


def test_failed_events_remain_retryable(monkeypatch):
    from app import application as application_module

    def fail(*args, **kwargs):
        raise RuntimeError('temporary failure')

    monkeypatch.setattr(application_module, 'EVENT_WEBHOOK_URL', 'https://example.invalid/events')
    monkeypatch.setattr(application_module.urllib_request, 'urlopen', fail)

    with db() as conn:
        event_id, _ = _seed_outbox(conn)

    with db() as conn:
        result = process_outbox_atomic(conn)
        assert result['failed'] == 1

    with db() as conn:
        event = conn.execute(
            'SELECT status, attempts FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()

    assert event['status'] == 'Failed'
    assert int(event['attempts']) == 1
