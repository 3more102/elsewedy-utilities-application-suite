from fastapi.testclient import TestClient

from app import telemetry_store
from app.database import db
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    response = client.post(
        '/api/auth/login',
        json={'username': username, 'password': password},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def create_channel(client, headers, code):
    assets = client.get('/api/assets', headers=headers).json()
    asset = next(a for a in assets if a['asset_no'] == 'CB-101')
    response = client.post(
        '/api/telemetry/channels',
        headers=headers,
        json={
            'channel_code': code,
            'asset_id': asset['id'],
            'name': f'{code} Current',
            'metric_type': 'Current',
            'unit': 'A',
            'source_system': 'Temporal test',
            'warning_high': 50,
            'critical_high': 75,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()['id']


def channel_state(channel_id):
    with db() as conn:
        row = conn.execute(
            'SELECT * FROM telemetry_channels WHERE id=?',
            (channel_id,),
        ).fetchone()
        return dict(row)


def active_alarms(channel_id):
    with db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')
                   ORDER BY id""",
                (channel_id,),
            ).fetchall()
        ]


def reading_count(channel_id):
    with db() as conn:
        return int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )


def ingest(client, headers, code, value, captured_at):
    return client.post(
        '/api/telemetry/ingest',
        headers=headers,
        json={
            'readings': [
                {
                    'channel_code': code,
                    'value': value,
                    'quality': 'Good',
                    'captured_at': captured_at,
                }
            ]
        },
    )


def test_delayed_normal_reading_cannot_clear_newer_critical_alarm():
    code = 'TEL-TEMP-NEWER-CRITICAL'
    newer_at = '2026-08-23T01:20:00+03:00'
    older_at = '2026-08-23T01:10:00+03:00'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        newer = ingest(client, headers, code, 80, newer_at)
        assert newer.status_code == 200, newer.text
        assert newer.json()['alarms_opened'] == 1
        assert newer.json()['historical'] == 0

        stale = ingest(client, headers, code, 30, older_at)
        assert stale.status_code == 200, stale.text
        payload = stale.json()
        assert payload['accepted'] == 1
        assert payload['historical'] == 1
        assert payload['alarms_cleared'] == 0
        assert payload['results'][0]['action'] == 'historical'
        assert payload['results'][0]['current_at'] == newer_at

        state = channel_state(channel_id)
        assert float(state['last_value']) == 80
        assert state['last_reading_at'] == newer_at

        alarms = active_alarms(channel_id)
        assert len(alarms) == 1
        assert alarms[0]['severity'] == 'Critical'
        assert int(alarms[0]['occurrence_count']) == 1
        assert float(alarms[0]['trigger_value']) == 80
        assert alarms[0]['last_seen_at'] == newer_at
        assert reading_count(channel_id) == 2


def test_delayed_high_reading_cannot_reopen_after_newer_normal_state():
    code = 'TEL-TEMP-NEWER-NORMAL'
    newer_at = '2026-08-23T01:40:00+03:00'
    older_at = '2026-08-23T01:30:00+03:00'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        newer = ingest(client, headers, code, 20, newer_at)
        assert newer.status_code == 200, newer.text
        assert newer.json()['normal'] == 1

        stale = ingest(client, headers, code, 90, older_at)
        assert stale.status_code == 200, stale.text
        assert stale.json()['historical'] == 1
        assert stale.json()['alarms_opened'] == 0
        assert stale.json()['results'][0]['action'] == 'historical'

        state = channel_state(channel_id)
        assert float(state['last_value']) == 20
        assert state['last_reading_at'] == newer_at
        assert active_alarms(channel_id) == []
        assert reading_count(channel_id) == 2


def test_equal_event_time_replay_is_historical_and_does_not_duplicate_alarm_evidence():
    code = 'TEL-TEMP-SAME-INSTANT'
    captured_at = '2026-08-23T01:50:00+03:00'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        first = ingest(client, headers, code, 80, captured_at)
        replay = ingest(client, headers, code, 95, captured_at)
        assert first.status_code == 200, first.text
        assert first.json()['alarms_opened'] == 1
        assert replay.status_code == 200, replay.text
        assert replay.json()['historical'] == 1
        assert replay.json()['alarms_opened'] == 0
        assert replay.json()['alarms_updated'] == 0
        assert replay.json()['results'][0]['action'] == 'historical'

        state = channel_state(channel_id)
        assert float(state['last_value']) == 80
        assert state['last_reading_at'] == captured_at
        alarms = active_alarms(channel_id)
        assert len(alarms) == 1
        assert int(alarms[0]['occurrence_count']) == 1
        assert float(alarms[0]['trigger_value']) == 80
        assert reading_count(channel_id) == 2


def test_equivalent_timezone_instant_is_not_a_new_live_generation():
    code = 'TEL-TEMP-EQUIVALENT-INSTANT'
    first_at = '2026-08-23T01:55:00+03:00'
    equivalent_at = '2026-08-22T22:55:00Z'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        first = ingest(client, headers, code, 20, first_at)
        equivalent = ingest(client, headers, code, 90, equivalent_at)
        assert first.status_code == 200, first.text
        assert equivalent.status_code == 200, equivalent.text
        assert equivalent.json()['historical'] == 1
        assert equivalent.json()['alarms_opened'] == 0

        state = channel_state(channel_id)
        assert float(state['last_value']) == 20
        assert state['last_reading_at'] == first_at
        assert active_alarms(channel_id) == []
        assert reading_count(channel_id) == 2


def test_untimestamped_readings_in_same_server_second_remain_live(monkeypatch):
    code = 'TEL-TEMP-SERVER-TIME-TIE'
    fixed_now = '2026-08-23T01:59:59'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)
        monkeypatch.setattr(telemetry_store, 'now', lambda: fixed_now)

        normal = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 20, 'quality': 'Good'}]},
        )
        warning = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 60, 'quality': 'Good'}]},
        )

        assert normal.status_code == 200, normal.text
        assert normal.json()['normal'] == 1
        assert warning.status_code == 200, warning.text
        assert warning.json()['historical'] == 0
        assert warning.json()['alarms_opened'] == 1
        assert warning.json()['results'][0]['severity'] == 'Warning'

        state = channel_state(channel_id)
        assert float(state['last_value']) == 60
        assert state['last_reading_at'] == fixed_now
        assert len(active_alarms(channel_id)) == 1
        assert reading_count(channel_id) == 2


def test_invalid_capture_timestamp_is_rejected_without_persisting_reading():
    code = 'TEL-TEMP-BAD-TIME'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        response = ingest(client, headers, code, 80, 'not-a-timestamp')
        assert response.status_code == 400, response.text
        assert reading_count(channel_id) == 0
        state = channel_state(channel_id)
        assert state['last_reading_at'] is None
        assert active_alarms(channel_id) == []
