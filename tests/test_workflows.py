import hashlib
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ['EUAS_DB_PATH'] = str(TEST_DB)

from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_euas_end_to_end():
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        users = {x['username']: x['id'] for x in ref['users']}
        assets = client.get('/api/assets', headers=admin, params={'q': 'TR-001'}).json()
        tr = next(x for x in assets if x['asset_no'] == 'TR-001')

        # Required demo record exists.
        wo_demo = next(x for x in client.get('/api/work-orders', headers=admin).json() if x['wo_no'] == 'WO-10025')
        assert wo_demo['asset_no'] == 'TR-001'
        assert wo_demo['priority'] == 'High'

        # Workflow 1: create -> approve -> assign -> execute -> labor/part -> complete -> close.
        r = client.post('/api/work-orders', headers=admin, json={
            'title': 'Regression transformer maintenance', 'asset_id': tr['id'], 'priority': 'High',
            'work_type': 'Corrective Maintenance', 'assigned_to': users['tech1'], 'supervisor_id': users['supervisor'],
            'estimated_hours': 3, 'target_start': '2026-08-19', 'target_finish': '2026-08-20',
            'safety_requirements': 'PTW and electrical PPE', 'instructions': 'Inspect transformer cooling and oil system.'
        })
        assert r.status_code == 200, r.text
        wid = r.json()['id']
        for action in ('submit', 'approve', 'assign'):
            rr = client.post(f'/api/work-orders/{wid}/transition', headers=admin, json={'action': action})
            assert rr.status_code == 200, rr.text

        tech = auth(client, 'tech1', 'Tech@2026')
        assert client.post(f'/api/work-orders/{wid}/transition', headers=tech, json={'action': 'start'}).status_code == 200
        assert client.post(f'/api/work-orders/{wid}/labor', headers=tech, json={'hours': 2.5, 'labor_rate': 30, 'notes': 'Diagnostics'}).status_code == 200
        inv = client.get('/api/inventory', headers=tech).json()
        oil = next(x for x in inv if x['item_no'] == 'OIL-FLT-TR')
        assert client.post(f'/api/work-orders/{wid}/materials', headers=tech, json={'item_id': oil['id'], 'quantity': 1}).status_code == 200
        assert client.post(f'/api/work-orders/{wid}/notes', headers=tech, json={'note': 'Oil system checked.'}).status_code == 200
        assert client.post(f'/api/field/assets/{tr["id"]}/condition-meter', headers=tech, json={'condition': 'Warning', 'meter_reading': 79.0}).status_code == 200
        assert client.post(f'/api/work-orders/{wid}/transition', headers=tech, json={'action': 'complete', 'notes': 'Completed and tested.', 'signature': 'Mahmoud Ali'}).status_code == 200
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        assert client.post(f'/api/work-orders/{wid}/transition', headers=supervisor, json={'action': 'close'}).status_code == 200
        done = client.get(f'/api/work-orders/{wid}', headers=admin).json()
        assert done['status'] == 'Closed'
        assert done['actual_hours'] == 2.5
        assert done['actual_cost'] > 0
        assert done['technician_signature'] == 'Mahmoud Ali'
        assert client.get(f'/api/work-orders/{wid}/report', headers=admin).status_code == 200

        # Newly created checklist persists every task, not only the first one.
        checklist_wo = client.post('/api/work-orders', headers=admin, json={
            'title': 'Checklist regression', 'asset_id': tr['id'], 'priority': 'Medium',
            'checklist': 'Isolate equipment;Inspect terminals;Record readings'
        })
        assert checklist_wo.status_code == 200, checklist_wo.text
        checklist_detail = client.get(f"/api/work-orders/{checklist_wo.json()['id']}", headers=admin).json()
        assert [x['task'] for x in checklist_detail['tasks']] == ['Isolate equipment', 'Inspect terminals', 'Record readings']

        # New digital inspection templates persist all configured inspection items.
        new_inspection = client.post('/api/inspections', headers=admin, json={
            'template_name': 'Regression Transformer Inspection', 'asset_id': tr['id'],
            'items': ['Oil Level', 'Temperature', 'Grounding']
        })
        assert new_inspection.status_code == 200, new_inspection.text
        new_inspection_detail = client.get(f"/api/inspections/{new_inspection.json()['id']}", headers=admin).json()
        assert [x['item_name'] for x in new_inspection_detail['items']] == ['Oil Level', 'Temperature', 'Grounding']

        # Warehouse transfer decrements source and creates/increments a destination stock record.
        inv_before_transfer = client.get('/api/inventory', headers=admin).json()
        source_item = next(x for x in inv_before_transfer if x['item_no'] == 'AHU-FLT-600')
        destination = next(x for x in ref['warehouses'] if x['id'] != source_item['warehouse_id'])
        transfer = client.post(f"/api/inventory/{source_item['id']}/transaction", headers=admin, json={
            'tx_type': 'TRANSFER', 'quantity': 2, 'to_warehouse_id': destination['id'], 'reference': 'QA-TRANSFER'
        })
        assert transfer.status_code == 200, transfer.text
        inv_after_transfer = client.get('/api/inventory', headers=admin).json()
        source_after = next(x for x in inv_after_transfer if x['id'] == source_item['id'])
        assert source_after['current_stock'] == source_item['current_stock'] - 2
        destination_match = [x for x in inv_after_transfer if x['warehouse_id'] == destination['id'] and x['name'] == source_item['name']]
        assert destination_match and destination_match[0]['current_stock'] >= 2

        # Workflow 2: low stock -> automatic purchase requisition.
        before = len(client.get('/api/procurement', headers=admin).json()['requisitions'])
        reorder = client.post('/api/inventory/reorder-scan', headers=admin)
        assert reorder.status_code == 200, reorder.text
        assert reorder.json()['count'] >= 1
        after = len(client.get('/api/procurement', headers=admin).json()['requisitions'])
        assert after > before

        # Procurement lifecycle: approve -> supplier quote -> PO -> receipt -> inventory increase.
        proc = client.get('/api/procurement', headers=admin).json()
        pr = next(x for x in proc['requisitions'] if x['status'] == 'Submitted')
        assert client.post(f"/api/procurement/requisitions/{pr['id']}/approve", headers=admin).status_code == 200
        vendor = client.post('/api/vendors', headers=admin, json={'name': 'Regression Vendor', 'category': 'Electrical'}).json()
        assert client.post('/api/procurement/quotations', headers=admin, json={'pr_id': pr['id'], 'vendor_id': vendor['id'], 'amount': max(pr['total_estimate'], 1)}).status_code == 200
        po = client.post('/api/procurement/purchase-orders', headers=admin, json={'pr_id': pr['id'], 'vendor_id': vendor['id'], 'expected_delivery': '2026-08-25'})
        assert po.status_code == 200, po.text
        assert client.post(f"/api/procurement/purchase-orders/{po.json()['id']}/receive", headers=admin).status_code == 200

        # Workflow 3: overdue PM -> generated preventive WO.
        pm = client.post('/api/maintenance-plans/generate', headers=admin)
        assert pm.status_code == 200, pm.text
        assert pm.json()['count'] >= 1

        # Workflow 4: failed inspection -> corrective WO.
        inspection = client.get('/api/inspections', headers=tech).json()[0]
        detail = client.get(f"/api/inspections/{inspection['id']}", headers=tech).json()
        responses = [
            {'id': x['id'], 'response': 'Fail' if x['item_name'] == 'Temperature' else 'Pass', 'reading': '92 C' if x['item_name'] == 'Temperature' else ''}
            for x in detail['items']
        ]
        failed = client.post(f"/api/inspections/{inspection['id']}/submit", headers=tech, json={'responses': responses, 'remarks': 'High oil temperature', 'create_corrective_on_fail': True})
        assert failed.status_code == 200, failed.text
        assert failed.json()['result'] == 'Fail'
        assert failed.json()['corrective_work_order_id']

        # CRUD and governance.
        new_asset = client.post('/api/assets', headers=admin, json={'asset_no': 'QA-DELETE-01', 'name': 'QA Disposable Asset', 'category': 'QA', 'criticality': 'Low', 'condition': 'Good', 'status': 'Operating'})
        assert new_asset.status_code == 200, new_asset.text
        new_id = new_asset.json()['id']
        assert client.patch(f'/api/assets/{new_id}', headers=admin, json={'condition': 'Fair'}).status_code == 200
        assert client.delete(f'/api/assets/{new_id}', headers=admin).status_code == 200

        hse = client.post('/api/hse', headers=admin, json={'incident_type': 'Hazard', 'title': 'Regression HSE record', 'severity': 3, 'probability': 2, 'description': 'Controlled test hazard.'})
        assert hse.status_code == 200, hse.text
        assert hse.json()['risk_score'] == 6

        contract = client.post('/api/contracts', headers=admin, json={'title': 'Regression Contract', 'vendor_id': vendor['id'], 'value': 10000})
        assert contract.status_code == 200, contract.text

        search = client.get('/api/search', headers=admin, params={'q': 'TR-001'}).json()
        modules = {x['module'] for x in search}
        assert {'assets', 'work', 'inspections'} <= modules

        # Unified Approval Center: submit a WO, approve it through the generic queue, and verify workflow history.
        approval_wo = client.post('/api/work-orders', headers=admin, json={
            'title': 'Approval center regression', 'asset_id': tr['id'], 'priority': 'Medium',
            'supervisor_id': users['supervisor']
        })
        assert approval_wo.status_code == 200, approval_wo.text
        approval_wid = approval_wo.json()['id']
        assert client.post(f'/api/work-orders/{approval_wid}/transition', headers=admin, json={'action': 'submit'}).status_code == 200
        approvals = client.get('/api/approvals?status=Pending', headers=admin).json()
        approval = next(x for x in approvals if x['record_type'] == 'work_order' and x['record_id'] == approval_wid)
        supervisor_headers = auth(client, 'supervisor', 'Supervisor@2026')
        decision = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor_headers, json={'decision': 'approve', 'comments': 'Approved in QA'})
        assert decision.status_code == 200, decision.text
        assert client.get(f'/api/work-orders/{approval_wid}', headers=admin).json()['status'] == 'Approved'
        history = client.get('/api/workflow-events', headers=admin, params={'module': 'Work Management', 'record_type': 'work_order', 'record_id': approval_wid}).json()
        assert {x['event'] for x in history} >= {'SUBMIT', 'APPROVE'}

        # Rejection returns work for correction and Resubmit creates a fresh approval.
        reject_wo = client.post('/api/work-orders', headers=admin, json={'title': 'Approval rejection regression', 'asset_id': tr['id'], 'supervisor_id': users['supervisor']}).json()['id']
        assert client.post(f'/api/work-orders/{reject_wo}/transition', headers=admin, json={'action': 'submit'}).status_code == 200
        rejection = next(x for x in client.get('/api/approvals?status=Pending', headers=supervisor_headers).json() if x['record_type'] == 'work_order' and x['record_id'] == reject_wo)
        assert client.post(f"/api/approvals/{rejection['id']}/decision", headers=supervisor_headers, json={'decision': 'reject', 'comments': 'Revise scope'}).status_code == 200
        assert client.get(f'/api/work-orders/{reject_wo}', headers=admin).json()['status'] == 'Rejected'
        assert client.post(f'/api/work-orders/{reject_wo}/transition', headers=admin, json={'action': 'resubmit'}).status_code == 200
        assert any(x['record_id'] == reject_wo for x in client.get('/api/approvals?status=Pending', headers=admin).json())

        # PR Draft -> Submit -> Approval Center -> Approved.
        qa_pr = client.post('/api/procurement/requisitions', headers=admin, json={
            'title': 'Approval PR regression', 'site_id': ref['sites'][0]['id'], 'justification': 'QA',
            'items': [{'description': 'QA spare', 'quantity': 2, 'estimated_unit_cost': 10}]
        })
        assert qa_pr.status_code == 200, qa_pr.text
        qa_pr_id = qa_pr.json()['id']
        assert client.post(f'/api/procurement/requisitions/{qa_pr_id}/submit', headers=admin).status_code == 200
        proc_headers = auth(client, 'proc', 'Proc@2026')
        pr_approval = next(x for x in client.get('/api/approvals?status=Pending', headers=proc_headers).json() if x['record_type'] == 'purchase_requisition' and x['record_id'] == qa_pr_id)
        assert client.post(f"/api/approvals/{pr_approval['id']}/decision", headers=proc_headers, json={'decision': 'approve'}).status_code == 200
        assert next(x for x in client.get('/api/procurement', headers=admin).json()['requisitions'] if x['id'] == qa_pr_id)['status'] == 'Approved'

        # Technicians cannot execute work assigned to another user.
        other_wo = client.post('/api/work-orders', headers=admin, json={'title': 'Assignment guard', 'asset_id': tr['id'], 'assigned_to': users['planner'], 'supervisor_id': users['supervisor']}).json()['id']
        for action in ('submit', 'approve', 'assign'):
            assert client.post(f'/api/work-orders/{other_wo}/transition', headers=admin, json={'action': action}).status_code == 200
        assert client.post(f'/api/work-orders/{other_wo}/transition', headers=tech, json={'action': 'start'}).status_code == 403

        # Health/readiness exposes database backend and schema migration version.
        health = client.get('/api/health').json(); ready = client.get('/api/health/ready').json()
        assert health['database_backend'] == 'sqlite' and health['schema_version'] >= 4
        assert ready['status'] == 'ready' and ready['checks']['assets'] >= 1

        viewer = auth(client, 'exec', 'Viewer@2026')
        denied = client.post('/api/assets', headers=viewer, json={'name': 'Forbidden', 'category': 'QA', 'criticality': 'Low', 'condition': 'Good', 'status': 'Operating'})
        assert denied.status_code == 403

        # Session expiry is enforced server-side against the digest-backed row.
        exp = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
        assert exp.status_code == 200
        expired_token = exp.json()['token']
        expired_digest = hashlib.sha256(expired_token.encode('utf-8')).hexdigest()
        import sqlite3
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute(
                "UPDATE auth_sessions SET expires_at='2000-01-01T00:00:00' WHERE token_digest=?",
                (expired_digest,),
            )
            conn.commit()
        expired = client.get('/api/auth/me', headers={'Authorization': f'Bearer {expired_token}'})
        assert expired.status_code == 401

        # Security headers are emitted for API responses.
        health = client.get('/api/health')
        assert health.status_code == 200
        assert health.headers['x-frame-options'] == 'DENY'
        assert health.headers['x-content-type-options'] == 'nosniff'
        assert health.headers['x-request-id']

        # Profile, password and session management.
        created_user = client.post('/api/admin/users', headers=admin, json={
            'username': 'qa_secure', 'password': 'SecurePass@2026', 'full_name': 'QA Secure User',
            'email': 'qa.secure@euas.local', 'role_code': 'planner', 'department': 'QA'
        })
        assert created_user.status_code == 200, created_user.text
        qa_id = created_user.json()['id']
        qa = auth(client, 'qa_secure', 'SecurePass@2026')
        qa2 = auth(client, 'qa_secure', 'SecurePass@2026')
        sessions = client.get('/api/auth/sessions', headers=qa).json()
        assert len(sessions) >= 2 and any(x['current'] for x in sessions)
        profile = client.patch('/api/auth/profile', headers=qa, json={'full_name': 'QA Secure Planner', 'phone': '+20 100 555 0101'})
        assert profile.status_code == 200 and profile.json()['full_name'] == 'QA Secure Planner'
        changed = client.post('/api/auth/change-password', headers=qa, json={'current_password': 'SecurePass@2026', 'new_password': 'SecurePass@2027!'})
        assert changed.status_code == 200, changed.text
        assert client.get('/api/auth/me', headers=qa2).status_code == 401
        assert auth(client, 'qa_secure', 'SecurePass@2027!')
        deactivated = client.patch(f'/api/admin/users/{qa_id}/status', headers=admin, json={'active': False})
        assert deactivated.status_code == 200
        assert client.post('/api/auth/login', json={'username': 'qa_secure', 'password': 'SecurePass@2027!'}).status_code == 401
        assert client.patch(f'/api/admin/users/{qa_id}/status', headers=admin, json={'active': True}).status_code == 200

        # HSE lifecycle can be investigated and closed with updated risk data.
        hse_record = client.post('/api/hse', headers=admin, json={'incident_type': 'Near Miss', 'title': 'QA HSE lifecycle', 'severity': 2, 'probability': 2, 'description': 'QA record'})
        hid = hse_record.json()['id']
        hse_closed = client.patch(f'/api/hse/{hid}', headers=admin, json={'status': 'Closed', 'severity': 1, 'probability': 1, 'corrective_action': 'Barrier installed'})
        assert hse_closed.status_code == 200, hse_closed.text
        assert hse_closed.json()['status'] == 'Closed' and hse_closed.json()['risk_score'] == 1

        # Project tasks are operational and recalculate project progress.
        project = client.get('/api/projects', headers=admin).json()[0]
        task = client.post(f"/api/projects/{project['id']}/tasks", headers=admin, json={'task_name': 'QA relay coordination review', 'status': 'Open', 'progress': 10})
        assert task.status_code == 200, task.text
        task_id = task.json()['id']
        task_done = client.patch(f"/api/projects/{project['id']}/tasks/{task_id}", headers=admin, json={'status': 'Completed'})
        assert task_done.status_code == 200 and task_done.json()['progress'] == 100
        refreshed_project = next(x for x in client.get('/api/projects', headers=admin).json() if x['id'] == project['id'])
        assert 0 <= refreshed_project['progress'] <= 100

        # Upload allow-list rejects executable content before persistence.
        rejected_upload = client.post('/api/documents/upload', headers=admin, data={'title': 'Blocked file', 'category': 'Report'}, files={'file': ('payload.exe', b'MZ', 'application/octet-stream')})
        assert rejected_upload.status_code == 400

        # Login throttling triggers after repeated failures for one principal.
        for _ in range(5):
            client.post('/api/auth/login', json={'username': 'blocked-user', 'password': 'WrongPassword!'})
        throttled = client.post('/api/auth/login', json={'username': 'blocked-user', 'password': 'WrongPassword!'})
        assert throttled.status_code == 429

        # All read modules return successfully.
        for path in ['/api/dashboard', '/api/operations', '/api/map', '/api/projects', '/api/documents', '/api/analytics', '/api/vendors', '/api/contracts']:
            assert client.get(path, headers=admin).status_code == 200
