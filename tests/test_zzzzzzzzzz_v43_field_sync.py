from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}, r.json()


def create_assigned_work(client, admin, title, checklist='Verify isolation;Inspect condition'):
    ref = client.get('/api/reference', headers=admin).json()
    tech = next(u for u in ref['users'] if u['username'] == 'tech1')
    supervisor = next(u for u in ref['users'] if u['username'] == 'supervisor')
    asset = next(a for a in client.get('/api/assets', headers=admin).json() if a['asset_no'] == 'TR-001')
    r = client.post('/api/work-orders', headers=admin, json={
        'title': title,
        'description': 'v4.3 offline synchronization regression work',
        'asset_id': asset['id'],
        'priority': 'High',
        'work_type': 'Corrective Maintenance',
        'assigned_to': tech['id'],
        'supervisor_id': supervisor['id'],
        'safety_requirements': 'LOTO and PPE required',
        'instructions': 'Follow approved field procedure.',
        'checklist': checklist,
    })
    assert r.status_code == 200, r.text
    wo_id = r.json()['id']
    for action in ('submit', 'approve', 'assign'):
        x = client.post(f'/api/work-orders/{wo_id}/transition', headers=admin, json={'action': action})
        assert x.status_code == 200, x.text
    return wo_id, asset['id']


def test_v43_offline_field_sync_idempotency_and_safe_batch_rebase():
    with TestClient(app) as client:
        admin, _ = auth(client)
        tech, login = auth(client, 'tech1', 'Tech@2026')
        assert login['expires_at']
        wo_id, _ = create_assigned_work(client, admin, 'V4.3 Offline Batch Rebase')

        client_id = 'v43-tech-device-batch'
        boot = client.get(f'/api/field/sync/bootstrap?client_id={client_id}&device_name=Android-Test', headers=tech)
        assert boot.status_code == 200, boot.text
        assert boot.json()['schema_version'] >= 13
        w = next(x for x in boot.json()['work_orders'] if x['id'] == wo_id)
        assert w['status'] == 'Assigned'
        assert len(w['sync_hash']) == 64
        assert len(w['tasks']) == 2 and all(len(t['sync_hash']) == 64 for t in w['tasks'])

        # Both operations intentionally carry the same original offline base hash.
        # The server may safely rebase the second one only because the first mutation
        # in this same ordered batch already proved that original base was current.
        ops = [
            {
                'operation_id': 'v43-op-batch-start-0001', 'entity_type': 'work_order',
                'entity_id': wo_id, 'operation_type': 'transition', 'base_hash': w['sync_hash'],
                'payload': {'action': 'start'}, 'client_created_at': '2026-08-21T18:10:00',
            },
            {
                'operation_id': 'v43-op-batch-pause-0002', 'entity_type': 'work_order',
                'entity_id': wo_id, 'operation_type': 'transition', 'base_hash': w['sync_hash'],
                'payload': {'action': 'pause'}, 'client_created_at': '2026-08-21T18:11:00',
            },
            {
                'operation_id': 'v43-op-task-00000003', 'entity_type': 'work_order_task',
                'entity_id': w['tasks'][0]['id'], 'operation_type': 'set_status',
                'base_hash': w['tasks'][0]['sync_hash'], 'payload': {'status': 'Completed'},
            },
            {
                'operation_id': 'v43-op-note-00000004', 'entity_type': 'work_order',
                'entity_id': wo_id, 'operation_type': 'append_note', 'base_hash': '',
                'payload': {'note': 'Captured offline during weak connectivity.'},
            },
        ]
        pushed = client.post('/api/field/sync/push', headers=tech, json={
            'client_id': client_id, 'device_name': 'Android-Test', 'operations': ops,
        })
        assert pushed.status_code == 200, pushed.text
        data = pushed.json()
        assert data['counts'] == {'Applied': 4, 'Conflict': 0, 'Rejected': 0}
        assert data['results'][1]['result']['rebased_in_batch'] is True
        assert client.get(f'/api/work-orders/{wo_id}', headers=tech).json()['status'] == 'Assigned'

        # Retry of the identical operation IDs is a replay, never a second mutation.
        replay = client.post('/api/field/sync/push', headers=tech, json={
            'client_id': client_id, 'device_name': 'Android-Test', 'operations': ops,
        })
        assert replay.status_code == 200, replay.text
        assert all(x.get('idempotent_replay') is True for x in replay.json()['results'])
        detail = client.get(f'/api/work-orders/{wo_id}', headers=tech).json()
        assert detail['comments'].count('Captured offline during weak connectivity.') == 1
        assert detail['tasks'][0]['status'] == 'Completed'

        logs = client.get(f'/api/field/sync/operations?client_id={client_id}', headers=tech)
        assert logs.status_code == 200 and len(logs.json()) == 4
        assert {x['status'] for x in logs.json()} == {'Applied'}


