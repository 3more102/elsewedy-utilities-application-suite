from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.alarm_lifecycle_store import (
    AlarmLifecycleConflict,
    acknowledge_alarm_atomic,
    close_alarm_atomic,
)
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - install production compatibility composition


WORKERS = 8


def _admin(conn) -> dict:
    ensure_audit_chain_lock(conn)
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    if not row:
        raise RuntimeError('alarm lifecycle smoke requires seeded admin')
    return dict(row)


def _seed_alarm(conn, suffix: str):
    user = _admin(conn)
    asset = conn.execute(
        '''SELECT a.id,l.site_id FROM assets a
           LEFT JOIN locations l ON l.id=a.location_id
           ORDER BY a.id LIMIT 1'''
    ).fetchone()
    if not asset:
        raise RuntimeError('alarm lifecycle smoke requires seeded asset')
    stamp = now()
    channel = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             active,created_at,updated_at
           ) VALUES(?,?,?,'Temperature','C','CI',1,?,?)''',
        (
            f'CH-PG-LC-{suffix}',
            asset['id'],
            f'PostgreSQL lifecycle channel {suffix}',
            stamp,
            stamp,
        ),
    )
    alarm_no = f'ALM-PG-LC-{suffix}'
    alarm = conn.execute(
        '''INSERT INTO operational_alarms(
             alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,
             message,trigger_value,threshold_value,opened_at,last_seen_at,
             occurrence_count
           ) VALUES(?,?,?,?,'Critical','Open','Threshold',?,?,?,?,?,1)''',
        (
            alarm_no,
            channel.lastrowid,
            asset['id'],
            asset['site_id'],
            f'PostgreSQL lifecycle race {suffix}',
            110.0,
            90.0,
            stamp,
            stamp,
        ),
    )
    return user, int(alarm.lastrowid), alarm_no


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(operation(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('alarm lifecycle worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'alarm lifecycle worker failed: {errors!r}')
    return results


def _evidence(alarm_id: int, alarm_no: str):
    with db() as conn:
        alarm = conn.execute(
            'SELECT status FROM operational_alarms WHERE id=?',
            (alarm_id,),
        ).fetchone()
        ack_audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Utilities Operations'
                     AND action='ACKNOWLEDGE ALARM' AND record_id=?""",
                (alarm_no,),
            ).fetchone()[0]
        )
        close_audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Utilities Operations'
                     AND action='CLOSE ALARM' AND record_id=?""",
                (alarm_no,),
            ).fetchone()[0]
        )
        events = int(
            conn.execute(
                """SELECT COUNT(*) FROM event_outbox
                   WHERE event_type='operations.alarm.closed'
                     AND aggregate_type='alarm' AND aggregate_id=?""",
                (alarm_no,),
            ).fetchone()[0]
        )
    return str(alarm['status']), ack_audits, close_audits, events


def main() -> None:
    suffix = uuid.uuid4().hex[:10]

    # Eight simultaneous acknowledgements must produce one state mutation/audit.
    with db() as conn:
        user, alarm_id, alarm_no = _seed_alarm(conn, suffix + '-ACK')

    def acknowledge(_: int):
        with db() as conn:
            return acknowledge_alarm_atomic(conn, alarm_id, user)

    ack_results = _race(acknowledge)
    if any(result != {'ok': True, 'status': 'Acknowledged'} for result in ack_results):
        raise RuntimeError(f'acknowledge replay inconsistency: {ack_results!r}')
    status, ack_audits, close_audits, events = _evidence(alarm_id, alarm_no)
    if (status, ack_audits, close_audits, events) != ('Acknowledged', 1, 0, 0):
        raise RuntimeError(
            'acknowledge evidence duplicated: '
            f'status={status} ack={ack_audits} close={close_audits} events={events}'
        )

    # Eight simultaneous closes must emit one close event and one close audit.
    with db() as conn:
        user, alarm_id, alarm_no = _seed_alarm(conn, suffix + '-CLOSE')

    def close(_: int):
        with db() as conn:
            return close_alarm_atomic(conn, alarm_id, user)

    close_results = _race(close)
    if any(result != {'ok': True, 'status': 'Closed'} for result in close_results):
        raise RuntimeError(f'close replay inconsistency: {close_results!r}')
    status, ack_audits, close_audits, events = _evidence(alarm_id, alarm_no)
    if (status, ack_audits, close_audits, events) != ('Closed', 0, 1, 1):
        raise RuntimeError(
            'close evidence duplicated: '
            f'status={status} ack={ack_audits} close={close_audits} events={events}'
        )

    # Repeated acknowledge-vs-close contention must never allow Closed to regress.
    for round_no in range(8):
        with db() as conn:
            user, alarm_id, alarm_no = _seed_alarm(
                conn, f'{suffix}-MIX-{round_no}'
            )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def mixed_ack() -> None:
            try:
                barrier.wait(timeout=15)
                with db() as conn:
                    acknowledge_alarm_atomic(conn, alarm_id, user)
                outcomes.append('ack')
            except AlarmLifecycleConflict:
                outcomes.append('ack_conflict')
            except BaseException as exc:
                errors.append(exc)

        def mixed_close() -> None:
            try:
                barrier.wait(timeout=15)
                with db() as conn:
                    close_alarm_atomic(conn, alarm_id, user)
                outcomes.append('close')
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=mixed_ack), threading.Thread(target=mixed_close)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=45)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError('ack/close race did not finish (possible deadlock)')
        if errors:
            raise RuntimeError(f'ack/close race failed: {errors!r}')
        if 'close' not in outcomes or not any(
            value in outcomes for value in ('ack', 'ack_conflict')
        ):
            raise RuntimeError(f'invalid ack/close outcomes: {outcomes!r}')

        status, ack_audits, close_audits, events = _evidence(alarm_id, alarm_no)
        if status != 'Closed' or close_audits != 1 or events != 1 or ack_audits not in (0, 1):
            raise RuntimeError(
                f'ack/close terminal invariant failed round={round_no}: '
                f'status={status} ack={ack_audits} close={close_audits} events={events}'
            )

    print(
        'alarm lifecycle concurrency smoke: PASS '
        'ack_workers=8 close_workers=8 mixed_rounds=8 closed_terminal=stable'
    )


if __name__ == '__main__':
    main()
