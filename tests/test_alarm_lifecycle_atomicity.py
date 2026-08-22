from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.alarm_authorization import ALARM_ACKNOWLEDGE_ROLES, ALARM_CLOSE_ROLES
from app.alarm_lifecycle_store import (
    AlarmLifecycleConflict,
    acknowledge_alarm_atomic,
    close_alarm_atomic,
)
from app.authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY
from app.database import db, now
from app.main import app


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_alarm(conn, suffix: str) -> tuple[dict, int, str]:
    user = _admin(conn)
    asset = conn.execute(
        '''SELECT a.id,l.site_id FROM assets a
           LEFT JOIN locations l ON l.id=a.location_id
           ORDER BY a.id LIMIT 1'''
    ).fetchone()
    assert asset
    stamp = now()
    channel = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             active,created_at,updated_at
           ) VALUES(?,?,?,'Temperature','C','CI',1,?,?)''',
        (
            f'CH-ALC-{suffix}',
            asset['id'],
            f'Alarm lifecycle channel {suffix}',
            stamp,
            stamp,
        ),
    )
    alarm_no = f'ALM-LC-{suffix}'
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
            f'Alarm lifecycle race {suffix}',
            110.0,
            90.0,
            stamp,
            stamp,
        ),
    )
    return user, int(alarm.lastrowid), alarm_no


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=25)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == workers
    return results


def test_concurrent_acknowledge_commits_one_audit_and_replays_state():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, alarm_id, alarm_no = _seed_alarm(conn, suffix)

        def acknowledge() -> dict:
            with db() as conn:
                return acknowledge_alarm_atomic(conn, alarm_id, user)

        results = _race(acknowledge)
        assert all(result == {'ok': True, 'status': 'Acknowledged'} for result in results)
        with db() as conn:
            alarm = conn.execute(
                '''SELECT status,acknowledged_at,acknowledged_by
                   FROM operational_alarms WHERE id=?''',
                (alarm_id,),
            ).fetchone()
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Utilities Operations'
                         AND action='ACKNOWLEDGE ALARM' AND record_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
        assert alarm['status'] == 'Acknowledged'
        assert alarm['acknowledged_at']
        assert int(alarm['acknowledged_by']) == int(user['id'])
        assert audits == 1


def test_concurrent_close_commits_one_event_and_audit():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, alarm_id, alarm_no = _seed_alarm(conn, suffix)

        def close() -> dict:
            with db() as conn:
                return close_alarm_atomic(conn, alarm_id, user)

        results = _race(close)
        assert all(result == {'ok': True, 'status': 'Closed'} for result in results)
        with db() as conn:
            alarm = conn.execute(
                'SELECT status,closed_at,closed_by FROM operational_alarms WHERE id=?',
                (alarm_id,),
            ).fetchone()
            audits = int(
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
        assert alarm['status'] == 'Closed'
        assert alarm['closed_at']
        assert int(alarm['closed_by']) == int(user['id'])
        assert audits == 1
        assert events == 1


def test_acknowledge_racing_close_never_regresses_closed_state():
    with TestClient(app):
        for round_no in range(6):
            suffix = f'{uuid.uuid4().hex[:8]}{round_no}'
            with db() as conn:
                user, alarm_id, alarm_no = _seed_alarm(conn, suffix)

            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            errors: list[BaseException] = []

            def acknowledge() -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        acknowledge_alarm_atomic(conn, alarm_id, user)
                    outcomes.append('ack')
                except AlarmLifecycleConflict:
                    outcomes.append('ack_conflict')
                except BaseException as exc:
                    errors.append(exc)

            def close() -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        close_alarm_atomic(conn, alarm_id, user)
                    outcomes.append('close')
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=acknowledge), threading.Thread(target=close)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=25)

            assert not any(thread.is_alive() for thread in threads)
            assert errors == []
            assert 'close' in outcomes
            assert any(value in outcomes for value in ('ack', 'ack_conflict'))

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
                ack_audits = int(
                    conn.execute(
                        """SELECT COUNT(*) FROM audit_logs
                           WHERE module='Utilities Operations'
                             AND action='ACKNOWLEDGE ALARM' AND record_id=?""",
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
            assert alarm['status'] == 'Closed'
            assert close_audits == 1
            assert ack_audits in (0, 1)
            assert events == 1


def test_terminal_replays_do_not_duplicate_alarm_evidence():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, alarm_id, alarm_no = _seed_alarm(conn, suffix)
            first_ack = acknowledge_alarm_atomic(conn, alarm_id, user)
        assert first_ack == {'ok': True, 'status': 'Acknowledged'}
        with db() as conn:
            second_ack = acknowledge_alarm_atomic(conn, alarm_id, user)
            first_close = close_alarm_atomic(conn, alarm_id, user)
        assert second_ack == {'ok': True, 'status': 'Acknowledged'}
        assert first_close == {'ok': True, 'status': 'Closed'}
        with db() as conn:
            second_close = close_alarm_atomic(conn, alarm_id, user)
        assert second_close == {'ok': True, 'status': 'Closed'}

        with db() as conn:
            ack_audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE action='ACKNOWLEDGE ALARM' AND record_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
            close_audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE action='CLOSE ALARM' AND record_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
            events = int(
                conn.execute(
                    """SELECT COUNT(*) FROM event_outbox
                       WHERE event_type='operations.alarm.closed'
                         AND aggregate_id=?""",
                    (alarm_no,),
                ).fetchone()[0]
            )
        assert ack_audits == 1
        assert close_audits == 1
        assert events == 1


def test_alarm_lifecycle_capabilities_match_historical_roles_and_routes_are_unique():
    assert PERMISSION_CATALOG['alarms.acknowledge'][1] == ALARM_ACKNOWLEDGE_ROLES
    assert PERMISSION_CATALOG['alarms.close'][1] == ALARM_CLOSE_ROLES
    assert ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/acknowledge')
    ] == 'alarms.acknowledge'
    assert ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/close')
    ] == 'alarms.close'

    for path in (
        '/api/alarms/{alarm_id}/acknowledge',
        '/api/alarms/{alarm_id}/close',
    ):
        routes = [
            route
            for route in app.router.routes
            if getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        ]
        assert len(routes) == 1
