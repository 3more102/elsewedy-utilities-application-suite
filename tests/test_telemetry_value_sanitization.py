from __future__ import annotations

import math
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db, now
from app.main import app
from app.telemetry_store import ingest_telemetry_atomic


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_channel(conn, suffix: str) -> tuple[int, str, str]:
    user = _admin(conn)
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    assert asset
    stamp = now()
    code = f'TEL-FIN-{suffix}'.upper()
    conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_high,critical_high,active,created_at,updated_at
           ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
        (code, asset['id'], f'Finite value guard {suffix}', stamp, stamp),
    )
    return int(
        conn.execute(
            'SELECT id FROM telemetry_channels WHERE channel_code=?', (code,)
        ).fetchone()[0]
    ), code, user['id']


def _body(code: str, value: float, captured_at: str):
    return _application.TelemetryIngestIn(
        readings=[
            _application.TelemetryReadingItem(
                channel_code=code,
                value=value,
                captured_at=captured_at,
                quality='Good',
                source='CI',
            )
        ]
    )


def _readings(channel_id: int) -> int:
    with db() as conn:
        return int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )


def _active_alarms(channel_id: int) -> list[dict]:
    with db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')""",
                (channel_id,),
            ).fetchall()
        ]


def _channel(channel_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            'SELECT * FROM telemetry_channels WHERE id=?', (channel_id,)
        ).fetchone()
        assert row
        return dict(row)


def test_nan_reading_cannot_clear_active_alarm():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user_id = _seed_channel(conn, suffix)
            opened = ingest_telemetry_atomic(
                conn,
                _body(code, 80.0, '2026-08-23T03:00:00+03:00'),
                {'id': user_id},
            )
        assert opened['alarms_opened'] == 1

        try:
            with db() as conn:
                ingest_telemetry_atomic(
                    conn,
                    _body(code, math.nan, '2026-08-23T03:10:00+03:00'),
                    {'id': user_id},
                )
        except Exception as exc:
            assert getattr(exc, 'status_code', None) == 400
        else:
            raise AssertionError('NaN value was accepted')

        alarms = _active_alarms(channel_id)
        assert len(alarms) == 1
        assert alarms[0]['status'] == 'Open'
        assert alarms[0]['severity'] == 'Critical'
        assert float(alarms[0]['trigger_value']) == 80.0
        assert _readings(channel_id) == 1
        assert float(_channel(channel_id)['last_value']) == 80.0


def test_infinite_reading_is_rejected():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, user_id = _seed_channel(conn, suffix)

        for value in (math.inf, -math.inf):
            try:
                with db() as conn:
                    ingest_telemetry_atomic(
                        conn,
                        _body(code, value, '2026-08-23T03:20:00+03:00'),
                        {'id': user_id},
                    )
            except Exception as exc:
                assert getattr(exc, 'status_code', None) == 400
            else:
                raise AssertionError(f'{value!r} value was accepted')

        assert _readings(channel_id) == 0
        assert _channel(channel_id)['last_value'] is None
        assert _active_alarms(channel_id) == []


def test_batch_with_one_nan_rolls_back_entirely():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            channel_a, code_a, user_id = _seed_channel(conn, suffix + '-A')
            channel_b, code_b, _ = _seed_channel(conn, suffix + '-B')

        batch = _application.TelemetryIngestIn(
            readings=[
                _application.TelemetryReadingItem(
                    channel_code=code_a,
                    value=30.0,
                    captured_at='2026-08-23T03:30:00+03:00',
                    quality='Good',
                    source='CI',
                ),
                _application.TelemetryReadingItem(
                    channel_code=code_b,
                    value=math.nan,
                    captured_at='2026-08-23T03:30:00+03:00',
                    quality='Good',
                    source='CI',
                ),
            ]
        )
        try:
            with db() as conn:
                ingest_telemetry_atomic(conn, batch, {'id': user_id})
        except Exception as exc:
            assert getattr(exc, 'status_code', None) == 400
        else:
            raise AssertionError('batch containing NaN was accepted')

        assert _readings(channel_a) == 0
        assert _readings(channel_b) == 0
        assert _active_alarms(channel_a) == []
        assert _active_alarms(channel_b) == []


def test_ingest_api_rejects_non_finite_values():
    with TestClient(app) as client:
        login = client.post(
            '/api/auth/login',
            json={'username': 'omar', 'password': 'EUAS@2026'},
        )
        assert login.status_code == 200
        token = login.json()['token']
        headers = {'Authorization': f'Bearer {token}'}

        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            channel_id, code, _user_id = _seed_channel(conn, suffix)

        # Open a live Critical alarm so we can prove the garbage sample
        # cannot clear it through the public API either.
        opened = client.post(
            '/api/telemetry/ingest',
            json={
                'readings': [
                    {
                        'channel_code': code,
                        'value': 90.0,
                        'captured_at': '2026-08-23T03:40:00+03:00',
                        'quality': 'Good',
                        'source': 'CI',
                    }
                ]
            },
            headers=headers,
        )
        assert opened.status_code == 200
        assert opened.json()['alarms_opened'] == 1

        # httpx refuses to serialize NaN, so send the raw JSON document a
        # misbehaving SCADA client would produce (Python's json parser
        # accepts NaN literals).
        response = client.post(
            '/api/telemetry/ingest',
            content=(
                '{"readings": [{"channel_code": "%s", "value": NaN, '
                '"captured_at": "2026-08-23T03:50:00+03:00", '
                '"quality": "Good", "source": "CI"}]}'
            ).replace('%s', code).encode(),
            headers={**headers, 'Content-Type': 'application/json'},
        )
        assert response.status_code == 400

        alarms = _active_alarms(channel_id)
        assert len(alarms) == 1
        assert alarms[0]['status'] == 'Open'
        assert _readings(channel_id) == 1
