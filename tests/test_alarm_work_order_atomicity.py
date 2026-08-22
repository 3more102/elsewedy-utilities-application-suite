from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.application import AlarmWorkOrderIn
from app.authorization import ROUTE_PERMISSION_OVERLAY
from app.database import db, now
from app.main import app
from app.alarm_store import create_alarm_work_order_atomic


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_alarm(conn, suffix: str) -> tuple[dict, int, str, str]:
    user = _admin(conn)
    asset = conn.execute(
        '''SELECT a.id,l.site_id FROM assets a
           LEFT JOIN locations l ON l.id=a.location_id
           ORDER BY a.id LIMIT 1'''
    ).fetchone()
    assert asset
    stamp = now()
    channel_code = f'CH-ALARM-CAS-{suffix}'
    channel = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             active,created_at,updated_at
           ) VALUES(?,?,?,'Temperature','C','CI',1,?,?)''',
        (
            channel_code,
            asset['id'],
            f'Alarm atomicity channel {suffix}',
            stamp,
            stamp,
        ),
    )
    alarm_no = f'ALM-CAS-{suffix}'
    alarm = conn.execute(
        '''INSERT INTO operational_alarms(
             alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,
             message,trigger_value,threshold_value,opened_at,last_seen_at,
             occurrence_count
           ) VALUES(?,?,?,?,'Critical','Open','Threshold',?,?,?, ?,?,1)''',
        (
            alarm_no,
            channel.lastrowid,
            asset['id'],
            asset['site_id'],
            f'Alarm work-order race {suffix}',
            110.0,
            90.0,
            stamp,
            stamp,
        ),
    )
    return user, int(alarm.lastrowid), alarm_no, channel_code


def test_alarm_work_order_concurrent_requests_create_one_business_record():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, alarm_id, alarm_no, channel_code = _seed_alarm(conn, suffix)

        barrier = threading.Barrier(WORKERS)
        results: list[dict] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(
                        create_alarm_work_order_atomic(
                            conn,
                            alarm_id,
                            AlarmWorkOrderIn(notes='atomic alarm work-order test'),
                            user,
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == WORKERS
        assert sum(1 for result in results if result['existing'] is False) == 1
        assert sum(1 for result in results if result['existing'] is True) == WORKERS - 1
        assert len({int(result['id']) for result in results}) == 1
        assert len({str(result['wo_no']) for result in results}) == 1

        with db() as conn:
            alarm = conn.execute(
                'SELECT work_order_id FROM operational_alarms WHERE id=?',
                (alarm_id,),
            ).fetchone()
            assert alarm and alarm['work_order_id']
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
            events = int(
                conn.execute(
                    """SELECT COUNT(*) FROM event_outbox
                       WHERE event_type='operations.alarm.work_order_created'
                         AND aggregate_type='alarm' AND aggregate_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )

        assert works == 1
        assert approvals == 1
        assert workflows == 1
        assert audits == 1
        assert events == 1


def test_global_work_order_number_allocator_serializes_independent_creators():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)

        barrier = threading.Barrier(WORKERS)
        numbers: list[str] = []
        errors: list[BaseException] = []

        def create(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    number = _application.next_no(
                        conn, 'work_orders', 'wo_no', 'WO-', 10026
                    )
                    stamp = now()
                    conn.execute(
                        '''INSERT INTO work_orders(
                             wo_no,title,priority,status,work_type,requested_by,
                             created_at,updated_at
                           ) VALUES(?,?,'Medium','Draft','Corrective Maintenance',?,?,?)''',
                        (
                            number,
                            f'WO number race {suffix}-{index}',
                            user['id'],
                            stamp,
                            stamp,
                        ),
                    )
                numbers.append(number)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(numbers) == WORKERS
        assert len(set(numbers)) == WORKERS
        with db() as conn:
            persisted = int(
                conn.execute(
                    'SELECT COUNT(*) FROM work_orders WHERE title LIKE ?',
                    (f'WO number race {suffix}-%',),
                ).fetchone()[0]
            )
        assert persisted == WORKERS


def test_alarm_work_order_route_uses_existing_work_create_capability():
    assert ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/work-order')
    ] == 'work.create'
    routes = [
        route
        for route in app.router.routes
        if getattr(route, 'path', None) == '/api/alarms/{alarm_id}/work-order'
        and 'POST' in set(getattr(route, 'methods', set()) or set())
    ]
    assert len(routes) == 1
