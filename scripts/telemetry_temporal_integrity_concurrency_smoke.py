from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.alarm_lifecycle_store import close_alarm_atomic
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - installs production suite composition
from app.telemetry_store import ingest_telemetry_atomic
from app.work_order_number_startup import initialize_work_order_number_support


ROUNDS = 8


def _bootstrap() -> tuple[dict, int]:
    with db() as conn:
        ensure_audit_chain_lock(conn)
        initialize_work_order_number_support(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
        if not user or not asset:
            raise RuntimeError('telemetry smoke requires seeded admin and asset')
        return dict(user), int(asset['id'])


def _seed_channel(conn, asset_id: int, suffix: str) -> tuple[int, str]:
    stamp = now()
    code = f'TEL-PG-TEMP-{suffix}'.upper()
    created = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_high,critical_high,active,created_at,updated_at
           ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
        (code, asset_id, f'PostgreSQL temporal {suffix}', stamp, stamp),
    )
    return int(created.lastrowid), code


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


def _run_threads(functions, timeout: int = 45):
    barrier = threading.Barrier(len(functions))
    results = []
    errors: list[BaseException] = []

    def worker(fn) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(fn())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in functions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('telemetry worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'telemetry worker failed: {errors!r}')
    return results


def _evidence(channel_id: int) -> tuple[dict, int, list[dict]]:
    with db() as conn:
        channel = conn.execute(
            'SELECT last_value,last_quality,last_reading_at FROM telemetry_channels WHERE id=?',
            (channel_id,),
        ).fetchone()
        readings = int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )
        alarms = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')
                   ORDER BY id""",
                (channel_id,),
            ).fetchall()
        ]
    return dict(channel), readings, alarms


def _single_channel_temporal_races(user: dict, asset_id: int, prefix: str) -> None:
    older = '2026-08-23T01:10:00+03:00'
    newer = '2026-08-23T01:20:00+03:00'

    for round_no in range(ROUNDS):
        with db() as conn:
            channel_id, code = _seed_channel(conn, asset_id, f'{prefix}-C-{round_no}')

        def old_normal():
            with db() as conn:
                return ingest_telemetry_atomic(conn, _body(code, 20, older), user)

        def new_critical():
            with db() as conn:
                return ingest_telemetry_atomic(conn, _body(code, 80, newer), user)

        results = _run_threads([old_normal, new_critical])
        if sum(int(result['accepted']) for result in results) != 2:
            raise RuntimeError(f'critical race lost reading: {results!r}')
        channel, readings, alarms = _evidence(channel_id)
        if readings != 2 or float(channel['last_value']) != 80.0:
            raise RuntimeError(f'critical race regressed channel: {channel!r}')
        if channel['last_reading_at'] != newer:
            raise RuntimeError(f'critical race timestamp regressed: {channel!r}')
        if len(alarms) != 1 or alarms[0]['severity'] != 'Critical':
            raise RuntimeError(f'critical race lost live alarm: {alarms!r}')
        if float(alarms[0]['trigger_value']) != 80.0 or alarms[0]['last_seen_at'] != newer:
            raise RuntimeError(f'critical alarm evidence regressed: {alarms[0]!r}')

        with db() as conn:
            channel_id, code = _seed_channel(conn, asset_id, f'{prefix}-N-{round_no}')

        def old_critical():
            with db() as conn:
                return ingest_telemetry_atomic(conn, _body(code, 90, older), user)

        def new_normal():
            with db() as conn:
                return ingest_telemetry_atomic(conn, _body(code, 20, newer), user)

        results = _run_threads([old_critical, new_normal])
        if sum(int(result['accepted']) for result in results) != 2:
            raise RuntimeError(f'normal race lost reading: {results!r}')
        channel, readings, alarms = _evidence(channel_id)
        if readings != 2 or float(channel['last_value']) != 20.0:
            raise RuntimeError(f'normal race regressed channel: {channel!r}')
        if channel['last_reading_at'] != newer or alarms:
            raise RuntimeError(f'older critical sample reopened live state: {alarms!r}')


def _opposite_order_batches(user: dict, asset_id: int, prefix: str) -> None:
    older = '2026-08-23T02:00:00+03:00'
    newer = '2026-08-23T02:10:00+03:00'
    for round_no in range(ROUNDS):
        with db() as conn:
            channel_a, code_a = _seed_channel(conn, asset_id, f'{prefix}-A-{round_no}')
            channel_b, code_b = _seed_channel(conn, asset_id, f'{prefix}-B-{round_no}')

        first = _batch([(code_a, 20, older), (code_b, 80, newer)])
        second = _batch([(code_b, 20, older), (code_a, 80, newer)])

        def ingest_first():
            with db() as conn:
                return ingest_telemetry_atomic(conn, first, user)

        def ingest_second():
            with db() as conn:
                return ingest_telemetry_atomic(conn, second, user)

        results = _run_threads([ingest_first, ingest_second])
        if sum(int(result['accepted']) for result in results) != 4:
            raise RuntimeError(f'opposite-order batch lost readings: {results!r}')
        for channel_id in (channel_a, channel_b):
            channel, readings, alarms = _evidence(channel_id)
            if readings != 2 or float(channel['last_value']) != 80.0:
                raise RuntimeError(f'opposite-order batch regressed channel: {channel!r}')
            if channel['last_reading_at'] != newer:
                raise RuntimeError(f'opposite-order timestamp regressed: {channel!r}')
            if len(alarms) != 1 or alarms[0]['severity'] != 'Critical':
                raise RuntimeError(f'opposite-order batch lost live alarm: {alarms!r}')
            if int(alarms[0]['occurrence_count']) != 1:
                raise RuntimeError(f'opposite-order duplicate alarm update: {alarms[0]!r}')


def _close_vs_clear(user: dict, asset_id: int, prefix: str) -> None:
    for round_no in range(ROUNDS):
        with db() as conn:
            channel_id, code = _seed_channel(conn, asset_id, f'{prefix}-L-{round_no}')
            opened = ingest_telemetry_atomic(
                conn,
                _body(code, 90, '2026-08-23T02:20:00+03:00'),
                user,
            )
            alarm_id = int(opened['results'][0]['alarm_id'])
            alarm_no = str(opened['results'][0]['alarm_no'])

        def clear_from_telemetry():
            with db() as conn:
                return ingest_telemetry_atomic(
                    conn,
                    _body(code, 20, '2026-08-23T02:30:00+03:00'),
                    user,
                )

        def close_manually():
            with db() as conn:
                return close_alarm_atomic(conn, alarm_id, user)

        _run_threads([clear_from_telemetry, close_manually])
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
        if alarm['status'] != 'Closed':
            raise RuntimeError(f'telemetry regressed terminal alarm: {dict(alarm)!r}')
        if close_audits != 1 or close_events != 1:
            raise RuntimeError(
                f'close evidence duplicated: audits={close_audits} events={close_events}'
            )
        channel, readings, active = _evidence(channel_id)
        if readings != 2 or float(channel['last_value']) != 20.0 or active:
            raise RuntimeError(
                f'close/clear race left incoherent live state: channel={channel!r} active={active!r}'
            )


def main() -> None:
    user, asset_id = _bootstrap()
    suffix = uuid.uuid4().hex[:8]
    _single_channel_temporal_races(user, asset_id, suffix)
    _opposite_order_batches(user, asset_id, suffix)
    _close_vs_clear(user, asset_id, suffix)
    print(
        'telemetry temporal integrity concurrency smoke: PASS '
        f'rounds={ROUNDS} stale_ordering=stable multi_channel_deadlock=none close_terminal=stable'
    )


if __name__ == '__main__':
    main()
