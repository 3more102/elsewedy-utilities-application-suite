from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.application import AlarmWorkOrderIn
from app.database import db, now
from app.main import app
from app.work_creation_store import (
    create_alarm_work_order_atomic,
    next_no_with_work_order_lock,
)


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_alarm(conn, suffix: str) -> tuple[int, str, dict]:
    user = _admin(conn)
    channel = conn.execute(
        '''SELECT tc.id channel_id,tc.asset_id,a.location_id,l.site_id
           FROM telemetry_channels tc
           JOIN assets a ON a.id=tc.asset_id
           LEFT JOIN locations l ON l.id=a.location_id
           ORDER BY tc.id LIMIT 1'''
    ).fetchone()
    assert channel
    alarm_no = f'ALM-CAS-{suffix}'
    created = conn.execute(
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
            f'Atomic alarm {suffix}',
            101.0,
            100.0,
            now(),
            now(),
        ),
    )
    return int(created.lastrowid), alarm_no, user


def test_application_uses_shared_work_order_number_allocator():
    assert _application.next_no is next_no_with_work_order_lock


def test_concurrent_alarm_work_creation_converges_on_one_work_order():
    suffix = uuid.uuid4().hex[:10]
    with TestClient(app):
        with db() as conn:
            alarm_id, alarm_no, user = _seed_alarm(conn, suffix)

        barrier = threading.Barrier(WORKERS)
        results: list[dict] = []
        errors: list[BaseException] = []
        body = AlarmWorkOrderIn(notes='alarm race regression')

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(
                        create_alarm_work_order_atomic(
                            conn, alarm_id, body, user
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == WORKERS
        assert sum(not result['existing'] for result in results) == 1
        assert len({int(result['id']) for result in results}) == 1
        assert len({str(result['wo_no']) for result in results}) == 1

        with db() as conn:
            alarm = conn.execute(
                'SELECT work_order_id FROM operational_alarms WHERE id=?',
                (alarm_id,),
            ).fetchone()
            work_id = int(alarm['work_order_id'])
            work = conn.execute(
                'SELECT wo_no,status FROM work_orders WHERE id=?', (work_id,)
            ).fetchone()
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
            outbox = int(
                conn.execute(
                    """SELECT COUNT(*) FROM event_outbox
                       WHERE event_type='operations.alarm.work_order_created'
                         AND aggregate_type='alarm' AND aggregate_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )

        assert work['status'] == 'Submitted'
        assert work['wo_no'] == results[0]['wo_no']
        assert approvals == workflows == audits == outbox == 1


def test_concurrent_independent_work_number_allocations_are_unique():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app):
        with db() as conn:
            user = _admin(conn)

        barrier = threading.Barrier(WORKERS)
        numbers: list[str] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    number = _application.next_no(
                        conn, 'work_orders', 'wo_no', 'WO-', 10026
                    )
                    conn.execute(
                        '''INSERT INTO work_orders(
                             wo_no,title,priority,status,work_type,requested_by,
                             created_at,updated_at
                           ) VALUES(?,?,'Medium','Draft','Corrective Maintenance',?,?,?)''',
                        (
                            number,
                            f'WO numbering {suffix}-{index}',
                            user['id'],
                            now(),
                            now(),
                        ),
                    )
                    numbers.append(number)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(WORKERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(numbers) == WORKERS
        assert len(set(numbers)) == WORKERS
