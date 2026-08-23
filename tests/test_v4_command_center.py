from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_v4_ingestion_idempotency_quality_and_series():
    with TestClient(app) as client:
        admin = auth(client)
        assets = client.get('/api/assets', headers=admin).json()
        gen = next(a for a in assets if a['asset_no'] == 'GEN-201')
        channel = client.post('/api/telemetry/channels', headers=admin, json={
            'channel_code': 'TEL-V4-GEN-FREQ', 'asset_id': gen['id'], 'name': 'Generator Frequency',
            'metric_type': 'Frequency', 'unit': 'Hz', 'source_system': 'SCADA-GW',
            'warning_low': 49.5, 'critical_low': 48.5, 'warning_high': 50.5, 'critical_high': 51.5,
        })
        assert channel.status_code == 200, channel.text
        channel_id = channel.json()['id']

        first = client.post('/api/telemetry/ingest', headers=admin, json={
            'source_system': 'SCADA-GW', 'idempotency_key': 'batch-v4-001',
            'readings': [
                {'channel_code': 'TEL-V4-GEN-FREQ', 'value': 50.0, 'quality': 'Good', 'external_id': 'freq-001'},
                {'channel_code': 'TEL-V4-GEN-FREQ', 'value': 52.0, 'quality': 'Bad', 'external_id': 'freq-002'},
            ],
        })
        assert first.status_code == 200, first.text
        body = first.json()
        assert body['accepted'] == 2 and body['bad_quality'] == 1
        assert body['alarms_opened'] == 0  # Bad-quality threshold violation must not create an alarm.

        replay = client.post('/api/telemetry/ingest', headers=admin, json={
            'source_system': 'SCADA-GW', 'idempotency_key': 'batch-v4-001',
            'readings': [{'channel_code': 'TEL-V4-GEN-FREQ', 'value': 49.9, 'external_id': 'freq-003'}],
        })
        assert replay.status_code == 200 and replay.json()['idempotent_replay'] is True
        assert replay.json()['batch_no'] == body['batch_no']

        duplicate = client.post('/api/telemetry/ingest', headers=admin, json={
            'source_system': 'SCADA-GW',
            'readings': [{'channel_code': 'TEL-V4-GEN-FREQ', 'value': 50.1, 'quality': 'Good', 'external_id': 'freq-001'}],
        })
        assert duplicate.status_code == 200 and duplicate.json()['duplicates'] == 1 and duplicate.json()['accepted'] == 0

        quality = client.get('/api/telemetry/quality', headers=admin, params={'hours': 24}).json()
        assert quality['total_readings'] >= 2 and quality['bad'] >= 1 and 'good_percent' in quality
        batches = client.get('/api/telemetry/batches', headers=admin).json()
        assert any(x['batch_no'] == body['batch_no'] and x['bad_quality_count'] == 1 for x in batches)
        series = client.get('/api/telemetry/series', headers=admin, params={'channel_id': channel_id, 'hours': 24, 'bucket_minutes': 60})
        assert series.status_code == 200 and series.json()['points']


def test_v4_alarm_suppression_and_correlation_incident_workflow():
    with TestClient(app) as client:
        admin = auth(client)
        assets = client.get('/api/assets', headers=admin).json()
        cb = next(a for a in assets if a['asset_no'] == 'CB-101')

        channel_ids = []
        for code, name in [('TEL-V4-CB-VOLT', 'CB Bus Voltage'), ('TEL-V4-CB-TEMP', 'CB Cubicle Temperature')]:
            created = client.post('/api/telemetry/channels', headers=admin, json={
                'channel_code': code, 'asset_id': cb['id'], 'name': name,
                'metric_type': 'Voltage' if 'VOLT' in code else 'Temperature',
                'unit': 'kV' if 'VOLT' in code else 'C', 'source_system': 'SCADA-V4', 'warning_high': 10, 'critical_high': 20,
            })
            assert created.status_code == 200, created.text
            channel_ids.append(created.json()['id'])

        start = (datetime.now() - timedelta(minutes=5)).isoformat(timespec='seconds')
        end = (datetime.now() + timedelta(hours=1)).isoformat(timespec='seconds')
        suppression = client.post('/api/alarm-suppressions', headers=admin, json={
            'channel_id': channel_ids[0], 'reason': 'Commissioning test window', 'start_at': start, 'end_at': end,
        })
        assert suppression.status_code == 200, suppression.text
        suppression_id = suppression.json()['id']

        suppressed = client.post('/api/telemetry/ingest', headers=admin, json={
            'readings': [{'channel_code': 'TEL-V4-CB-VOLT', 'value': 25, 'quality': 'Good', 'external_id': 'v4-v-001'}],
        })
        assert suppressed.status_code == 200 and suppressed.json()['suppressed'] == 1
        assert suppressed.json()['results'][0]['action'] == 'suppressed'
        assert suppressed.json()['results'][0]['suppression_no'].startswith('SUP-')
        assert client.post(f'/api/alarm-suppressions/{suppression_id}/deactivate', headers=admin).status_code == 200

        first = client.post('/api/telemetry/ingest', headers=admin, json={
            'readings': [{'channel_code': 'TEL-V4-CB-VOLT', 'value': 25, 'quality': 'Good', 'external_id': 'v4-v-002'}],
        }).json()
        second = client.post('/api/telemetry/ingest', headers=admin, json={
            'readings': [{'channel_code': 'TEL-V4-CB-TEMP', 'value': 25, 'quality': 'Good', 'external_id': 'v4-t-001'}],
        }).json()
        assert first['alarms_opened'] == 1 and second['alarms_opened'] == 1
        inc_no_1 = first['results'][0]['incident_no']; inc_no_2 = second['results'][0]['incident_no']
        assert inc_no_1 == inc_no_2

        incidents = client.get('/api/alarm-incidents', headers=admin).json()
        incident = next(i for i in incidents if i['incident_no'] == inc_no_1)
        detail = client.get(f"/api/alarm-incidents/{incident['id']}", headers=admin).json()
        assert detail['alarm_count'] >= 2 and detail['active_alarm_count'] >= 2 and detail['severity'] == 'Critical'

        ack = client.post(f"/api/alarm-incidents/{incident['id']}/acknowledge", headers=admin, json={'notes': 'Control room acknowledged'})
        assert ack.status_code == 200 and ack.json()['status'] == 'Acknowledged'
        blocked_resolve = client.post(f"/api/alarm-incidents/{incident['id']}/resolve", headers=admin, json={'notes': 'Too early'})
        assert blocked_resolve.status_code == 409
        work = client.post(f"/api/alarm-incidents/{incident['id']}/work-order", headers=admin, json={})
        assert work.status_code == 200, work.text
        wo = client.get(f"/api/work-orders/{work.json()['id']}", headers=admin).json()
        assert wo['status'] == 'Submitted' and wo['priority'] == 'Critical' and wo['failure_code'].startswith('INCIDENT-')

        # Clear both member alarms; the incident should resolve automatically.
        for code, ext in [('TEL-V4-CB-VOLT', 'v4-v-clear'), ('TEL-V4-CB-TEMP', 'v4-t-clear')]:
            clear = client.post('/api/telemetry/ingest', headers=admin, json={
                'readings': [{'channel_code': code, 'value': 5, 'quality': 'Good', 'external_id': ext}],
            })
            assert clear.status_code == 200 and clear.json()['alarms_cleared'] == 1
        resolved = client.get(f"/api/alarm-incidents/{incident['id']}", headers=admin).json()
        assert resolved['status'] == 'Resolved' and resolved['active_alarm_count'] == 0


