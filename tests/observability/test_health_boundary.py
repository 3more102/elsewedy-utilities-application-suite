from fastapi.testclient import TestClient

from app.main import app


def test_health_and_readiness_remain_operational():
    with TestClient(app) as client:
        health = client.get('/api/health')
        ready = client.get('/api/health/ready')

    assert health.status_code == 200
    assert health.json()['status'] == 'ok'
    assert ready.status_code == 200
    assert ready.json()['status'] == 'ready'
    assert ready.json()['checks']['users'] >= 1
