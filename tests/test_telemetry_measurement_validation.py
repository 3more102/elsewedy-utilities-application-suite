from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
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
            'name': f'{code} measurement',
            'metric_type': 'Current',
            'unit': 'A',
            'source_system': 'Measurement validation test',
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


def reading_count(channel_id):
    with db() as conn:
        return int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )


def post_raw(client, headers, payload: str):
    return client.post(
        '/api/telemetry/ingest',
        headers={**headers, 'Content-Type': 'application/json'},
        content=payload,
    )


@pytest.mark.parametrize(
    ('literal', 'suffix'),
    [('NaN', 'Nan'), ('Infinity', 'PosInf'), ('-Infinity', 'NegInf')],
)
def test_non_finite_measurement_is_rejected_without_persisting(literal, suffix):
    code = f'TEL-NAN-REJECTED-{suffix}'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        response = post_raw(
            client,
            headers,
            f'{{"readings": [{{"channel_code": "{code}", "value": {literal}}}]}}',
        )
        assert response.status_code == 400, response.text

        assert reading_count(channel_id) == 0
        state = channel_state(channel_id)
        assert state['last_reading_at'] is None
        assert state['last_value'] is None


def test_batch_containing_non_finite_value_persists_nothing():
    code = 'TEL-NAN-BATCH'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        response = post_raw(
            client,
            headers,
            '{"readings": ['
            f'{{"channel_code": "{code}", "value": 20}}, '
            f'{{"channel_code": "{code}", "value": NaN}}'
            ']}',
        )
        assert response.status_code == 400, response.text
        assert reading_count(channel_id) == 0
        assert channel_state(channel_id)['last_reading_at'] is None


