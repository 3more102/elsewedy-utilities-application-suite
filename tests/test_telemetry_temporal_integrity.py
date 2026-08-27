from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application as _application
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


def _seed_channel(conn, suffix: str) -> tuple[int, str, dict]:
    user = _admin(conn)
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    assert asset
    stamp = now()
    code = f'TEL-TEMP-{suffix}'.upper()
    created = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_high,critical_high,active,created_at,updated_at
           ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
        (code, asset['id'], f'Temporal integrity {suffix}', stamp, stamp),
    )
    return int(created.lastrowid), code, user


def _body(code: str, value: float, captured_at: str):
    return _application.TelemetryIngestIn(
        readings=[
            _application.TelemetryReadingItem(
                channel_code=code,
                value=value,
                captured_at=captured_at,
                quality='Good',
                source='CI',
            )
        ]
    )


def _batch(entries: list[tuple[str, float, str]]):
    return _application.TelemetryIngestIn(
        readings=[
            _application.TelemetryReadingItem(
                channel_code=code,
                value=value,
                captured_at=captured_at,
                quality='Good',
                source='CI',
            )
            for code, value, captured_at in entries
        ]
    )


def _channel(channel_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            'SELECT * FROM telemetry_channels WHERE id=?',
            (channel_id,),
        ).fetchone()
        assert row
        return dict(row)


def _readings(channel_id: int) -> int:
    with db() as conn:
        return int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )


def _active_alarms(channel_id: int) -> list[dict]:
    with db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')
                   ORDER BY id""",
                (channel_id,),
            ).fetchall()
        ]


def test_delayed_normal_reading_cannot_clear_newer_critical_alarm():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user = _seed_channel(conn, suffix)
            newer = ingest_telemetry_atomic(
                conn, _body(code, 80, '2026-08-23T01:20:00+03:00'), user
            )
        assert newer['alarms_opened'] == 1

        with db() as conn:
            stale = ingest_telemetry_atomic(
                conn, _body(code, 20, '2026-08-23T01:10:00+03:00'), user
            )
        assert stale['accepted'] == 1
        assert stale['historical'] == 1
        assert stale['alarms_cleared'] == 0
        assert stale['results'][0]['action'] == 'historical'

        channel = _channel(channel_id)
        assert float(channel['last_value']) == 80.0
        assert channel['last_reading_at'] == '2026-08-23T01:20:00+03:00'
        alarms = _active_alarms(channel_id)
        assert len(alarms) == 1
        assert alarms[0]['severity'] == 'Critical'
        assert int(alarms[0]['occurrence_count']) == 1
        assert float(alarms[0]['trigger_value']) == 80.0
        assert _readings(channel_id) == 2


def test_delayed_critical_reading_cannot_reopen_after_newer_normal_state():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user = _seed_channel(conn, suffix)
            current = ingest_telemetry_atomic(
                conn, _body(code, 20, '2026-08-23T01:40:00+03:00'), user
            )
        assert current['normal'] == 1

        with db() as conn:
            stale = ingest_telemetry_atomic(
                conn, _body(code, 90, '2026-08-23T01:30:00+03:00'), user
            )
        assert stale['historical'] == 1
        assert stale['alarms_opened'] == 0
        assert _active_alarms(channel_id) == []
        channel = _channel(channel_id)
        assert float(channel['last_value']) == 20.0
        assert channel['last_reading_at'] == '2026-08-23T01:40:00+03:00'
        assert _readings(channel_id) == 2


def test_equal_and_timezone_equivalent_instants_are_historical():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user = _seed_channel(conn, suffix)
            first = ingest_telemetry_atomic(
                conn, _body(code, 20, '2026-08-23T01:55:00+03:00'), user
            )
        assert first['normal'] == 1

        with db() as conn:
            equal = ingest_telemetry_atomic(
                conn, _body(code, 90, '2026-08-23T01:55:00+03:00'), user
            )
            equivalent = ingest_telemetry_atomic(
                conn, _body(code, 95, '2026-08-22T22:55:00Z'), user
            )
        assert equal['historical'] == 1
        assert equivalent['historical'] == 1
        assert _active_alarms(channel_id) == []
        assert float(_channel(channel_id)['last_value']) == 20.0
        assert _readings(channel_id) == 3


def test_invalid_capture_timestamp_rolls_back_without_reading():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user = _seed_channel(conn, suffix)

        try:
            with db() as conn:
                ingest_telemetry_atomic(conn, _body(code, 90, 'not-a-timestamp'), user)
        except Exception as exc:
            assert getattr(exc, 'status_code', None) == 400
        else:
            raise AssertionError('invalid timestamp was accepted')

        assert _readings(channel_id) == 0
        assert _channel(channel_id)['last_reading_at'] is None


def test_opposite_order_multi_channel_batches_converge_without_deadlock():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            channel_a, code_a, user = _seed_channel(conn, suffix + '-A')
            channel_b, code_b, _ = _seed_channel(conn, suffix + '-B')

        older = '2026-08-23T02:00:00+03:00'
        newer = '2026-08-23T02:10:00+03:00'
        bodies = [
            _batch([(code_a, 20, older), (code_b, 80, newer)]),
            _batch([(code_b, 20, older), (code_a, 80, newer)]),
        ]
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker(body) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(ingest_telemetry_atomic(conn, body, user))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert sum(int(result['accepted']) for result in results) == 4
        for channel_id in (channel_a, channel_b):
            channel = _channel(channel_id)
            assert float(channel['last_value']) == 80.0
            assert channel['last_reading_at'] == newer
            alarms = _active_alarms(channel_id)
            assert len(alarms) == 1
            assert alarms[0]['severity'] == 'Critical'
            assert int(alarms[0]['occurrence_count']) == 1
            assert _readings(channel_id) == 2


def test_normal_telemetry_racing_manual_close_never_regresses_closed_alarm():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user = _seed_channel(conn, suffix)
            opened = ingest_telemetry_atomic(
                conn, _body(code, 90, '2026-08-23T02:20:00+03:00'), user
            )
            alarm_id = int(opened['results'][0]['alarm_id'])
            alarm_no = str(opened['results'][0]['alarm_no'])

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def clear_from_telemetry() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    ingest_telemetry_atomic(
                        conn,
                        _body(code, 20, '2026-08-23T02:30:00+03:00'),
                        user,
                    )
            except BaseException as exc:
                errors.append(exc)

        def close_manually() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    close_alarm_atomic(conn, alarm_id, user)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=clear_from_telemetry),
            threading.Thread(target=close_manually),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        with db() as conn:
            alarm = conn.execute(
                'SELECT status FROM operational_alarms WHERE id=?',
                (alarm_id,),
            ).fetchone()
            close_audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Utilities Operations'
                         AND action='CLOSE ALARM' AND record_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
            close_events = int(
                conn.execute(
                    """SELECT COUNT(*) FROM event_outbox
                       WHERE event_type='operations.alarm.closed'
                         AND aggregate_type='alarm' AND aggregate_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
        assert alarm['status'] == 'Closed'
        assert close_audits >= 1
        assert close_events >= 1
        assert _active_alarms(channel_id) == []
        assert float(_channel(channel_id)['last_value']) == 20.0
        assert _readings(channel_id) == 2
