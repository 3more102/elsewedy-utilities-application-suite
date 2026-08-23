from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

TEST_DB = Path(__file__).resolve().parents[1] / 'euas_test.db'


def auth(client, username='omar', password='EUAS@2026'):
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def refs(client, admin):
    data = client.get('/api/reference', headers=admin).json()
    return {u['username']: u['id'] for u in data['users']}


def make_work(client, admin, supervisor_id, title):
    asset = next(x for x in client.get('/api/assets', headers=admin).json() if x['asset_no'] == 'TR-001')
    response = client.post('/api/work-orders', headers=admin, json={
        'title': title, 'asset_id': asset['id'], 'priority': 'High',
        'supervisor_id': supervisor_id, 'estimated_hours': 1,
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


def submit_and_approval(client, admin, work_id):
    submitted = client.post(f'/api/work-orders/{work_id}/transition', headers=admin, json={'action': 'submit'})
    assert submitted.status_code == 200, submitted.text
    queue = client.get('/api/approvals', headers=admin, params={'status': 'Pending'}).json()
    return next(x for x in queue if x['record_type'] == 'work_order' and x['record_id'] == work_id)


def test_delegation_resource_scope_and_future_window_are_enforced():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        users = refs(client, admin)
        first_id = make_work(client, admin, users['supervisor'], 'Scoped delegation allowed')
        second_id = make_work(client, admin, users['supervisor'], 'Scoped delegation denied')
        start = (datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')
        created = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['planner'], 'module': 'Work Management', 'record_type': 'work_order',
            'resource_id': first_id, 'start_at': start, 'end_at': end, 'reason': 'Cover one work order only',
        })
        assert created.status_code == 200, created.text

        first = submit_and_approval(client, admin, first_id)
        signed = client.post(f"/api/approvals/{first['id']}/decision", headers=planner, json={
            'decision': 'approve', 'current_password': 'Planner@2026',
            'signer_intent': f"I approve {first['record_code']}", 'comments': 'Scoped delegated approval',
        })
        assert signed.status_code == 200, signed.text
        assert signed.json()['decision_evidence']['evidence_no'].startswith('APE-')

        second = submit_and_approval(client, admin, second_id)
        denied = client.post(f"/api/approvals/{second['id']}/decision", headers=planner, json={
            'decision': 'approve', 'current_password': 'Planner@2026',
            'signer_intent': f"I approve {second['record_code']}",
        })
        assert denied.status_code == 403

        future_start = (datetime.now() + timedelta(days=2)).isoformat(timespec='seconds')
        future_end = (datetime.now() + timedelta(days=3)).isoformat(timespec='seconds')
        future = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['planner'], 'module': 'Work Management', 'record_type': 'work_order',
            'resource_id': second_id, 'start_at': future_start, 'end_at': future_end,
        })
        assert future.status_code == 200, future.text
        denied_future = client.post(f"/api/approvals/{second['id']}/decision", headers=planner, json={
            'decision': 'approve', 'current_password': 'Planner@2026',
            'signer_intent': f"I approve {second['record_code']}",
        })
        assert denied_future.status_code == 403
        assert client.patch(f"/api/approval-delegations/{created.json()['id']}/deactivate", headers=supervisor).status_code == 200
        assert client.patch(f"/api/approval-delegations/{future.json()['id']}/deactivate", headers=supervisor).status_code == 200


def test_delegation_cannot_grant_missing_or_revoked_authority():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        users = refs(client, admin)
        start = (datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')

        missing = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['tech1'], 'module': 'Work Management', 'start_at': start, 'end_at': end,
        })
        assert missing.status_code == 409
        assert 'Delegate lacks approval authority' in missing.text

        created = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['planner'], 'module': 'Work Management', 'start_at': start, 'end_at': end,
            'reason': 'Temporary coverage',
        })
        assert created.status_code == 200, created.text
        work_id = make_work(client, admin, users['supervisor'], 'Delegator authority revoked')
        approval = submit_and_approval(client, admin, work_id)

        with sqlite3.connect(TEST_DB) as conn:
            permission_id = conn.execute("SELECT id FROM permissions WHERE code='approvals.decide'").fetchone()[0]
            conn.execute(
                '''INSERT OR REPLACE INTO user_permission_overrides(user_id,permission_id,effect,reason,expires_at,updated_by,updated_at)
                   VALUES(?,?,?,'security regression',NULL,?,?)''',
                (users['supervisor'], permission_id, 'Deny', users['omar'], datetime.now().isoformat(timespec='seconds')),
            )
            conn.commit()
        try:
            denied = client.post(f"/api/approvals/{approval['id']}/decision", headers=planner, json={
                'decision': 'approve', 'current_password': 'Planner@2026',
                'signer_intent': f"I approve {approval['record_code']}",
            })
            assert denied.status_code == 403
        finally:
            with sqlite3.connect(TEST_DB) as conn:
                conn.execute("DELETE FROM user_permission_overrides WHERE user_id=? AND permission_id=?", (users['supervisor'], permission_id))
                conn.commit()
            client.patch(f"/api/approval-delegations/{created.json()['id']}/deactivate", headers=supervisor)


