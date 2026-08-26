from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application
from app.alarm_lifecycle_store import close_alarm_atomic
from app.database import db, now
from app.main import app
from app.telemetry_store import ingest_telemetry_atomic


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_channel(conn, suffix: str) -> int:
    user = _admin(conn)
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    stamp = now()
    cursor = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_high,critical_high,active,created_at,updated_at
           ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
        (
            f'TEL-RACE-{suffix}'.upper(),
            asset['id'],
            f'Alarm telemetry race {suffix}',
            stamp,
            stamp,
        ),
    )
    del user
    return int(cursor.lastrowid)


def _ingest(conn, code: str, value: float, user: dict) -> dict:
    body = application.TelemetryIngestIn(
        readings=[application.TelemetryReadingItem(channel_code=code, value=value)],
    )
    return ingest_telemetry_atomic(conn, body, user)


def _alarm(alarm_id: int) -> dict:
    with db() as conn:
        return dict(
            conn.execute(
                'SELECT * FROM operational_alarms WHERE id=?',
                (alarm_id,),
            ).fetchone()
        )


def _count(conn_sql: str, args: tuple) -> int:
    with db() as conn:
        return int(conn.execute(conn_sql, args).fetchone()[0])


def test_normal_reading_racing_operator_close_never_regresses_closed_state(monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app):
        with db() as conn:
            user = _admin(conn)
            channel_id = _seed_channel(conn, suffix)
            code = conn.execute(
                'SELECT channel_code FROM telemetry_channels WHERE id=?',
                (channel_id,),
            ).fetchone()['channel_code']
            summary = _ingest(conn, code, 80.0, user)
        alarm_id = summary['results'][0]['alarm_id']
        alarm_no = summary['results'][0]['alarm_no']

        original_site = application._channel_site
        raced = []

        def racing_site(conn, asset_id):
            # Model the PostgreSQL READ COMMITTED interleaving where the
            # operator's close commits between telemetry's alarm SELECT and
            # its previously unguarded clear UPDATE.
            if not raced:
                raced.append(True)
                close_alarm_atomic(conn, alarm_id, user)
            return original_site(conn, asset_id)

        monkeypatch.setattr(application, '_channel_site', racing_site)
        try:
            with db() as conn:
                result = _ingest(conn, code, 20.0, user)
        finally:
            monkeypatch.undo()

        assert raced
        assert result['results'][0]['action'] != 'cleared'

        final = _alarm(alarm_id)
        assert final['status'] == 'Closed'
        assert final['closed_at']
        assert final['cleared_at'] is None

        assert _count(
            "SELECT COUNT(*) FROM event_outbox WHERE event_type='operations.alarm.cleared' AND aggregate_id=?",
            (alarm_no,),
        ) == 0
        assert _count(
            """SELECT COUNT(*) FROM audit_logs
               WHERE action='ALARM CLEAR' AND record_id=?""",
            (alarm_no,),
        ) == 0
        assert _count(
            """SELECT COUNT(*) FROM audit_logs
               WHERE action='CLOSE ALARM' AND record_id=?""",
            (alarm_no,),
        ) == 1


def test_violating_reading_racing_close_opens_fresh_alarm_without_mutating_closed_one(monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app):
        with db() as conn:
            user = _admin(conn)
            channel_id = _seed_channel(conn, suffix)
            code = conn.execute(
                'SELECT channel_code FROM telemetry_channels WHERE id=?',
                (channel_id,),
            ).fetchone()['channel_code']
            summary = _ingest(conn, code, 80.0, user)
        alarm_id = summary['results'][0]['alarm_id']

        original_site = application._channel_site
        raced = []

        def racing_site(conn, asset_id):
            if not raced:
                raced.append(True)
                close_alarm_atomic(conn, alarm_id, user)
            return original_site(conn, asset_id)

        monkeypatch.setattr(application, '_channel_site', racing_site)
        try:
            with db() as conn:
                result = _ingest(conn, code, 90.0, user)
        finally:
            monkeypatch.undo()

        assert raced
        closed = _alarm(alarm_id)
        assert closed['status'] == 'Closed'
        assert float(closed['trigger_value']) == 80.0
        assert int(closed['occurrence_count']) == 1

        assert result['results'][0]['action'] == 'opened'
        fresh = _alarm(result['results'][0]['alarm_id'])
        assert fresh['status'] == 'Open'
        assert float(fresh['trigger_value']) == 90.0


def test_concurrent_normal_reading_and_close_always_end_closed():
    for round_no in range(6):
        suffix = f'{uuid.uuid4().hex[:8]}{round_no}'
        with TestClient(app):
            with db() as conn:
                user = _admin(conn)
                channel_id = _seed_channel(conn, suffix)
                code = conn.execute(
                    'SELECT channel_code FROM telemetry_channels WHERE id=?',
                    (channel_id,),
                ).fetchone()['channel_code']
                summary = _ingest(conn, code, 80.0, user)
            alarm_id = summary['results'][0]['alarm_id']
            alarm_no = summary['results'][0]['alarm_no']

            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            outcomes: list[str] = []

            def clear_via_telemetry() -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        result = _ingest(conn, code, 20.0, user)
                    outcomes.append(result['results'][0]['action'])
                except BaseException as exc:
                    errors.append(exc)

            def close_as_operator() -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        close_alarm_atomic(conn, alarm_id, user)
                    outcomes.append('closed')
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=clear_via_telemetry),
                threading.Thread(target=close_as_operator),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert not any(thread.is_alive() for thread in threads)
            assert errors == []
            assert 'closed' in outcomes

            final = _alarm(alarm_id)
            assert final['status'] == 'Closed'
            assert final['cleared_at'] is None or final['closed_at']
            assert _count(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE action='CLOSE ALARM' AND record_id=?""",
                (alarm_no,),
            ) == 1
            assert _count(
                """SELECT COUNT(*) FROM event_outbox
                   WHERE event_type='operations.alarm.closed' AND aggregate_id=?""",
                (alarm_no,),
            ) == 1
