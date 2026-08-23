from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app


def auth(client, username, password):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _submit_supervisor_approval(client, admin, supervisor, users, title):
    asset = next(x for x in client.get('/api/assets', headers=admin).json())
    wo = client.post(
        '/api/work-orders',
        headers=admin,
        json={
            'title': title,
            'asset_id': asset['id'],
            'priority': 'Medium',
            'supervisor_id': users['supervisor'],
        },
    )
    assert wo.status_code == 200, wo.text
    wo_id = wo.json()['id']
    assert (
        client.post(
            f'/api/work-orders/{wo_id}/transition', headers=admin, json={'action': 'submit'}
        ).status_code
        == 200
    )
    approval = next(
        x
        for x in client.get('/api/approvals?status=Pending', headers=admin).json()
        if x['record_type'] == 'work_order' and x['record_id'] == wo_id
    )
    return int(approval['id'])


def test_expired_delegation_does_not_confer_decision_authority():
    with TestClient(app) as client:
        admin = auth(client, 'omar', 'EUAS@2026')
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        ref = client.get('/api/reference', headers=admin).json()
        users = {x['username']: x['id'] for x in ref['users']}

        start = (datetime.now() - timedelta(hours=2)).isoformat(timespec='seconds')
        end = (datetime.now() - timedelta(minutes=30)).isoformat(timespec='seconds')
        created = client.post(
            '/api/approval-delegations',
            headers=supervisor,
            json={
                'delegate_user_id': users['planner'],
                'module': '*',
                'start_at': start,
                'end_at': end,
            },
        )
        assert created.status_code == 200, created.text

        approval_id = _submit_supervisor_approval(
            client, admin, supervisor, users, 'Expired delegation regression'
        )
        denied = client.post(
            f"/api/approvals/{approval_id}/decision",
            headers=planner,
            json={'decision': 'approve'},
        )
        assert denied.status_code == 403, denied.text


def test_self_delegation_is_rejected():
    with TestClient(app) as client:
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        ref = client.get('/api/reference', headers=supervisor).json()
        users = {x['username']: x['id'] for x in ref['users']}
        start = datetime.now().isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')
        rejected = client.post(
            '/api/approval-delegations',
            headers=supervisor,
            json={
                'delegate_user_id': users['supervisor'],
                'module': '*',
                'start_at': start,
                'end_at': end,
            },
        )
        assert rejected.status_code == 400, rejected.text


def test_delegate_cannot_decide_approvals_outside_delegator_scope():
    with TestClient(app) as client:
        admin = auth(client, 'omar', 'EUAS@2026')
        tech = auth(client, 'tech1', 'Tech@2026')
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        planner = auth(client, 'planner', 'Planner@2026')
        ref = client.get('/api/reference', headers=admin).json()
        users = {x['username']: x['id'] for x in ref['users']}

        # The technician holds a valid delegation from the supervisor.
        start = datetime.now().isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')
        created = client.post(
            '/api/approval-delegations',
            headers=supervisor,
            json={
                'delegate_user_id': users['tech1'],
                'module': '*',
                'start_at': start,
                'end_at': end,
            },
        )
        assert created.status_code == 200, created.text

        # A different supervisor-assigned approval is outside the delegator's
        # scope when the delegator is not its assignee: here the approval is
        # assigned to the planner user instead.
        asset = next(x for x in client.get('/api/assets', headers=admin).json())
        wo = client.post(
            '/api/work-orders',
            headers=admin,
            json={
                'title': 'Outside delegation scope regression',
                'asset_id': asset['id'],
                'priority': 'Medium',
                'supervisor_id': users['planner'],
            },
        )
        assert wo.status_code == 200, wo.text
        wo_id = wo.json()['id']
        assert (
            client.post(
                f'/api/work-orders/{wo_id}/transition',
                headers=admin,
                json={'action': 'submit'},
            ).status_code
            == 200
        )
        approval_id = int(
            next(
                x
                for x in client.get('/api/approvals?status=Pending', headers=admin).json()
                if x['record_type'] == 'work_order' and x['record_id'] == wo_id
            )['id']
        )

        # Neither the delegate (tech1) nor anyone else may decide an approval
        # that was never assigned to their delegator.
        denied = client.post(
            f'/api/approvals/{approval_id}/decision', headers=tech, json={'decision': 'approve'}
        )
        assert denied.status_code == 403, denied.text

        # The actual assignee can still decide it: delegation grants no new
        # authority to third parties.
        allowed = client.post(
            f'/api/approvals/{approval_id}/decision',
            headers=planner,
            json={'decision': 'approve'},
        )
        assert allowed.status_code == 200, allowed.text
