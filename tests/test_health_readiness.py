from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import SCHEMA_VERSION
from app.database import db, now
from app.main import app


def test_readiness_reports_ready_with_applied_schema():
    with TestClient(app) as client:
        response = client.get('/api/health/ready')
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['status'] == 'ready'
        assert payload['applied_schema_version'] >= SCHEMA_VERSION
        assert payload['checks']['users'] >= 1
        assert payload['checks']['assets'] >= 1


def test_readiness_fails_closed_when_schema_migrations_lag():
    with TestClient(app) as client:
        healthy = client.get('/api/health/ready')
        assert healthy.status_code == 200

        with db() as conn:
            versions = [int(r[0]) for r in conn.execute(
                'SELECT version FROM schema_migrations'
            ).fetchall()]
            conn.execute('DELETE FROM schema_migrations')

        try:
            degraded = client.get('/api/health/ready')
            assert degraded.status_code == 503, degraded.text
            payload = degraded.json()
            assert payload['status'] == 'degraded'
            assert payload['schema_version'] == SCHEMA_VERSION
            assert payload['applied_schema_version'] == 0
        finally:
            with db() as conn:
                for version in versions:
                    conn.execute(
                        'INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',
                        (version, now()),
                    )

        restored = client.get('/api/health/ready')
        assert restored.status_code == 200
        assert restored.json()['status'] == 'ready'
