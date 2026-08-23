from __future__ import annotations

from datetime import datetime, timedelta
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app import outbox_store
from app.database import db, now
from app.main import app
from app.outbox_store import (
    _claim_delivery,
    _finalize_claim,
    process_outbox_atomic,
    retry_outbox_event_atomic,
)


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_event(
    conn,
    suffix: str,
    *,
    status: str = 'Pending',
    attempts: int = 0,
    processed_at: str | None = None,
) -> tuple[int, str]:
    event_no = f'EVT-LEASE-{suffix}'
    created = conn.execute(
        '''INSERT INTO event_outbox(
             event_no,event_type,aggregate_type,aggregate_id,payload_json,
             status,attempts,created_at,processed_at,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (
            event_no,
            'test.outbox.lease',
            'test',
            suffix,
            '{"ok":true}',
            status,
            attempts,
            now(),
            processed_at,
            '',
        ),
    )
    return int(created.lastrowid), event_no


def _event_header(request) -> str | None:
    return request.get_header('X-euas-event-id') or request.get_header('X-EUAS-Event-ID')


def test_active_claim_is_durable_and_not_reclaimed(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, event_no = _seed_event(conn, suffix)
        with db() as conn:
            snapshot = dict(
                conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
            )
            claimed = _claim_delivery(conn, snapshot)
        assert claimed is not None
        assert claimed['status'] == 'Pending'
        assert int(claimed['attempts']) == 1
        assert claimed['processed_at']

        calls: list[str | None] = []

        def fake_urlopen(request, timeout=5):
            calls.append(_event_header(request))
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)
        with db() as conn:
            process_outbox_atomic(conn)
        assert calls.count(event_no) == 0

        with db() as conn:
            row = conn.execute(
                'SELECT status,attempts,processed_at FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
            assert row['status'] == 'Pending'
            assert int(row['attempts']) == 1
            assert row['processed_at'] == claimed['processed_at']
            conn.execute(
                "UPDATE event_outbox SET status='Skipped',processed_at=?,last_error='test cleanup' WHERE id=?",
                (now(), event_id),
            )


def test_stale_lease_is_reclaimed_as_new_generation(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        stale = (
            datetime.now()
            - timedelta(seconds=outbox_store.OUTBOX_LEASE_SECONDS + 5)
        ).isoformat(timespec='seconds')
        with db() as conn:
            event_id, event_no = _seed_event(
                conn, suffix, attempts=1, processed_at=stale
            )

        calls: list[str | None] = []

        def fake_urlopen(request, timeout=5):
            calls.append(_event_header(request))
            return _Response()

        monkeypatch.setattr(_application, 'EVENT_WEBHOOK_URL', 'https://example.invalid/euas')
        monkeypatch.setattr(_application.urllib_request, 'urlopen', fake_urlopen)
        with db() as conn:
            result = process_outbox_atomic(conn)

        assert calls.count(event_no) == 1
        assert result['delivered'] >= 1
        with db() as conn:
            row = conn.execute(
                'SELECT status,attempts,processed_at,last_error FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
        assert row['status'] == 'Delivered'
        assert int(row['attempts']) == 2
        assert row['processed_at']
        assert row['last_error'] == ''


def test_operator_retry_resets_exhausted_stale_pending_lease():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        stale = (
            datetime.now()
            - timedelta(seconds=outbox_store.OUTBOX_LEASE_SECONDS + 5)
        ).isoformat(timespec='seconds')
        with db() as conn:
            user = _admin(conn)
            event_id, event_no = _seed_event(
                conn,
                suffix,
                attempts=_application.OUTBOX_MAX_ATTEMPTS,
                processed_at=stale,
            )

        with db() as conn:
            result = retry_outbox_event_atomic(conn, event_id, user)
        assert result == {'ok': True, 'event_no': event_no}

        with db() as conn:
            row = conn.execute(
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
        assert row['status'] == 'Pending'
        assert int(row['attempts']) == 0
        assert row['processed_at'] is None
        assert row['last_error'] == ''
        assert audits == 1


def test_reclaimed_generation_fences_older_sender(monkeypatch):
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            event_id, _ = _seed_event(conn, suffix)
        with db() as conn:
            snapshot = dict(
                conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
            )
            first = _claim_delivery(conn, snapshot)
        assert first is not None
        assert int(first['attempts']) == 1

        # Advance the lease clock without sleeping so the same durable row can
        # be reclaimed as a newer generation.
        monkeypatch.setattr(outbox_store, '_lease_cutoff', lambda: '9999-12-31T23:59:59')
        with db() as conn:
            stale_snapshot = dict(
                conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
            )
            second = _claim_delivery(conn, stale_snapshot)
        assert second is not None
        assert int(second['attempts']) == 2
        assert second['processed_at'] != first['processed_at'] or int(second['attempts']) != int(first['attempts'])

        with db() as conn:
            finalized = _finalize_claim(
                conn,
                first,
                status='Delivered',
                processed_at=now(),
                last_error='',
            )
        assert finalized is False

        with db() as conn:
            row = conn.execute(
                'SELECT status,attempts,processed_at FROM event_outbox WHERE id=?',
                (event_id,),
            ).fetchone()
            assert row['status'] == 'Pending'
            assert int(row['attempts']) == 2
            assert row['processed_at'] == second['processed_at']
            conn.execute(
                "UPDATE event_outbox SET status='Skipped',processed_at=?,last_error='test cleanup' WHERE id=?",
                (now(), event_id),
            )