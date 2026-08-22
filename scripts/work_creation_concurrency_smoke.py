from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.application import AlarmWorkOrderIn, WorkOrderIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app
from app.work_creation_startup import initialize_work_creation_support
from app.work_creation_store import (
    create_alarm_work_order_atomic,
    next_no_with_work_order_lock,
)


WORKERS = 8


def _bootstrap_race() -> None:
    with db() as conn:
        conn.execute('DROP TABLE IF EXISTS work_order_creation_lock')

    barrier = threading.Barrier(WORKERS)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                initialize_work_creation_support(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('work creation bootstrap deadlocked')
    if errors:
        raise RuntimeError(f'work creation bootstrap failed: {errors!r}')
    with db() as conn:
        rows = conn.execute('SELECT id FROM work_order_creation_lock').fetchall()
    if len(rows) != 1 or int(rows[0]['id']) != 1:
        raise RuntimeError(f'invalid work creation coordinator: {rows!r}')


def _admin_channel():
    with db() as conn:
        ensure_audit_chain_lock(conn)
        initialize_work_creation_support(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        channel = conn.execute(
            '''SELECT tc.id channel_id,tc.asset_id,l.site_id
               FROM telemetry_channels tc
               JOIN assets a ON a.id=tc.asset_id
               LEFT JOIN locations l ON l.id=a.location_id
               ORDER BY tc.id LIMIT 1'''
        ).fetchone()
        if not user or not channel:
            raise RuntimeError('work creation smoke requires seeded admin/telemetry')
        return dict(user), dict(channel)


def _seed_alarm(suffix: str, channel: dict) -> tuple[int, str]:
    alarm_no = f'ALM-PG-WO-{suffix}'
    with db() as conn:
        cur = conn.execute(
            '''INSERT INTO operational_alarms(
                 alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,
                 message,trigger_value,threshold_value,opened_at,last_seen_at,
                 occurrence_count
               ) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)''',
            (
                alarm_no,
                channel['channel_id'],
                channel['asset_id'],
                channel['site_id'],
                'Critical',
                f'PostgreSQL work creation race {suffix}',
                101.0,
                100.0,
                now(),
                now(),
            ),
        )
        return int(cur.lastrowid), alarm_no


def _same_alarm_race(user: dict, channel: dict) -> None:
    suffix = uuid.uuid4().hex[:10]
    alarm_id, alarm_no = _seed_alarm(suffix, channel)
    body = AlarmWorkOrderIn(notes='same alarm concurrency')
    barrier = threading.Barrier(WORKERS)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                results.append(
                    create_alarm_work_order_atomic(conn, alarm_id, body, user)
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('same-alarm work creation deadlocked')
    if errors:
        raise RuntimeError(f'same-alarm work creation failed: {errors!r}')
    if len(results) != WORKERS or sum(not r['existing'] for r in results) != 1:
        raise RuntimeError(f'alarm creation did not converge: {results!r}')
    if len({int(r['id']) for r in results}) != 1:
        raise RuntimeError(f'alarm requests linked different work orders: {results!r}')

    with db() as conn:
        alarm = conn.execute(
            'SELECT work_order_id FROM operational_alarms WHERE id=?', (alarm_id,)
        ).fetchone()
        work_id = int(alarm['work_order_id'])
        approvals = int(conn.execute(
            "SELECT COUNT(*) FROM approval_requests WHERE record_type='work_order' AND record_id=?",
            (work_id,),
        ).fetchone()[0])
        workflows = int(conn.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='ALARM GENERATED'",
            (work_id,),
        ).fetchone()[0])
        audits = int(conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE module='Utilities Operations' AND action='CREATE WORK FROM ALARM' AND record_id=?",
            (alarm_no,),
        ).fetchone()[0])
        outbox = int(conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE event_type='operations.alarm.work_order_created' AND aggregate_type='alarm' AND aggregate_id=?",
            (alarm_no,),
        ).fetchone()[0])
    if approvals != 1 or workflows != 1 or audits != 1 or outbox != 1:
        raise RuntimeError(
            'alarm side effects duplicated: '
            f'approvals={approvals} workflows={workflows} audits={audits} outbox={outbox}'
        )


def _cross_creator_number_race(user: dict, channel: dict) -> None:
    suffix = uuid.uuid4().hex[:8]
    alarms = [_seed_alarm(f'{suffix}-A{i}', channel)[0] for i in range(4)]
    barrier = threading.Barrier(8)
    numbers: list[str] = []
    errors: list[BaseException] = []

    def manual(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            result = _application.create_work(
                WorkOrderIn(title=f'Manual number race {suffix}-{index}'),
                user,
            )
            numbers.append(str(result['wo_no']))
        except BaseException as exc:
            errors.append(exc)

    def alarm(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                result = create_alarm_work_order_atomic(
                    conn,
                    alarms[index],
                    AlarmWorkOrderIn(notes='cross creator number race'),
                    user,
                )
            numbers.append(str(result['wo_no']))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=manual, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=alarm, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('cross-creator WO numbering deadlocked')
    if errors:
        raise RuntimeError(f'cross-creator WO numbering failed: {errors!r}')
    if len(numbers) != 8 or len(set(numbers)) != 8:
        raise RuntimeError(f'WO numbers were not unique: {numbers!r}')


def main() -> None:
    _bootstrap_race()
    if _application.next_no is not next_no_with_work_order_lock:
        raise RuntimeError('shared work-order allocator was not installed')
    user, channel = _admin_channel()
    _same_alarm_race(user, channel)
    _cross_creator_number_race(user, channel)
    print(
        'work creation concurrency smoke: PASS '
        'bootstrap=8 same_alarm=1 cross_creator_numbers=8'
    )


if __name__ == '__main__':
    main()
