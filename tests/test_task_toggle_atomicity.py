from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    response = client.post(
        '/api/auth/login', json={'username': username, 'password': password}
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _task_audit_count(wo_no: str, action: str) -> int:
    with db() as conn:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Work Management' AND action=?
                     AND record_id=?""",
                (action, wo_no),
            ).fetchone()[0]
        )


def _seed_task(client, headers) -> tuple[int, str]:
    asset = next(x for x in client.get('/api/assets', headers=headers).json())
    wo = client.post(
        '/api/work-orders',
        headers=headers,
        json={
            'title': f'Task toggle regression {uuid.uuid4().hex[:8]}',
            'asset_id': asset['id'],
            'priority': 'Low',
            'instructions': '',
            'checklist': 'Verify insulator torque\nPhotograph seal',
        },
    )
    assert wo.status_code == 200, wo.text
    detail = client.get(f"/api/work-orders/{wo.json()['id']}", headers=headers).json()
    tasks = detail['tasks']
    assert tasks, 'seeded checklist must create tasks'
    return int(wo.json()['id']), int(tasks[0]['id'])


def test_toggle_transition_is_audited_once_and_reversible():
    with TestClient(app) as client:
        headers = _auth(client)
        wo_id, task_id = _seed_task(client, headers)
        with db() as conn:
            wo_no = conn.execute(
                'SELECT wo_no FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()[0]

        first = client.post(
            f'/api/work-orders/{wo_id}/tasks/{task_id}/toggle', headers=headers
        )
        assert first.status_code == 200, first.text
        assert first.json() == {'ok': True, 'status': 'Completed'}
        assert _task_audit_count(wo_no, 'TASK COMPLETED') == 1
        with db() as conn:
            row = dict(
                conn.execute(
                    'SELECT status,completed_at FROM work_order_tasks WHERE id=?',
                    (task_id,),
                ).fetchone()
            )
        assert row['status'] == 'Completed' and row['completed_at']

        # Reverse transition works and produces exactly one reverse audit.
        second = client.post(
            f'/api/work-orders/{wo_id}/tasks/{task_id}/toggle', headers=headers
        )
        assert second.status_code == 200 and second.json()['status'] == 'Pending'
        with db() as conn:
            row = dict(
                conn.execute(
                    'SELECT status,completed_at FROM work_order_tasks WHERE id=?',
                    (task_id,),
                ).fetchone()
            )
        assert row['status'] == 'Pending' and row['completed_at'] is None
        assert _task_audit_count(wo_no, 'TASK PENDING') == 1


def test_concurrent_identical_toggles_commit_one_transition():
    with TestClient(app) as client:
        headers = _auth(client)
        wo_id, task_id = _seed_task(client, headers)
        with db() as conn:
            wo_no = conn.execute(
                'SELECT wo_no FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()[0]
            completed_before = _task_audit_count(wo_no, 'TASK COMPLETED')

        workers = 6
        barrier = threading.Barrier(workers)
        statuses: list[int] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    f'/api/work-orders/{wo_id}/tasks/{task_id}/toggle',
                    headers=headers,
                )
                statuses.append(response.status_code)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert statuses.count(200) >= 1
        assert statuses.count(409) == workers - statuses.count(200)

        # Every committed toggle is a distinct real transition carrying exactly
        # one audit record; no transition may produce duplicate evidence.
        completed_after = _task_audit_count(wo_no, 'TASK COMPLETED')
        pending_audits = _task_audit_count(wo_no, 'TASK PENDING')
        assert completed_before == 0
        assert completed_after + pending_audits == statuses.count(200)

        # Final state must match the parity of committed transitions.
        with db() as conn:
            final_status = conn.execute(
                'SELECT status FROM work_order_tasks WHERE id=?', (task_id,)
            ).fetchone()[0]
        expected = 'Completed' if completed_after > pending_audits else 'Pending'
        assert final_status == expected
        if final_status == 'Completed':
            assert completed_after >= 1


def test_toggle_missing_task_and_work_order_behavior_unchanged():
    with TestClient(app) as client:
        headers = _auth(client)
        wo_id, task_id = _seed_task(client, headers)

        missing_task = client.post(
            f'/api/work-orders/{wo_id}/tasks/999999/toggle', headers=headers
        )
        assert missing_task.status_code == 404

        missing_wo = client.post(
            f'/api/work-orders/999999/tasks/{task_id}/toggle', headers=headers
        )
        assert missing_wo.status_code == 404


def test_toggle_route_is_registered_exactly_once_with_overlay():
    from app.authorization import ROUTE_PERMISSION_OVERLAY

    with TestClient(app):
        key_path = '/api/work-orders/{wo_id}/tasks/{task_id}/toggle'
        matches = [
            route
            for route in app.routes
            if getattr(route, 'path', None) == key_path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        ]
        assert len(matches) == 1
        assert ROUTE_PERMISSION_OVERLAY[('POST', key_path)] == 'work.tasks.manage'