def test_atomic_ingest_rejects_non_finite_value_and_rolls_back():
    code = 'TEL-NAN-ATOMIC'

    with TestClient(app):
        with db() as conn:
            user = dict(
                conn.execute(
                    """SELECT u.id,u.full_name,r.code role FROM users u
                       JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
                ).fetchone()
            )
            asset = conn.execute(
                'SELECT id FROM assets ORDER BY id LIMIT 1'
            ).fetchone()
            stamp = telemetry_store.now()
            channel_id = int(
                conn.execute(
                    '''INSERT INTO telemetry_channels(
                         channel_code,asset_id,name,metric_type,unit,source_system,
                         warning_high,critical_high,active,created_at,updated_at
                       ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
                    (code, asset['id'], f'{code} atomic', stamp, stamp),
                ).lastrowid
            )

        # Bypass Pydantic request validation to prove the ingestion transaction
        # itself refuses non-finite measurements (defense in depth).
        item = telemetry_store._application.TelemetryReadingItem.model_construct(
            channel_code=code,
            value=float('nan'),
            quality='Good',
            captured_at=None,
            source=None,
        )
        body = telemetry_store._application.TelemetryIngestIn.model_construct(
            readings=[item]
        )

        with pytest.raises(HTTPException) as excinfo:
            with db() as conn:
                telemetry_store.ingest_telemetry_atomic(conn, body, user)
        assert excinfo.value.status_code == 400

        assert reading_count(channel_id) == 0
        assert channel_state(channel_id)['last_reading_at'] is None


def test_future_capture_far_beyond_skew_is_rejected():
    code = 'TEL-FUTURE-SKEW'
    future_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        response = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={
                'readings': [
                    {
                        'channel_code': code,
                        'value': 80,
                        'quality': 'Good',
                        'captured_at': future_at,
                    }
                ]
            },
        )
        assert response.status_code == 400, response.text
        assert 'future' in response.json()['detail']
        assert reading_count(channel_id) == 0
        assert channel_state(channel_id)['last_reading_at'] is None


def test_future_capture_within_configured_skew_is_live(monkeypatch):
    code = 'TEL-FUTURE-TOLERATED'
    monkeypatch.setattr(telemetry_store, 'TELEMETRY_MAX_FUTURE_SKEW_SECONDS', 7200)
    future_at = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
    ).isoformat()

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        response = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={
                'readings': [
                    {
                        'channel_code': code,
                        'value': 60,
                        'quality': 'Good',
                        'captured_at': future_at,
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['historical'] == 0
        assert payload['alarms_opened'] == 1

        state = channel_state(channel_id)
        assert state['last_reading_at'] == future_at
        assert float(state['last_value']) == 60
        assert reading_count(channel_id) == 1


def test_valid_reading_is_accepted_after_invalid_attempts():
    code = 'TEL-VALID-AFTER-BAD'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        rejected = post_raw(
            client,
            headers,
            f'{{"readings": [{{"channel_code": "{code}", "value": Infinity}}]}}',
        )
        assert rejected.status_code == 400

        accepted = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 40, 'quality': 'Good'}]},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['normal'] == 1
        assert reading_count(channel_id) == 1
        state = channel_state(channel_id)
        assert float(state['last_value']) == 40
        assert state['last_reading_at']


def test_metrics_expose_stale_telemetry_gauge():
    code = 'TEL-STALE-GAUGE'

    def stale_gauge(text):
        for line in text.splitlines():
            if line.startswith('euas_telemetry_stale_channels_24h '):
                return int(line.rsplit(' ', 1)[1])
        raise AssertionError('missing euas_telemetry_stale_channels_24h gauge')

    with TestClient(app) as client:
        headers = auth(client)
        baseline = stale_gauge(client.get('/api/metrics', headers=headers).text)

        channel_id = create_channel(client, headers, code)
        after_create = stale_gauge(client.get('/api/metrics', headers=headers).text)
        assert after_create == baseline + 1

        ingested = client.post(
            '/api/telemetry/ingest',
            headers=headers,
            json={'readings': [{'channel_code': code, 'value': 10, 'quality': 'Good'}]},
        )
        assert ingested.status_code == 200, ingested.text
        after_fresh = stale_gauge(client.get('/api/metrics', headers=headers).text)
        assert after_fresh == baseline


def _set_last_reading_at(channel_id, value):
    with db() as conn:
        conn.execute(
            'UPDATE telemetry_channels SET last_reading_at=? WHERE id=?',
            (value, channel_id),
        )


def test_stale_gauge_classifies_offset_timestamps_by_instant():
    from datetime import datetime, timedelta

    now = datetime.now()
    stale_cut = now - timedelta(hours=24)

    # A '+14:00' local string sorts lexicographically AFTER the cutoff, but
    # its UTC instant (25 hours ago) is genuinely stale.
    stale_instant = (now - timedelta(hours=25)).replace(tzinfo=timezone.utc)
    stale_offset_value = stale_instant.astimezone(
        timezone(timedelta(hours=14))
    ).isoformat()
    assert stale_offset_value > stale_cut.isoformat(timespec='seconds')

    # A '-03:00' local string sorts BEFORE the cutoff, but its UTC instant
    # (one minute past the cutoff) is genuinely fresh.
    fresh_instant = (stale_cut + timedelta(minutes=1)).replace(tzinfo=timezone.utc)
    fresh_offset_value = fresh_instant.astimezone(
        timezone(timedelta(hours=-3))
    ).isoformat()
    assert fresh_offset_value < stale_cut.isoformat(timespec='seconds')

    with TestClient(app) as client:
        headers = auth(client)

        def gauge():
            return int(
                next(
                    line.rsplit(' ', 1)[1]
                    for line in client.get('/api/metrics', headers=headers).text.splitlines()
                    if line.startswith('euas_telemetry_stale_channels_24h ')
                )
            )

        baseline = gauge()
        stale_channel = create_channel(client, headers, 'TEL-STALE-OFFSET-STALE')
        fresh_channel = create_channel(client, headers, 'TEL-STALE-OFFSET-FRESH')
        assert gauge() == baseline + 2

        _set_last_reading_at(stale_channel, stale_offset_value)
        _set_last_reading_at(fresh_channel, fresh_offset_value)
        assert gauge() == baseline + 1
