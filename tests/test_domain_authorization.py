"""Authorization contract tests for domain-specific RBAC modules.

Covers: alarm_authorization, inspection_authorization,
reservation_authorization, workflow_authorization.

Each module defines permission catalog entries and route overlays that gate
specific mutations.  These tests verify:
  1. Revoking the capability blocks the mutation immediately.
  2. Granting the capability to a role that is outside the legacy role
     set does NOT elevate that role past the legacy ceiling.
  3. Anonymous callers are always denied.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.authorization import permission_codes_for_role, replace_role_permissions
from app.database import db
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return _bearer(resp.json()['token'])


# ---------------------------------------------------------------------------
# Helpers: create minimal seed data with unique suffixes to avoid collisions
# ---------------------------------------------------------------------------

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _seed_alarm(conn) -> int:
    u = _uid()
    cur = conn.execute(
        "INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,status,"
        "alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count)"
        " VALUES(?,1,1,1,'Critical','Open','Threshold','Test alarm',100.0,90.0,"
        "datetime('now'),datetime('now'),1)",
        (f'A-{u}',),
    )
    return cur.lastrowid


def _seed_inspection(conn) -> int:
    u = _uid()
    cur = conn.execute(
        "INSERT INTO inspections(inspection_no,template_name,asset_id,status,created_at)"
        " VALUES(?,'Routine Check',1,'Open',datetime('now'))",
        (f'I-{u}',),
    )
    return cur.lastrowid


def _seed_reservation(conn) -> int:
    u = _uid()
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    wo_cur = conn.execute(
        "INSERT INTO work_orders(wo_no,title,priority,status,work_type,requested_by,created_at,updated_at)"
        " VALUES(?,'Reservation Test WO','Medium','Open','Corrective',1,?,?)",
        (f'WO-R-{u}', now, now),
    )
    wo_id = wo_cur.lastrowid
    item_cur = conn.execute(
        "INSERT INTO inventory_items(item_no,name,category,warehouse_id,current_stock,reserved_stock,reorder_point,unit_price,unit)"
        " VALUES(?,'Reservation test item','Parts',1,50.0,0.0,10,10.0,'ea')",
        (f'IT-R-{u}',),
    )
    item_id = item_cur.lastrowid
    res_cur = conn.execute(
        "INSERT INTO inventory_reservations(reservation_no,work_order_id,inventory_item_id,quantity,reserved_by,reserved_at)"
        " VALUES(?,?,?,5,1,?)",
        (f'RES-{u}', wo_id, item_id, now),
    )
    return res_cur.lastrowid


def _seed_dispatch(conn) -> int:
    u = _uid()
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    wo_cur = conn.execute(
        "INSERT INTO work_orders(wo_no,title,priority,status,work_type,requested_by,created_at,updated_at)"
        " VALUES(?,'Dispatch Test WO','Medium','Open','Corrective',1,?,?)",
        (f'WO-D-{u}', now, now),
    )
    wo_id = wo_cur.lastrowid
    disp_cur = conn.execute(
        "INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,dispatched_by,status,dispatched_at)"
        " VALUES(?,?,1,1,'Dispatched',?)",
        (f'D-{u}', wo_id, now),
    )
    return disp_cur.lastrowid


# ===========================================================================
# Alarm authorization tests
# ===========================================================================

class TestAlarmAuthorization:
    """Tests for alarm_authorization.py capability overlay."""

    def test_acknowledge_requires_alarm_acknowledge_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'alarms.acknowledge' in original
            with db() as conn:
                alarm_id = _seed_alarm(conn)
            baseline = client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=admin)
            assert baseline.status_code == 200, baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'alarms.acknowledge'])
                denied = client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=admin)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_close_requires_alarm_close_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'alarms.close' in original
            with db() as conn:
                alarm_id = _seed_alarm(conn)
            ack = client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=admin)
            assert ack.status_code == 200, ack.text
            baseline = client.post(f'/api/alarms/{alarm_id}/close', headers=admin)
            assert baseline.status_code == 200, baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'alarms.close'])
                denied = client.post(f'/api/alarms/{alarm_id}/close', headers=admin)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_alarm_capability_cannot_promote_executive_role(self):
        with TestClient(app) as client:
            executive = _login(client, 'exec', 'Viewer@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'executive')
            with db() as conn:
                alarm_id = _seed_alarm(conn)
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', sorted(set(original) | {'alarms.acknowledge'}))
                denied = client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=executive)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', original)

    def test_anonymous_always_denied_on_alarm_mutation(self):
        with TestClient(app) as client:
            with db() as conn:
                alarm_id = _seed_alarm(conn)
            r = client.post(f'/api/alarms/{alarm_id}/acknowledge')
            assert r.status_code == 401

    def test_alarm_work_order_requires_work_create(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'work.create' in original
            with db() as conn:
                alarm_id = _seed_alarm(conn)
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'work.create'])
                denied = client.post(f'/api/alarms/{alarm_id}/work-order', headers=admin, json={'title': 'Test WO'})
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)


# ===========================================================================
# Inspection authorization tests
# ===========================================================================

class TestInspectionAuthorization:
    """Tests for inspection_authorization.py capability overlay."""

    def test_submit_requires_inspections_submit_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'inspections.submit' in original
            with db() as conn:
                inspection_id = _seed_inspection(conn)
            body = {'responses': [{'item_name': 'Test', 'response': 'Pass'}]}
            baseline = client.post(f'/api/inspections/{inspection_id}/submit', headers=admin, json=body)
            assert baseline.status_code in (200, 409), baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'inspections.submit'])
                denied = client.post(f'/api/inspections/{inspection_id}/submit', headers=admin, json=body)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_inspection_capability_cannot_promote_executive_role(self):
        with TestClient(app) as client:
            executive = _login(client, 'exec', 'Viewer@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'executive')
            with db() as conn:
                inspection_id = _seed_inspection(conn)
            body = {'responses': [{'item_name': 'Test', 'response': 'Pass'}]}
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', sorted(set(original) | {'inspections.submit'}))
                denied = client.post(f'/api/inspections/{inspection_id}/submit', headers=executive, json=body)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', original)

    def test_anonymous_always_denied_on_inspection_submit(self):
        with TestClient(app) as client:
            with db() as conn:
                inspection_id = _seed_inspection(conn)
            body = {'responses': [{'item_name': 'Test', 'response': 'Pass'}]}
            r = client.post(f'/api/inspections/{inspection_id}/submit', json=body)
            assert r.status_code == 401


# ===========================================================================
# Reservation authorization tests
# ===========================================================================

class TestReservationAuthorization:
    """Tests for reservation_authorization.py capability overlay."""

    def test_issue_requires_work_material_issue_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'work.material.issue' in original
            with db() as conn:
                reservation_id = _seed_reservation(conn)
            baseline = client.post(f'/api/reservations/{reservation_id}/issue', headers=admin, json={'qty': 2})
            assert baseline.status_code in (200, 409), baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'work.material.issue'])
                denied = client.post(f'/api/reservations/{reservation_id}/issue', headers=admin, json={'qty': 2})
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_release_requires_work_material_reserve_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'work.material.reserve' in original
            with db() as conn:
                reservation_id = _seed_reservation(conn)
            baseline = client.post(f'/api/reservations/{reservation_id}/release', headers=admin)
            assert baseline.status_code in (200, 409), baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'work.material.reserve'])
                denied = client.post(f'/api/reservations/{reservation_id}/release', headers=admin)
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_reservation_capability_cannot_promote_executive_role(self):
        with TestClient(app) as client:
            executive = _login(client, 'exec', 'Viewer@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'executive')
            with db() as conn:
                reservation_id = _seed_reservation(conn)
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', sorted(set(original) | {'work.material.issue'}))
                denied = client.post(f'/api/reservations/{reservation_id}/issue', headers=executive, json={'qty': 1})
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', original)

    def test_anonymous_always_denied_on_reservation_mutation(self):
        with TestClient(app) as client:
            with db() as conn:
                reservation_id = _seed_reservation(conn)
            r = client.post(f'/api/reservations/{reservation_id}/issue', json={'qty': 1})
            assert r.status_code == 401


# ===========================================================================
# Workflow / Dispatch authorization tests
# ===========================================================================

class TestWorkflowAuthorization:
    """Tests for workflow_authorization.py capability overlay."""

    def test_dispatch_transition_requires_work_dispatch_transition_capability(self):
        with TestClient(app) as client:
            admin = _login(client, 'omar', 'EUAS@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'admin')
            assert 'work.dispatch.transition' in original
            with db() as conn:
                dispatch_id = _seed_dispatch(conn)
            baseline = client.post(f'/api/dispatch/{dispatch_id}/transition', headers=admin, json={'action': 'Accept'})
            assert baseline.status_code in (200, 409), baseline.text
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', [c for c in original if c != 'work.dispatch.transition'])
                denied = client.post(f'/api/dispatch/{dispatch_id}/transition', headers=admin, json={'action': 'Accept'})
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'admin', original)

    def test_dispatch_capability_cannot_promote_executive_role(self):
        with TestClient(app) as client:
            executive = _login(client, 'exec', 'Viewer@2026')
            with db() as conn:
                original = permission_codes_for_role(conn, 'executive')
            with db() as conn:
                dispatch_id = _seed_dispatch(conn)
            try:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', sorted(set(original) | {'work.dispatch.transition'}))
                denied = client.post(f'/api/dispatch/{dispatch_id}/transition', headers=executive, json={'action': 'Accept'})
                assert denied.status_code == 403, denied.text
            finally:
                with db() as conn:
                    replace_role_permissions(conn, 'executive', original)

    def test_anonymous_always_denied_on_dispatch_transition(self):
        with TestClient(app) as client:
            with db() as conn:
                dispatch_id = _seed_dispatch(conn)
            r = client.post(f'/api/dispatch/{dispatch_id}/transition', json={'action': 'Accept'})
            assert r.status_code == 401

    def test_technician_can_transition_dispatch_when_granted(self):
        with TestClient(app) as client:
            tech = _login(client, 'tech1', 'Tech@2026')
            with db() as conn:
                tech_user = conn.execute("SELECT id FROM users WHERE username='tech1'").fetchone()
                tech_uid = tech_user[0]
                u = _uid()
                now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                wo_cur = conn.execute(
                    "INSERT INTO work_orders(wo_no,title,priority,status,work_type,requested_by,created_at,updated_at)"
                    " VALUES(?,'Dispatch Tech WO','Medium','Open','Corrective',1,?,?)",
                    (f'WO-DT-{u}', now, now),
                )
                wo_id = wo_cur.lastrowid
                disp_cur = conn.execute(
                    "INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,dispatched_by,status,dispatched_at)"
                    " VALUES(?,?,?,1,'Dispatched',?)",
                    (f'DT-{u}', wo_id, tech_uid, now),
                )
                dispatch_id = disp_cur.lastrowid
            resp = client.post(f'/api/dispatch/{dispatch_id}/transition', headers=tech, json={'action': 'Accept'})
            assert resp.status_code in (200, 409), resp.text