def test_v4_utility_command_center_and_metrics():
    with TestClient(app) as client:
        admin = auth(client)
        cc = client.get('/api/operations/command-center', headers=admin)
        assert cc.status_code == 200, cc.text
        data = cc.json()
        assert 'summary' in data and 'data_quality' in data and 'incidents' in data and 'suppressions' in data
        assert data['summary']['telemetry_channels'] >= 3
        launchpad = client.get('/api/launchpad', headers=admin).json()
        assert any(x['code'] == 'commandcenter' for x in launchpad)
        metrics = client.get('/api/metrics', headers=admin)
        assert metrics.status_code == 200
        text = metrics.text
        assert 'euas_open_alarm_incidents' in text
        assert 'euas_active_alarm_suppressions' in text
        assert 'euas_bad_quality_readings_24h' in text
        assert client.get('/api/exports/alarm-incidents.csv', headers=admin).status_code == 200
        assert client.get('/api/exports/alarm-suppressions.csv', headers=admin).status_code == 200
        assert client.get('/api/exports/telemetry-batches.csv', headers=admin).status_code == 200
        analytics = client.get('/api/analytics', headers=admin)
        assert analytics.status_code == 200
        assert 'operational_incident_summary' in analytics.json() and 'telemetry_quality_24h' in analytics.json()


def test_v4_machine_to_machine_telemetry_api_key():
    with TestClient(app) as client:
        admin = auth(client)
        assets = client.get('/api/assets', headers=admin).json()
        gen = next(a for a in assets if a['asset_no'] == 'GEN-201')
        created_channel = client.post('/api/telemetry/channels', headers=admin, json={
            'channel_code': 'TEL-V4-M2M-GEN-V', 'asset_id': gen['id'], 'name': 'Generator Output Voltage',
            'metric_type': 'Voltage', 'unit': 'V', 'source_system': 'Gateway-V4', 'warning_high': 450, 'critical_high': 480,
        })
        assert created_channel.status_code == 200, created_channel.text

        key_resp = client.post('/api/integrations/api-keys', headers=admin, json={'name': 'Regression SCADA Gateway'})
        assert key_resp.status_code == 200, key_resp.text
        key_body = key_resp.json(); raw_key = key_body['api_key']; key_id = key_body['id']
        assert raw_key.startswith('euas_')

        # No user bearer token: the integration key is the authentication principal.
        ingest = client.post('/api/telemetry/ingest', headers={'X-EUAS-Integration-Key': raw_key}, json={
            'source_system': 'Gateway-V4',
            'readings': [{'channel_code': 'TEL-V4-M2M-GEN-V', 'value': 440, 'quality': 'Good', 'external_id': 'm2m-v-001'}],
        })
        assert ingest.status_code == 200, ingest.text
        assert ingest.json()['accepted'] == 1

        listing = client.get('/api/integrations/api-keys', headers=admin)
        assert listing.status_code == 200
        row = next(x for x in listing.json() if x['id'] == key_id)
        assert row['last_used_at'] is not None
        assert 'key_hash' not in row and 'api_key' not in row

        assert client.post(f'/api/integrations/api-keys/{key_id}/revoke', headers=admin).status_code == 200
        denied = client.post('/api/telemetry/ingest', headers={'X-EUAS-Integration-Key': raw_key}, json={
            'readings': [{'channel_code': 'TEL-V4-M2M-GEN-V', 'value': 441, 'external_id': 'm2m-v-002'}],
        })
        assert denied.status_code == 401
