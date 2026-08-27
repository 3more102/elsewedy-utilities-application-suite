"""Automation-engine integration for the KPI refresh step.

Lives in the late-ordering (test_z*) group because a full automation run
consumes one-shot seeded state (reorder scan, due PM generation) that earlier
alphabetical end-to-end tests depend on.
"""
from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _collect_all_kpis(client, admin):
    """Collect KPIs from all family endpoints."""
    families = client.get('/api/kpis', headers=admin).json()['families']
    all_kpis = []
    for fam in families:
        resp = client.get(f'/api/kpis/{fam}', headers=admin)
        if resp.status_code == 200:
            payload = resp.json()
            kpis_dict = payload.get('kpis', {})
            if isinstance(kpis_dict, dict):
                all_kpis.extend(kpis_dict.values())
            elif isinstance(kpis_dict, list):
                all_kpis.extend(kpis_dict)
    return all_kpis


def test_automation_refresh_bootstraps_then_skips_fresh_kpis():
    with TestClient(app) as client:
        admin = auth(client)

        index = client.get('/api/kpis', headers=admin)
        assert index.status_code == 200, index.text
        families = index.json()['families']
        assert len(families) >= 4

        active = _collect_all_kpis(client, admin)
        assert len(active) >= 12

        first = client.post('/api/automation/run', headers=admin)
        assert first.status_code == 200, first.text
        summary = first.json()['summary']
        assert 'kpi_refreshed' in summary
        assert 'kpi_unsupported_source' in summary

        second = client.post('/api/automation/run', headers=admin)
        assert second.status_code == 200, second.text
        assert second.json()['summary']['kpi_refreshed'] == 0

        kpi_result = client.get('/api/kpis/reliability', headers=admin)
        assert kpi_result.status_code == 200
        kpis = kpi_result.json().get('kpis', {})
        assert len(kpis) >= 4
        for kpi_id, kpi in kpis.items():
            assert 'name' in kpi, f"KPI {kpi_id} missing name"
            assert 'definition' in kpi, f"KPI {kpi_id} missing definition"
