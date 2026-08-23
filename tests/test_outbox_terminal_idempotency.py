from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app
from app.outbox_store import retry_outbox_event_atomic


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def test_delivered_event_retry_is_terminal_idempotent_noop():
    """A retry request must never requeue an event already delivered remotely."""
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        event_no = f'EVT-DELIVERED-{suffix}'
        processed_at = now()
        with db() as conn:
            user = _admin(conn)
            created = conn.execute(
                '''INSERT INTO event_outbox(
                     event_no,event_type,aggregate_type,aggregate_id,payload_json,
                     status,attempts,created_at,processed_at,last_error
                   ) VALUES(?,?,?,?,?,'Delivered',1,?,?,?)''',
                (
                    event_no,
                    'test.outbox.delivered-terminal',
                    'test',
                    suffix,
                    '{"ok":true}',
                    now(),
                    processed_at,
                    '',
                ),
            )
            event_id = int(created.lastrowid)

        with db() as conn:
            first = retry_outbox_event_atomic(conn, event_id, user)
        with db() as conn:
            second = retry_outbox_event_atomic(conn, event_id, user)

        assert first == {'ok': True, 'event_no': event_no}
        assert second == first

        with db() as conn:
            event = conn.execute(
                '''SELECT status,attempts,processed_at,last_error
                   FROM event_outbox WHERE id=?''',
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
        assert event['processed_at'] == processed_at
        assert event['last_error'] == ''
        assert audits == 0
