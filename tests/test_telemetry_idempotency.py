from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application
from app.database import db, now
from app.main import app
from app.telemetry_store import ingest_telemetry_atomic


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
            'name': f'{code} idempotency',
            'metric_type': 'Current',
            'unit': 'A',
            'source_system': 'Idempotency test',
            'warning_high': 50,
            'critical_high': 75,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()['id']


def ingest(client, headers, code, value, client_ref):
    return client.post(
        '/api/telemetry/ingest',
        headers=headers,
        json={
            'readings': [
                {
                    'channel_code': code,
                    'value': value,
                    'quality': 'Good',
                    'client_ref': client_ref,
                }
            ]
        },
    )


def reading_count(channel_id):
    with db() as conn:
        return int(
            conn.execute(
                'SELECT COUNT(*) FROM telemetry_readings WHERE channel_id=?',
                (channel_id,),
            ).fetchone()[0]
        )


def alarms_for(channel_id):
    with db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM operational_alarms
                   WHERE channel_id=? AND status IN ('Open','Acknowledged')""",
                (channel_id,),
            ).fetchall()
        ]


def test_client_ref_redelivery_is_duplicate_without_side_effects():
    code = f'TEL-IDEM-RETRY-{uuid.uuid4().hex[:6].upper()}'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        first = ingest(client, headers, code, 80, 'ref-retry-1')
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload['accepted'] == 1 and payload['duplicates'] == 0
        assert payload['alarms_opened'] == 1

        replay = ingest(client, headers, code, 80, 'ref-retry-1')
        assert replay.status_code == 200, replay.text
        replay_payload = replay.json()
        assert replay_payload['accepted'] == 0
        assert replay_payload['duplicates'] == 1
        assert replay_payload['results'][0]['action'] == 'duplicate'
        assert replay_payload['alarms_opened'] == 0
        assert replay_payload['alarms_updated'] == 0

        assert reading_count(channel_id) == 1
        active = alarms_for(channel_id)
        assert len(active) == 1
        assert int(active[0]['occurrence_count']) == 1


def test_duplicate_retry_cannot_flip_alarm_state():
    code = f'TEL-IDEM-FLIP-{uuid.uuid4().hex[:6].upper()}'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)

        opened = ingest(client, headers, code, 80, 'ref-flip-1')
        assert opened.status_code == 200 and opened.json()['alarms_opened'] == 1

        ignored = ingest(client, headers, code, 30, 'ref-flip-1')
        assert ignored.status_code == 200
        assert ignored.json()['duplicates'] == 1
        assert ignored.json()['alarms_cleared'] == 0

        active = alarms_for(channel_id)
        assert len(active) == 1
        assert float(active[0]['trigger_value']) == 80.0
        assert active[0]['status'] == 'Open'


def test_distinct_client_refs_and_channels_are_processed_normally():
    code = f'TEL-IDEM-DISTINCT-{uuid.uuid4().hex[:6].upper()}'

    with TestClient(app) as client:
        headers = auth(client)
        channel_id = create_channel(client, headers, code)
        other_id = create_channel(client, headers, f'{code}-B')

        first = ingest(client, headers, code, 20, 'shared-ref')
        second = ingest(client, headers, code, 60, 'other-ref')
        assert first.status_code == 200 and first.json()['normal'] == 1
        assert second.status_code == 200 and second.json()['alarms_opened'] == 1

        cross = ingest(client, headers, f'{code}-B', 10, 'shared-ref')
        assert cross.status_code == 200, cross.text
        assert cross.json()['normal'] == 1
        assert cross.json()['duplicates'] == 0

        assert reading_count(channel_id) == 2
        assert reading_count(other_id) == 1


def test_concurrent_identical_delivery_persists_exactly_one_reading():
    suffix = uuid.uuid4().hex[:10]
    code = f'TEL-IDEM-RACE-{suffix}'.upper()
    workers = 6

    with TestClient(app):
        with db() as conn:
            user = dict(
                conn.execute(
                    """SELECT u.id,u.full_name,r.code role FROM users u
                       JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
                ).fetchone()
            )
            asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
            stamp = now()
            cursor = conn.execute(
                '''INSERT INTO telemetry_channels(
                     channel_code,asset_id,name,metric_type,unit,source_system,
                     warning_high,critical_high,active,created_at,updated_at
                   ) VALUES(?,?,?,'Current','A','CI',50,75,1,?,?)''',
                (code, asset['id'], f'idem race {suffix}', stamp, stamp),
            )
            channel_id = int(cursor.lastrowid)

        body = application.TelemetryIngestIn(
            readings=[
                application.TelemetryReadingItem(
                    channel_code=code,
                    value=80.0,
                    quality='Good',
                    client_ref=f'race-{suffix}',
                )
            ]
        )

        barrier = threading.Barrier(workers)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=15)
                with db() as conn:
                    results.append(ingest_telemetry_atomic(conn, body, user))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=45)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == workers

        total_accepted = sum(result['accepted'] for result in results)
        total_duplicates = sum(result['duplicates'] for result in results)
        assert total_accepted + total_duplicates == workers
        assert total_accepted == 1

        assert reading_count(channel_id) == 1
        active = alarms_for(channel_id)
        assert len(active) == 1
        assert int(active[0]['occurrence_count']) == 1


def test_legacy_database_gains_client_ref_idempotency_on_startup():
    import sqlite3

    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        '''CREATE TABLE telemetry_readings(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             channel_id INTEGER NOT NULL,
             value REAL NOT NULL,
             quality TEXT NOT NULL DEFAULT 'Good',
             source TEXT NOT NULL DEFAULT 'Manual',
             captured_at TEXT NOT NULL,
             ingested_at TEXT NOT NULL,
             ingested_by INTEGER
           )'''
    )

    from app.database import _ensure_telemetry_idempotency

    _ensure_telemetry_idempotency(conn)

    columns = {r['name'] for r in conn.execute('PRAGMA table_info(telemetry_readings)')}
    assert 'client_ref' in columns

    def insert(value, ref):
        return conn.execute(
            '''INSERT OR IGNORE INTO telemetry_readings(
                 channel_id,value,captured_at,ingested_at,client_ref
               ) VALUES(?,?,?,?,?)''',
            (1, value, now(), now(), ref),
        ).rowcount

    assert int(insert(10.0, 'ref-a')) == 1
    assert int(insert(99.0, 'ref-a')) == 0
    assert int(insert(20.0, None)) == 1
    assert int(insert(30.0, None)) == 1
