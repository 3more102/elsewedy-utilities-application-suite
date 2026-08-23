from fastapi.testclient import TestClient

from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_asset_mutation_service_preserves_api_and_emits_domain_event():
    with TestClient(app) as client:
        admin = auth(client)
        created = client.post(
            '/api/assets',
            headers=admin,
            json={
                'asset_no': 'MOD-ASSET-01',
                'name': 'Modular asset boundary',
                'category': 'QA',
                'criticality': 'Low',
                'condition': 'Good',
                'status': 'Operating',
            },
        )
        assert created.status_code == 200, created.text
        asset_id = created.json()['id']

        updated = client.patch(f'/api/assets/{asset_id}', headers=admin, json={'condition': 'Fair'})
        assert updated.status_code == 200 and updated.json()['ok'] is True

        outbox = client.get('/api/events/outbox', headers=admin).json()
        domain_event = next(x for x in outbox if x['event_type'] == 'asset.created' and x['aggregate_id'] == 'MOD-ASSET-01')
        assert domain_event['status'] == 'Pending'

        deleted = client.delete(f'/api/assets/{asset_id}', headers=admin)
        assert deleted.status_code == 200 and deleted.json()['ok'] is True
