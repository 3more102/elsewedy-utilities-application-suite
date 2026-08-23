"""Automation-engine integration for the KPI refresh step.

Lives in the late-ordering (test_z*) group because a full automation run
consumes one-shot seeded state (reorder scan, due PM generation) that earlier
alphabetical end-to-end tests depend on.
"""
from fastapi.testclient import TestClient

from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_automation_refresh_bootstraps_then_skips_fresh_kpis():
    with TestClient(app) as client:
        admin = auth(client)
        active = client.get('/api/kpis', headers=admin).json()['kpis']
        assert len(active) >= 12

        first = client.post('/api/automation/run', headers=admin)
        assert first.status_code == 200, first.text
        summary = first.json()['summary']
        assert 'kpi_refreshed' in summary and 'kpi_unsupported_source' in summary
        # After a run every active KPI has at least one persisted snapshot.
        for k in active:
            hist = client.get(f"/api/kpis/{k['id']}/history", headers=admin).json()['history']
            assert hist, f"{k['code']} has no snapshot after automation run"

        # An immediate second run must find every snapshot fresh and refresh nothing.
        second = client.post('/api/automation/run', headers=admin)
        assert second.status_code == 200, second.text
        assert second.json()['summary']['kpi_refreshed'] == 0

        # Snapshots are readable through history with provenance preserved.
        overdue = next(k for k in active if k['code'] == 'KPI-OVERDUE-WO')
        snap = client.get(f"/api/kpis/{overdue['id']}/history", headers=admin).json()['history'][0]
        assert snap['provenance_json']
        assert snap['data_freshness_at'] or snap['calculated_at']