def test_stale_target_snapshot_blocks_decision_without_mutating_workflow():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        users = refs(client, admin)
        work_id = make_work(client, admin, users['supervisor'], 'Snapshot binding regression')
        approval = submit_and_approval(client, admin, work_id)
        assert len(approval['request_snapshot_hash']) == 64

        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE work_orders SET title=? WHERE id=?', ('Materially changed after approval request', work_id))
            conn.commit()

        rejected = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor, json={
            'decision': 'approve', 'current_password': 'Supervisor@2026',
            'signer_intent': f"I approve {approval['record_code']}",
        })
        assert rejected.status_code == 409
        assert 'changed after the request' in rejected.text
        assert client.get(f'/api/work-orders/{work_id}', headers=admin).json()['status'] == 'Submitted'
        pending = next(x for x in client.get('/api/approvals', headers=admin, params={'status': 'Pending'}).json() if x['id'] == approval['id'])
        assert pending['status'] == 'Pending'


def test_approval_evidence_chain_verifies_history_and_detects_tampering():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        users = refs(client, admin)
        work_id = make_work(client, admin, users['supervisor'], 'Lifecycle evidence regression')
        approval = submit_and_approval(client, admin, work_id)
        signed = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor, json={
            'decision': 'approve', 'current_password': 'Supervisor@2026',
            'signer_intent': f"I approve {approval['record_code']}", 'comments': 'Evidence chain regression',
        })
        assert signed.status_code == 200, signed.text

        history = client.get(f"/api/approvals/{approval['id']}/decision-history", headers=supervisor)
        assert history.status_code == 200, history.text
        assert [item['event_type'] for item in history.json()] == ['ApprovalRequested', 'ApprovalGranted']
        assert history.json()[-1]['payload']['actor_user_id'] == users['supervisor']
        assert history.json()[-1]['payload']['effective_actor_user_id'] == users['supervisor']

        valid = client.get('/api/approval-evidence/verify', headers=admin)
        assert valid.status_code == 200 and valid.json()['valid'] is True

        with sqlite3.connect(TEST_DB) as conn:
            row = conn.execute('SELECT id,decision FROM approval_evidence_events WHERE approval_id=? ORDER BY id DESC LIMIT 1', (approval['id'],)).fetchone()
            conn.execute('UPDATE approval_evidence_events SET decision=? WHERE id=?', ('TAMPERED', row[0]))
            conn.commit()
        broken = client.get('/api/approval-evidence/verify', headers=admin)
        assert broken.status_code == 200 and broken.json()['valid'] is False
        assert broken.json()['reason'] == 'column_payload_mismatch'
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE approval_evidence_events SET decision=? WHERE id=?', (row[1], row[0]))
            conn.commit()
        assert client.get('/api/approval-evidence/verify', headers=admin).json()['valid'] is True


def test_self_duplicate_and_nested_delegation_are_rejected():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        users = refs(client, admin)
        start = (datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')
        self_delegation = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['supervisor'], 'module': '*', 'start_at': start, 'end_at': end,
        })
        assert self_delegation.status_code == 400
        first = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['planner'], 'module': 'Work Management', 'start_at': start, 'end_at': end,
        })
        assert first.status_code == 200, first.text
        duplicate = client.post('/api/approval-delegations', headers=supervisor, json={
            'delegate_user_id': users['planner'], 'module': 'Work Management', 'start_at': start, 'end_at': end,
        })
        assert duplicate.status_code == 409
        nested = client.post('/api/approval-delegations', headers=planner, json={
            'delegate_user_id': users['supervisor'], 'module': 'Work Management', 'start_at': start, 'end_at': end,
        })
        assert nested.status_code == 409
        assert 'Nested approval delegation' in nested.text
        assert client.patch(f"/api/approval-delegations/{first.json()['id']}/deactivate", headers=supervisor).status_code == 200
