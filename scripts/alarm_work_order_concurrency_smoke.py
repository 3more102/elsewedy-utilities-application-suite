from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.application import AlarmWorkOrderIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - installs production compatibility composition
from app.alarm_store import create_alarm_work_order_atomic
from app.work_order_number_startup import initialize_work_order_number_support


WORKERS = 8


def _run_race(operation, workers: int = WORKERS):
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
        raise RuntimeError('alarm/WO concurrency worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'alarm/WO concurrency worker failed: {errors!r}')
    return results


def _bootstrap_race() -> None:
    with db() as conn:
        conn.execute('DROP TABLE IF EXISTS work_order_number_lock')

    def bootstrap(_: int):
        with db() as conn:
            initialize_work_order_number_support(conn)
        return True

    _run_race(bootstrap)
    with db() as conn:
        rows = conn.execute('SELECT id,guard FROM work_order_number_lock').fetchall()
    if len(rows) != 1 or int(rows[0]['id']) != 1:
        raise RuntimeError(f'invalid work-order number coordinator rows: {rows!r}')


def _admin(conn) -> dict:
    ensure_audit_chain_lock(conn)
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    if not row:
        raise RuntimeError('alarm smoke requires seeded admin')
    return dict(row)


def _seed_alarm(conn, suffix: str):
    user = _admin(conn)
    asset = conn.execute(
        '''SELECT a.id,l.site_id FROM assets a
           LEFT JOIN locations l ON l.id=a.location_id
           ORDER BY a.id LIMIT 1'''
    ).fetchone()
    if not asset:
        raise RuntimeError('alarm smoke requires seeded asset')
    stamp = now()
    channel_code = f'CH-PG-ALM-{suffix}'
    channel = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             active,created_at,updated_at
           ) VALUES(?,?,?,'Temperature','C','CI',1,?,?)''',
        (
            channel_code,
            asset['id'],
            f'PostgreSQL alarm channel {suffix}',
            stamp,
            stamp,
        ),
    )
    alarm_no = f'ALM-PG-CAS-{suffix}'
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
            f'PostgreSQL alarm work-order race {suffix}',
            110.0,
            90.0,
            stamp,
            stamp,
        ),
    )
    return user, int(alarm.lastrowid), alarm_no, channel_code


def main() -> None:
    _bootstrap_race()
    suffix = uuid.uuid4().hex[:10]

    with db() as conn:
        initialize_work_order_number_support(conn)
        user, alarm_id, alarm_no, channel_code = _seed_alarm(conn, suffix)

    def same_alarm(_: int):
        with db() as conn:
            return create_alarm_work_order_atomic(
                conn,
                alarm_id,
                AlarmWorkOrderIn(notes='PostgreSQL alarm concurrency smoke'),
                user,
            )

    results = _run_race(same_alarm)
    if sum(1 for result in results if result['existing'] is False) != 1:
        raise RuntimeError(f'same-alarm race did not have one creator: {results!r}')
    if sum(1 for result in results if result['existing'] is True) != WORKERS - 1:
        raise RuntimeError(f'same-alarm race did not replay existing work: {results!r}')
    if len({int(result['id']) for result in results}) != 1:
        raise RuntimeError(f'same-alarm race returned different work ids: {results!r}')

    with db() as conn:
        alarm = conn.execute(
            'SELECT work_order_id FROM operational_alarms WHERE id=?',
            (alarm_id,),
        ).fetchone()
        if not alarm or not alarm['work_order_id']:
            raise RuntimeError('alarm was not linked to generated work')
        work_id = int(alarm['work_order_id'])
        works = int(
            conn.execute(
                '''SELECT COUNT(*) FROM work_orders
                   WHERE failure_code=? AND description LIKE ?''',
                (f'ALARM-{channel_code}', f'%{alarm_no}%'),
            ).fetchone()[0]
        )
        approvals = int(
            conn.execute(
                """SELECT COUNT(*) FROM approval_requests
                   WHERE record_type='work_order' AND record_id=?""",
                (work_id,),
            ).fetchone()[0]
        )
        workflows = int(
            conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='work_order' AND record_id=?
                     AND event='ALARM GENERATED'""",
                (work_id,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Utilities Operations'
                     AND action='CREATE WORK FROM ALARM' AND record_id=?""",
                (alarm_no,),
            ).fetchone()[0]
        )
    if (works, approvals, workflows, audits) != (1, 1, 1, 1):
        raise RuntimeError(
            'same-alarm side effects duplicated: '
            f'works={works} approvals={approvals} workflow={workflows} audit={audits}'
        )

    # Independent creators compete on the global WO number sequence. Every
    # allocation must remain unique instead of producing a unique-key loser.
    reference = f'PG WO sequence race {suffix}'

    def independent(index: int):
        with db() as conn:
            number = _application.next_no(conn, 'work_orders', 'wo_no', 'WO-', 10026)
            stamp = now()
            conn.execute(
                '''INSERT INTO work_orders(
                     wo_no,title,priority,status,work_type,requested_by,
                     created_at,updated_at
                   ) VALUES(?,?,'Medium','Draft','Corrective Maintenance',?,?,?)''',
                (number, f'{reference}-{index}', user['id'], stamp, stamp),
            )
            return number

    numbers = _run_race(independent)
    if len(set(str(value) for value in numbers)) != WORKERS:
        raise RuntimeError(f'WO allocator returned duplicate numbers: {numbers!r}')
    with db() as conn:
        persisted = int(
            conn.execute(
                'SELECT COUNT(*) FROM work_orders WHERE title LIKE ?',
                (f'{reference}-%',),
            ).fetchone()[0]
        )
    if persisted != WORKERS:
        raise RuntimeError(
            f'WO sequence race expected {WORKERS} commits, found {persisted}'
        )

    print(
        'alarm work-order concurrency smoke: PASS '
        'bootstrap=8 same_alarm=1 side_effects=1 wo_numbers=8'
    )


if __name__ == '__main__':
    main()
