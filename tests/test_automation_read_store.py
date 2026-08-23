from __future__ import annotations

from datetime import date
import json
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db, now
from app.main import app


def _auth(client: TestClient, username='omar', password='EUAS@2026') -> dict[str, str]:
    response = client.post(
        '/api/auth/login',
        json={'username': username, 'password': password},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _route(method: str, path: str):
    matches = [
        route
        for route in app.router.routes
        if getattr(route, 'path', None) == path
        and method in set(getattr(route, 'methods', set()) or set())
    ]
    assert len(matches) == 1
    return matches[0]


def test_automation_read_routes_are_owned_by_focused_store_once():
    with TestClient(app):
        status = _route('GET', '/api/automation/status')
        runs = _route('GET', '/api/automation/runs')
        assert status.endpoint.__module__ == 'app.automation_read_store'
        assert runs.endpoint.__module__ == 'app.automation_read_store'
        assert _application.automation_status is status.endpoint
        assert _application.automation_runs is runs.endpoint


def test_automation_status_preserves_established_queue_read_model():
    with TestClient(app) as client:
        headers = _auth(client)
        response = client.get('/api/automation/status', headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()

        with db() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE status='Pending'"
            ).fetchone()[0]
            due_pm = conn.execute(
                '''SELECT COUNT(*) FROM maintenance_plans
                   WHERE active=1 AND trigger_type='Calendar'
                     AND next_due IS NOT NULL AND next_due<=?''',
                (date.today().isoformat(),),
            ).fetchone()[0]
            low = conn.execute(
                '''SELECT COUNT(*) FROM inventory_items
                   WHERE current_stock-reserved_stock<=reorder_point'''
            ).fetchone()[0]
            overdue = conn.execute(
                '''SELECT COUNT(*) FROM work_orders
                   WHERE target_finish IS NOT NULL AND target_finish<?
                     AND status NOT IN ('Completed','Closed','Cancelled')''',
                (date.today().isoformat(),),
            ).fetchone()[0]
            sla = conn.execute(
                '''SELECT COUNT(*) FROM work_order_sla s
                   JOIN work_orders w ON w.id=s.work_order_id
                   WHERE w.status NOT IN ('Completed','Closed','Cancelled')
                     AND (s.response_status='Breached' OR s.resolution_status='Breached')'''
            ).fetchone()[0]
            outbox_pending = conn.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed')"
            ).fetchone()[0]
            outbox_exhausted = conn.execute(
                '''SELECT COUNT(*) FROM event_outbox
                   WHERE status IN ('Pending','Failed') AND attempts>=?''',
                (_application.OUTBOX_MAX_ATTEMPTS,),
            ).fetchone()[0]

        assert body['version'] == _application.APP_VERSION
        assert body['scheduler_enabled'] is (_application.AUTOMATION_INTERVAL_MINUTES > 0)
        assert body['interval_minutes'] == _application.AUTOMATION_INTERVAL_MINUTES
        assert body['webhook_configured'] is bool(_application.EVENT_WEBHOOK_URL)
        assert body['queue'] == {
            'due_pm': due_pm,
            'low_stock': low,
            'overdue_work': overdue,
            'pending_approvals': pending,
            'sla_breaches': sla,
            'outbox_pending': outbox_pending,
            'outbox_exhausted': outbox_exhausted,
        }


def test_automation_runs_preserves_descending_order_and_limit_bounds():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:10]
        run_nos = [f'JOB-READ-{suffix}-{index}' for index in range(3)]
        with db() as conn:
            admin = conn.execute(
                "SELECT id FROM users WHERE username='omar'"
            ).fetchone()
            assert admin
            for index, run_no in enumerate(run_nos):
                conn.execute(
                    '''INSERT INTO job_runs(
                         run_no,trigger_source,status,actor_id,as_of,
                         started_at,finished_at,summary_json
                       ) VALUES(?,?,?,?,?,?,?,?)''',
                    (
                        run_no,
                        'test',
                        'Succeeded',
                        admin['id'],
                        date.today().isoformat(),
                        now(),
                        now(),
                        json.dumps({'index': index}),
                    ),
                )

        response = client.get('/api/automation/runs', headers=headers, params={'limit': 2})
        assert response.status_code == 200, response.text
        returned = response.json()
        assert len(returned) == 2
        assert [item['run_no'] for item in returned] == [run_nos[2], run_nos[1]]

        assert client.get('/api/automation/runs', headers=headers, params={'limit': 0}).status_code == 422
        assert client.get('/api/automation/runs', headers=headers, params={'limit': 201}).status_code == 422

        with db() as conn:
            conn.execute(
                'DELETE FROM job_runs WHERE run_no IN (?,?,?)',
                tuple(run_nos),
            )


def test_automation_read_routes_preserve_historical_role_ceiling():
    with TestClient(app) as client:
        technician = _auth(client, 'tech1', 'Tech@2026')
        assert client.get('/api/automation/status', headers=technician).status_code == 403
        assert client.get('/api/automation/runs', headers=technician).status_code == 403
