from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db, now
from app.main import app
from app.outbox_store import process_outbox_atomic


def _seed_event(conn, suffix: str, attempts: int = 0) -> tuple[int, str]:
    event_no = f'EVT-POISON-{suffix}'
    created = conn.execute(
        '''INSERT INTO event_outbox(
             event_no,event_type,aggregate_type,aggregate_id,payload_json,
             status,attempts,created_at,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            event_no,
            'test.outbox.poison',
            'test',
            suffix,
            '{"ok":true}',
            'Pending',
            attempts,
            now(),
            '',
        ),
    )
    return int(created.lastrowid), event_no


def _exhausted_alerts(event_no: str) -> list[dict]:
    with db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM notifications
                   WHERE title='Outbox delivery exhausted'
                     AND link_module='Integration Events'
                     AND link_id=?""",
                (f'outbox-exhausted:{event_no}',),
            ).fetchall()
        ]


class _FailingResponse:
    status = 500

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_exhausted_event_raises_single_operator_alert(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, event_no = _seed_event(conn, suffix)

        def failing_urlopen(request, timeout=5):
            return _FailingResponse()

        monkeypatch.setattr(
            _application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas'
        )
        monkeypatch.setattr(_application.urllib_request, 'urlopen', failing_urlopen)

        max_attempts = int(_application.OUTBOX_MAX_ATTEMPTS)
        for cycle in range(max_attempts):
            with db() as conn:
                process_outbox_atomic(conn)
            # Other tests may leave eligible rows in the shared outbox, so
            # assert on this event's own generation rather than batch totals.
            with db() as conn:
                event = conn.execute(
                    'SELECT status,attempts FROM event_outbox WHERE id=?',
                    (event_id,),
                ).fetchone()
            assert int(event['attempts']) == cycle + 1

        # Budget spent: the event is now a poison message...
        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert event['status'] == 'Failed'
        assert int(event['attempts']) == max_attempts

        # ...and exactly one unread operator alert exists for it.
        alerts = _exhausted_alerts(event_no)
        assert len(alerts) == 1
        assert alerts[0]['severity'] == 'Warning'
        assert alerts[0]['is_read'] == 0
        assert f'{max_attempts} attempts' in alerts[0]['message']

        # The exhausted event is not re-claimed, so no duplicate alert appears.
        with db() as conn:
            process_outbox_atomic(conn)
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert int(event['attempts']) == max_attempts
        assert len(_exhausted_alerts(event_no)) == 1


def test_retryable_failure_does_not_alert_operators(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, event_no = _seed_event(conn, suffix)

        def failing_urlopen(request, timeout=5):
            return _FailingResponse()

        monkeypatch.setattr(
            _application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas'
        )
        monkeypatch.setattr(_application.urllib_request, 'urlopen', failing_urlopen)

        with db() as conn:
            process_outbox_atomic(conn)

        with db() as conn:
            event = conn.execute(
                'SELECT status,attempts FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert event['status'] == 'Failed'
        assert int(event['attempts']) < int(_application.OUTBOX_MAX_ATTEMPTS)
        assert _exhausted_alerts(event_no) == []
