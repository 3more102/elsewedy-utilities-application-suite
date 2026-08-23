from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _open_test_alarm(client, headers):
    assets = client.get('/api/assets', headers=headers).json()
    gen = next(a for a in assets if a['asset_no'] == 'GEN-201')
    code = 'TEL-V41-SHELF-GEN-TEMP'
    created = client.post('/api/telemetry/channels', headers=headers, json={
        'channel_code': code, 'asset_id': gen['id'], 'name': 'Generator Bearing Temperature',
        'metric_type': 'Temperature', 'unit': 'C', 'source_system': 'SCADA-V41',
        'warning_high': 70, 'critical_high': 90,
    })
    assert created.status_code == 200, created.text
    ingest = client.post('/api/telemetry/ingest', headers=headers, json={
        'readings': [{'channel_code': code, 'value': 78, 'quality': 'Good', 'external_id': 'v41-shelf-001'}],
    })
    assert ingest.status_code == 200 and ingest.json()['alarms_opened'] == 1
    alarm_no = ingest.json()['results'][0]['alarm_no']
    alarms = client.get('/api/alarms', headers=headers).json()
    return next(a for a in alarms if a['alarm_no'] == alarm_no)


def test_v41_alarm_shelf_requires_four_eyes_approval_and_expires():
    with TestClient(app) as client:
        admin = auth(client, 'omar')
        manager = auth(client, 'seif')
        alarm = _open_test_alarm(client, admin)

        requested = client.post(f"/api/alarms/{alarm['id']}/shelf", headers=admin, json={
            'reason': 'Known transient during controlled diagnostics', 'duration_minutes': 60,
        })
        assert requested.status_code == 200, requested.text
        shelf = requested.json()
        assert shelf['status'] == 'Pending' and shelf['approval_required'] is True

        # Pending requests do not hide an alarm from the actionable queue.
        cc = client.get('/api/operations/command-center', headers=admin).json()
        assert any(a['id'] == alarm['id'] for a in cc['actionable_alarms'])

        approvals = client.get('/api/approvals', headers=manager).json()
        approval = next(a for a in approvals if a['record_type'] == 'alarm_shelf' and a['record_id'] == shelf['id'])

        # The requester cannot approve their own shelf, even when they are an administrator.
        self_approval = client.post(f"/api/approvals/{approval['id']}/decision", headers=admin, json={'decision': 'approve', 'comments': 'self'})
        assert self_approval.status_code == 403

        approved = client.post(f"/api/approvals/{approval['id']}/decision", headers=manager, json={'decision': 'approve', 'comments': 'Reviewed in control room', 'current_password': 'EUAS@2026', 'signer_intent': f"I approve {approval['record_code']}"})
        assert approved.status_code == 200, approved.text

        active = client.get('/api/alarm-shelves', headers=admin, params={'active_only': True}).json()
        row = next(x for x in active if x['id'] == shelf['id'])
        assert row['status'] == 'Approved' and row['approved_by_name'] == 'Seif'

        alarm_after = next(a for a in client.get('/api/alarms', headers=admin).json() if a['id'] == alarm['id'])
        assert alarm_after['shelved'] is True and alarm_after['shelf_no'] == shelf['shelf_no']
        cc = client.get('/api/operations/command-center', headers=admin).json()
        assert not any(a['id'] == alarm['id'] for a in cc['actionable_alarms'])
        assert any(x['alarm_id'] == alarm['id'] for x in cc['shelves'])
        assert cc['summary']['active_alarm_shelves'] >= 1

        # Force expiry and verify automation restores actionability without changing alarm lifecycle.
        from app.database import db
        with db() as conn:
            conn.execute("UPDATE alarm_shelves SET end_at=? WHERE id=?", ((datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds'), shelf['id']))
        run = client.post('/api/automation/run', headers=manager)
        assert run.status_code == 200, run.text
        assert run.json()['summary']['alarm_shelves_expired'] >= 1
        expired = next(x for x in client.get('/api/alarm-shelves', headers=admin).json() if x['id'] == shelf['id'])
        assert expired['status'] == 'Expired'
        alarm_restored = next(a for a in client.get('/api/alarms', headers=admin).json() if a['id'] == alarm['id'])
        assert alarm_restored['shelved'] is False and alarm_restored['status'] in ('Open', 'Acknowledged')

        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_active_alarm_shelves' in metrics
        assert client.get('/api/exports/alarm-shelves.csv', headers=admin).status_code == 200


def test_v41_critical_shelf_duration_policy_and_revoke():
    with TestClient(app) as client:
        admin = auth(client, 'omar')
        manager = auth(client, 'seif')
        assets = client.get('/api/assets', headers=admin).json()
        gen = next(a for a in assets if a['asset_no'] == 'GEN-201')
        code = 'TEL-V41-CRIT-GEN-VIB'
        assert client.post('/api/telemetry/channels', headers=admin, json={
            'channel_code': code, 'asset_id': gen['id'], 'name': 'Generator Critical Vibration',
            'metric_type': 'Vibration', 'unit': 'mm/s', 'source_system': 'SCADA-V41', 'critical_high': 10,
        }).status_code == 200
        body = client.post('/api/telemetry/ingest', headers=admin, json={'readings': [{'channel_code': code, 'value': 15, 'external_id': 'v41-crit-1'}]}).json()
        alarm_no = body['results'][0]['alarm_no']
        alarm = next(a for a in client.get('/api/alarms', headers=admin).json() if a['alarm_no'] == alarm_no)

        too_long = client.post(f"/api/alarms/{alarm['id']}/shelf", headers=admin, json={
            'reason': 'Critical diagnostic transient under investigation', 'duration_minutes': 180,
        })
        assert too_long.status_code == 422

        req = client.post(f"/api/alarms/{alarm['id']}/shelf", headers=admin, json={
            'reason': 'Critical diagnostic transient under investigation', 'duration_minutes': 30,
        }).json()
        approval = next(a for a in client.get('/api/approvals', headers=manager).json() if a['record_type'] == 'alarm_shelf' and a['record_id'] == req['id'])
        assert client.post(f"/api/approvals/{approval['id']}/decision", headers=manager, json={'decision': 'approve', 'current_password': 'EUAS@2026', 'signer_intent': f"I approve {approval['record_code']}"}).status_code == 200
        revoked = client.post(f"/api/alarm-shelves/{req['id']}/revoke", headers=manager)
        assert revoked.status_code == 200 and revoked.json()['status'] == 'Revoked'
        restored = next(a for a in client.get('/api/alarms', headers=admin).json() if a['id'] == alarm['id'])
        assert restored['shelved'] is False
