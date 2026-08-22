from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - install production compatibility composition
from app.telemetry_store import ingest_telemetry_atomic


ROUNDS = 8


def _admin_and_asset(conn):
    ensure_audit_chain_lock(conn)
    user = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    if not user or not asset:
        raise RuntimeError('telemetry temporal smoke requires seeded admin and asset')
    return dict(user), int(asset['id'])


def _seed_channel(conn, suffix: str):
    user, asset_id = _admin_and_asset(conn)
    stamp = now()
    code = f'TEL-PG-TEMP-{suffix}'
    cursor = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_high,critical_high,active,created_at,updated_at
           ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
        (code, asset_id, f'PostgreSQL temporal channel {suffix}', stamp, stamp),
    )
    return user, int(cursor.lastrowid), code


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


def _parallel_two(code: str, user: dict, first, second):
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker(value: float, captured_at: str) -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                results.append(
                    ingest_telemetry_atomic(
                        conn,
                        _body(code, value, captured_at),
                        user,
                    )
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=first),
        threading.Thread(target=worker, args=second),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('telemetry temporal worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'telemetry temporal worker failed: {errors!r}')
    if len(results) != 2 or sum(int(x['accepted']) for x in results) != 2:
        raise RuntimeError(f'telemetry race lost a reading: {results!r}')
    return results


def _evidence(channel_id: int):
    with db() as conn:
        channel = conn.execute(
            '''SELECT last_value,last_quality,last_reading_at
               FROM telemetry_channels WHERE id=?''',
            (channel_id,),
        ).fetchone()
        readings = int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )
        active = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')
                   ORDER BY id""",
                (channel_id,),
            ).fetchall()
        ]
        all_alarms = [
            dict(row)
            for row in conn.execute(
                'SELECT * FROM operational_alarms WHERE channel_id=? ORDER BY id',
                (channel_id,),
            ).fetchall()
        ]
    return dict(channel), readings, active, all_alarms


def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    older_at = '2026-08-23T01:10:00+03:00'
    newer_at = '2026-08-23T01:20:00+03:00'
    equal_at = '2026-08-23T01:30:00+03:00'

    for round_no in range(ROUNDS):
        # Older normal vs newer critical: durable state must be the newer
        # critical generation regardless of which transaction takes the lock first.
        with db() as conn:
            user, channel_id, code = _seed_channel(conn, f'{suffix}-CRIT-{round_no}')
        _parallel_two(code, user, (20.0, older_at), (80.0, newer_at))
        channel, readings, active, _ = _evidence(channel_id)
        if readings != 2:
            raise RuntimeError(f'critical race lost history round={round_no}: {readings}')
        if float(channel['last_value']) != 80.0 or channel['last_reading_at'] != newer_at:
            raise RuntimeError(f'critical race regressed state round={round_no}: {channel!r}')
        if len(active) != 1 or active[0]['severity'] != 'Critical':
            raise RuntimeError(f'critical race lost alarm round={round_no}: {active!r}')
        if int(active[0]['occurrence_count']) != 1:
            raise RuntimeError(f'critical race duplicated occurrence round={round_no}: {active!r}')

        # Older critical vs newer normal: the delayed old reading may open first
        # and then be cleared, or may be historical immediately, but it can never
        # remain the current state after the newer normal reading commits.
        with db() as conn:
            user, channel_id, code = _seed_channel(conn, f'{suffix}-NORMAL-{round_no}')
        _parallel_two(code, user, (90.0, older_at), (20.0, newer_at))
        channel, readings, active, _ = _evidence(channel_id)
        if readings != 2:
            raise RuntimeError(f'normal race lost history round={round_no}: {readings}')
        if float(channel['last_value']) != 20.0 or channel['last_reading_at'] != newer_at:
            raise RuntimeError(f'normal race regressed state round={round_no}: {channel!r}')
        if active:
            raise RuntimeError(f'older critical remained active round={round_no}: {active!r}')

        # Equal captured timestamps have no event-time successor relation. One
        # serialized arrival becomes live and the other must remain historical;
        # this must never create a second alarm occurrence.
        with db() as conn:
            user, channel_id, code = _seed_channel(conn, f'{suffix}-EQUAL-{round_no}')
        results = _parallel_two(code, user, (80.0, equal_at), (95.0, equal_at))
        channel, readings, active, all_alarms = _evidence(channel_id)
        if readings != 2 or sum(int(x['historical']) for x in results) != 1:
            raise RuntimeError(
                f'equal-time race did not preserve one live generation round={round_no}: {results!r}'
            )
        if channel['last_reading_at'] != equal_at:
            raise RuntimeError(f'equal-time marker changed round={round_no}: {channel!r}')
        if len(active) != 1 or len(all_alarms) != 1 or int(active[0]['occurrence_count']) != 1:
            raise RuntimeError(f'equal-time alarm evidence duplicated round={round_no}: {all_alarms!r}')
        if float(channel['last_value']) not in (80.0, 95.0):
            raise RuntimeError(f'equal-time winner invalid round={round_no}: {channel!r}')

    print(
        'telemetry temporal-ordering concurrency smoke: PASS '
        f'rounds={ROUNDS} history=preserved newest_state=stable equal_time=single_generation'
    )


if __name__ == '__main__':
    main()
