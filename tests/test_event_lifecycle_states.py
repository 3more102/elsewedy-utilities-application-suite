from __future__ import annotations

from app.database import db, now
from app.outbox_store import process_outbox_atomic
from app import application as _application


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_outbox_pending_to_delivered_lifecycle(monkeypatch):
    with db() as conn:
        row = conn.execute(
            '''INSERT INTO event_outbox(
                 event_no,event_type,aggregate_type,aggregate_id,payload_json,
                 status,attempts,created_at,last_error
               ) VALUES(?,?,?,?,?,'Pending',0,?,?)''',
            (
                'EVT-LIFECYCLE-001',
                'test.lifecycle',
                'TestAggregate',
                '1',
                '{"ok":true}',
                now(),
                '',
            ),
        )
        event_id = int(row.lastrowid)

    monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
    monkeypatch.setattr(_application.urllib_request, 'urlopen', lambda request, timeout=5: _Response())

    with db() as conn:
        result = process_outbox_atomic(conn)
        assert result['delivered'] == 1
        event = conn.execute(
            'SELECT status,attempts,last_error FROM event_outbox WHERE id=?',
            (event_id,),
        ).fetchone()

    assert event['status'] == 'Delivered'
    assert int(event['attempts']) == 1
    assert event['last_error'] == ''
