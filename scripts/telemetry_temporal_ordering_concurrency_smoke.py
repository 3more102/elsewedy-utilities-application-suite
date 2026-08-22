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
    asset = conn.execute(
        'SELECT id FROM assets ORDER BY id LIMIT 1'
    ).fetchone()
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


def _race(code: str, user: dict, older_value: float, newer_value: float):
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []
    older_at = '2026-08-23T01:10:00+03:00'
    newer_at = '2026-08-23T01:20:00+03:00'

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
        threading.Thread(target=worker, args=(older_value, older_at)),
        threading.Thread(target=worker, args=(newer_value, newer_at)),
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
    return results, newer_at


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


def main() -> None:
    suffix = uuid.uuid4().hex[:8]

    for round_no in range(ROUNDS):
        # A delayed older normal sample must never clear a newer critical state.
        with db() as conn:
            user, channel_id, code = _seed_channel(
                conn,
                f'{suffix}-CRIT-{round_no}',
            )
        _race(code, user, older_value=20.0, newer_value=80.0)
        channel, readings, alarms = _evidence(channel_id)
        if readings != 2:
            raise RuntimeError(
                f'critical race did not preserve both readings round={round_no}: {readings}'
            )
        if (
            float(channel['last_value']) != 80.0
            or channel['last_reading_at'] != '2026-08-23T01:20:00+03:00'
        ):
            raise RuntimeError(
                f'critical race regressed channel state round={round_no}: {channel!r}'
            )
        if len(alarms) != 1 or alarms[0]['severity'] != 'Critical':
            raise RuntimeError(
                f'critical race lost current alarm round={round_no}: {alarms!r}'
            )
        if (
            float(alarms[0]['trigger_value']) != 80.0
            or alarms[0]['last_seen_at'] != '2026-08-23T01:20:00+03:00'
        ):
            raise RuntimeError(
                f'critical alarm evidence regressed round={round_no}: {alarms[0]!r}'
            )

        # A delayed older critical sample must never reopen state after a newer
        # normal sample. Depending on lock acquisition order it may briefly open
        # then be cleared by the newer sample, or be classified historical. The
        # durable current state must be identical in both schedules.
        with db() as conn:
            user, channel_id, code = _seed_channel(
                conn,
                f'{suffix}-NORMAL-{round_no}',
            )
        _race(code, user, older_value=90.0, newer_value=20.0)
        channel, readings, alarms = _evidence(channel_id)
        if readings != 2:
            raise RuntimeError(
                f'normal race did not preserve both readings round={round_no}: {readings}'
            )
        if (
            float(channel['last_value']) != 20.0
            or channel['last_reading_at'] != '2026-08-23T01:20:00+03:00'
        ):
            raise RuntimeError(
                f'normal race regressed channel state round={round_no}: {channel!r}'
            )
        if alarms:
            raise RuntimeError(
                f'older critical sample reopened current alarm round={round_no}: {alarms!r}'
            )

    print(
        'telemetry temporal-ordering concurrency smoke: PASS '
        f'rounds={ROUNDS} readings_preserved=yes event_time_current_state=stable'
    )


if __name__ == '__main__':
    main()
