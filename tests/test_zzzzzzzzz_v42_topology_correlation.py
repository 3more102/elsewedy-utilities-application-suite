from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _make_asset(client, headers, asset_no, name, location_id):
    r = client.post('/api/assets', headers=headers, json={
        'asset_no': asset_no, 'name': name, 'category': 'Electrical Test Asset',
        'criticality': 'High', 'condition': 'Good', 'status': 'Operating',
        'location_id': location_id, 'maintenance_strategy': 'Condition Based',
    })
    assert r.status_code == 200, r.text
    return r.json()['id']


def _make_channel(client, headers, code, asset_id, name):
    r = client.post('/api/telemetry/channels', headers=headers, json={
        'channel_code': code, 'asset_id': asset_id, 'name': name,
        'metric_type': 'Temperature', 'unit': 'C', 'source_system': 'SCADA-V42',
        'warning_high': 70, 'critical_high': 90,
    })
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_v42_topology_correlates_connected_assets_and_targets_root_candidate():
    with TestClient(app) as client:
        admin = auth(client)
        assets = client.get('/api/assets', headers=admin).json()
        tr = next(a for a in assets if a['asset_no'] == 'TR-001')
        upstream = _make_asset(client, admin, 'V42-UP-001', 'V4.2 Upstream Test Asset', tr['location_id'])
        downstream = _make_asset(client, admin, 'V42-DN-001', 'V4.2 Downstream Test Asset', tr['location_id'])

        link = client.post('/api/asset-topology', headers=admin, json={
            'upstream_asset_id': upstream, 'downstream_asset_id': downstream,
            'relation_type': 'Feeds', 'notes': 'V4.2 deterministic topology regression link',
        })
        assert link.status_code == 200, link.text
        assert link.json()['link_no'].startswith('TPL-')

        _make_channel(client, admin, 'TEL-V42-UP-TEMP', upstream, 'Upstream Winding Temperature')
        _make_channel(client, admin, 'TEL-V42-DN-TEMP', downstream, 'Downstream Cubicle Temperature')

        first = client.post('/api/telemetry/ingest', headers=admin, json={
            'readings': [{'channel_code': 'TEL-V42-UP-TEMP', 'value': 96, 'external_id': 'v42-up-1'}],
        })
        second = client.post('/api/telemetry/ingest', headers=admin, json={
            'readings': [{'channel_code': 'TEL-V42-DN-TEMP', 'value': 82, 'external_id': 'v42-dn-1'}],
        })
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()['alarms_opened'] == 1 and second.json()['alarms_opened'] == 1
        incident_no = first.json()['results'][0]['incident_no']
        assert second.json()['results'][0]['incident_no'] == incident_no

        incident = next(x for x in client.get('/api/alarm-incidents', headers=admin).json() if x['incident_no'] == incident_no)
        detail = client.get(f"/api/alarm-incidents/{incident['id']}", headers=admin).json()
        assert detail['correlation_mode'] == 'Topology'
        assert detail['root_cause_asset_no'] == 'V42-UP-001'
        assert detail['root_cause_score'] >= 90
        assert detail['topology_hops'] == 1
        assert 'upstream of 1 of 1 other alarmed asset' in detail['root_cause_reason']
        assert {a['asset_no'] for a in detail['alarms']} >= {'V42-UP-001', 'V42-DN-001'}

        work = client.post(f"/api/alarm-incidents/{incident['id']}/work-order", headers=admin, json={})
        assert work.status_code == 200, work.text
        wo = client.get(f"/api/work-orders/{work.json()['id']}", headers=admin).json()
        assert wo['asset_no'] == 'V42-UP-001'
        assert 'Deterministic root-cause candidate: V42-UP-001' in wo['description']


def test_v42_topology_governance_metrics_exports_and_command_center_ui_regression():
    with TestClient(app) as client:
        admin = auth(client)
        links = client.get('/api/asset-topology', headers=admin).json()
        assert len(links) >= 3
        link = next(x for x in links if x['upstream_asset_no'] == 'V42-UP-001' and x['downstream_asset_no'] == 'V42-DN-001')

        # Reversing an active directed dependency would form a cycle and must be rejected.
        cycle = client.post('/api/asset-topology', headers=admin, json={
            'upstream_asset_id': link['downstream_asset_id'], 'downstream_asset_id': link['upstream_asset_id'],
            'relation_type': 'Feeds', 'notes': 'Must be rejected as cycle',
        })
        assert cycle.status_code == 409

        cc = client.get('/api/operations/command-center', headers=admin)
        assert cc.status_code == 200, cc.text
        data = cc.json()
        assert data['summary']['active_topology_links'] >= 3
        assert data['summary']['topology_correlated_incidents'] >= 1
        assert any(x['link_no'] == link['link_no'] for x in data['topology_links'])
        assert 'actionable_alarms' in data and 'shelves' in data

        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_active_topology_links' in metrics
        assert 'euas_topology_correlated_incidents' in metrics
        assert client.get('/api/exports/asset-topology.csv', headers=admin).status_code == 200
        export = client.get('/api/exports/alarm-incidents.csv', headers=admin)
        assert export.status_code == 200 and 'Root Cause Candidate' in export.text and 'Correlation' in export.text

        # v4.1 had a browser runtime bug: renderCommandCenter referenced these without binding them.
        js = (Path(__file__).resolve().parents[1] / 'static' / 'app.js').read_text(encoding='utf-8')
        assert "actionable=d.actionable_alarms||[]" in js
        assert "shelves=d.shelves||[]" in js
        assert "topology=d.topology_links||[]" in js
