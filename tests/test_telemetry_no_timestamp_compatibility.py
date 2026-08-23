from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_rapid_implicit_capture_times_remain_live_generations():
    with TestClient(app) as client:
        headers = _auth(client)
        asset = client.get('/api/assets', headers=headers).json()[0]
        code = f'TEL-IMPLICIT-{uuid.uuid4().hex[:10]}'
        created = client.post(
            '/api/telemetry/channels',
            headers=headers,
            json={
                'channel_code': code,
                'asset_id': asset['id'],
                'name': 'Implicit capture compatibility',
                'metric_type': 'Current',
                'unit': 'A',
                'source_system': 'Compatibility test',
                'warning_high': 50,
                'critical_high': 75,
            },
        )
        assert created.status_code == 200, created.text

        normal = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 40, 'quality': 'Good'}]},
        )
        warning = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 60, 'quality': 'Good'}]},
        )
        critical = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 80, 'quality': 'Good'}]},
        )

        assert normal.status_code == 200 and normal.json()['normal'] == 1
        assert normal.json()['historical'] == 0
        assert warning.status_code == 200 and warning.json()['alarms_opened'] == 1
        assert warning.json()['historical'] == 0
        assert critical.status_code == 200 and critical.json()['alarms_updated'] == 1
        assert critical.json()['historical'] == 0