def test_v43_field_sync_conflict_detection_resolution_metrics_and_pwa_contract():
    with TestClient(app) as client:
        admin, _ = auth(client)
        tech, _ = auth(client, 'tech1', 'Tech@2026')
        wo_id, asset_id = create_assigned_work(client, admin, 'V4.3 Conflict Resolution')
        client_id = 'v43-tech-device-conflict'
        boot = client.get(f'/api/field/sync/bootstrap?client_id={client_id}&device_name=Rugged-Tablet', headers=tech).json()
        w = next(x for x in boot['work_orders'] if x['id'] == wo_id)

        # Server changes after the technician's snapshot: stale transition must not overwrite it.
        online = client.post(f'/api/work-orders/{wo_id}/transition', headers=admin, json={'action': 'start'})
        assert online.status_code == 200
        stale = client.post('/api/field/sync/push', headers=tech, json={
            'client_id': client_id, 'operations': [{
                'operation_id': 'v43-conflict-work-0001', 'entity_type': 'work_order',
                'entity_id': wo_id, 'operation_type': 'transition', 'base_hash': w['sync_hash'],
                'payload': {'action': 'start'},
            }],
        })
        assert stale.status_code == 200, stale.text
        conflict = stale.json()['results'][0]
        assert conflict['status'] == 'Conflict'
        assert conflict['conflict']['server_state']['status'] == 'In Progress'
        assert len(conflict['conflict']['server_hash']) == 64
        discard = client.post('/api/field/sync/conflicts/v43-conflict-work-0001/resolve', headers=tech, json={'resolution': 'discard'})
        assert discard.status_code == 200 and discard.json()['status'] == 'Discarded'

        # A second stale snapshot demonstrates explicit "retry mine" resolution.
        fresh_boot = client.get(f'/api/field/sync/bootstrap?client_id={client_id}', headers=tech).json()
        wf = next(x for x in fresh_boot['work_orders'] if x['id'] == wo_id)
        old_asset_hash = wf['asset_sync_hash']
        server_condition = 'Critical' if wf['asset_state']['condition'] != 'Critical' else 'Good'
        server_meter = float(wf['asset_state']['meter_reading'] or 0) + 123.456
        server_change = client.patch(f'/api/assets/{asset_id}', headers=admin, json={'condition': server_condition, 'meter_reading': server_meter})
        assert server_change.status_code == 200
        stale_asset = client.post('/api/field/sync/push', headers=tech, json={
            'client_id': client_id, 'operations': [{
                'operation_id': 'v43-conflict-asset-002', 'entity_type': 'asset',
                'entity_id': asset_id, 'operation_type': 'update', 'base_hash': old_asset_hash,
                'payload': {'condition': 'Poor', 'meter_reading': 4321.5},
            }],
        }).json()['results'][0]
        assert stale_asset['status'] == 'Conflict'
        server_hash = stale_asset['conflict']['server_hash']
        retry = client.post('/api/field/sync/conflicts/v43-conflict-asset-002/resolve', headers=tech, json={
            'resolution': 'retry', 'expected_server_hash': server_hash,
        })
        assert retry.status_code == 200, retry.text
        assert retry.json()['status'] == 'Applied'
        asset = client.get(f'/api/assets/{asset_id}', headers=tech).json()
        assert asset['condition'] == 'Poor' and float(asset['meter_reading']) == 4321.5

        # Governance/observability surfaces.
        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_field_sync_conflicts' in metrics
        assert 'euas_field_sync_applied_24h' in metrics
        export = client.get('/api/exports/field-sync.csv', headers=admin)
        assert export.status_code == 200 and 'v43-conflict-asset-002' in export.text

        js = (Path(__file__).resolve().parents[1] / 'static' / 'app.js').read_text(encoding='utf-8')
        sw = (Path(__file__).resolve().parents[1] / 'static' / 'sw.js').read_text(encoding='utf-8')
        assert 'fieldQueueOperation' in js and 'fieldSyncBootstrap' in js
        assert 'Saved offline — will synchronize when connected' in js
        assert 'euas_session_expires_at' in js
        assert "euas-shell-v4." in sw
