from __future__ import annotations

from datetime import datetime, timedelta
import json
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.auth import hash_password
from app.config import OUTBOX_LEASE_SECONDS
from app.database import db, now
from app.main import app
from app.outbox_observability import outbox_operational_snapshot


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _metric_value(text: str, name: str) -> float:
    prefix = name + ' '
    matches = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, (name, matches)
    return float(matches[0].split(' ', 1)[1])


def _admin_token(client: TestClient) -> str:
    login = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert login.status_code == 200, login.text
    return login.json()['token']


def _seed(
    conn,
    suffix: str,
    index: int,
    *,
    status: str,
    attempts: int,
    processed_at: str | None,
) -> None:
    conn.execute(
        '''INSERT INTO event_outbox(
             event_no,event_type,aggregate_type,aggregate_id,payload_json,
             status,attempts,created_at,processed_at,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (
            f'EVT-OBS-{suffix}-{index}',
            'test.outbox.observability',
            f'test-observability-{suffix}',
            str(index),
            '{"secret":"DO_NOT_EXPOSE_PAYLOAD"}',
            status,
            attempts,
            now(),
            processed_at,
            'seed error' if status == 'Failed' else '',
        ),
    )


def test_operational_snapshot_classifies_backlog_without_payloads():
    with TestClient(app):
        with db() as conn:
            before = outbox_operational_snapshot(conn)
            suffix = uuid.uuid4().hex[:10]
            active = now()
            stale = (
                datetime.now()
                - timedelta(seconds=OUTBOX_LEASE_SECONDS + 5)
            ).isoformat(timespec='seconds')
            _seed(conn, suffix, 1, status='Pending', attempts=0, processed_at=None)
            _seed(conn, suffix, 2, status='Failed', attempts=1, processed_at=None)
            _seed(conn, suffix, 3, status='Pending', attempts=1, processed_at=active)
            _seed(conn, suffix, 4, status='Pending', attempts=1, processed_at=stale)
            _seed(
                conn,
                suffix,
                5,
                status='Failed',
                attempts=_application.OUTBOX_MAX_ATTEMPTS,
                processed_at=None,
            )
            _seed(conn, suffix, 6, status='Delivered', attempts=1, processed_at=active)
            _seed(conn, suffix, 7, status='Skipped', attempts=1, processed_at=active)

        with db() as conn:
            after = outbox_operational_snapshot(conn)
            conn.execute(
                'DELETE FROM event_outbox WHERE aggregate_type=?',
                (f'test-observability-{suffix}',),
            )

        assert after['queue']['queued'] == before['queue']['queued'] + 1
        assert after['queue']['failed_retryable'] == before['queue']['failed_retryable'] + 1
        assert after['queue']['active_leases'] == before['queue']['active_leases'] + 1
        assert after['queue']['stale_leases'] == before['queue']['stale_leases'] + 1
        assert after['queue']['exhausted'] == before['queue']['exhausted'] + 1
        assert after['queue']['retryable'] == before['queue']['retryable'] + 3
        assert after['queue']['unresolved'] == before['queue']['unresolved'] + 5
        assert after['terminal']['delivered'] == before['terminal']['delivered'] + 1
        assert after['terminal']['skipped'] == before['terminal']['skipped'] + 1
        assert after['total_events'] == before['total_events'] + 7
        assert 'payload' not in json.dumps(after).lower()
        assert 'DO_NOT_EXPOSE_PAYLOAD' not in json.dumps(after)


def test_outbox_status_endpoint_is_available_to_admin_and_payload_free():
    with TestClient(app) as client:
        token = _admin_token(client)
        response = client.get('/api/events/outbox/status', headers=_bearer(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body['queue']) == {
            'retryable',
            'queued',
            'failed_retryable',
            'active_leases',
            'stale_leases',
            'exhausted',
            'unresolved',
        }
        assert body['config']['max_attempts'] == _application.OUTBOX_MAX_ATTEMPTS
        assert body['config']['lease_seconds'] == OUTBOX_LEASE_SECONDS
        assert body['oldest_retryable_age_seconds'] is None or body['oldest_retryable_age_seconds'] >= 0
        assert 'payload' not in json.dumps(body).lower()


def test_outbox_status_endpoint_requires_authentication():
    with TestClient(app) as client:
        response = client.get('/api/events/outbox/status')
        assert response.status_code == 401


def test_outbox_status_preserves_operator_role_ceiling():
    username = f'outbox-observer-tech-{uuid.uuid4().hex[:8]}'
    password = 'Observer@2026!'
    with TestClient(app) as client:
        with db() as conn:
            role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
            assert role
            conn.execute(
                '''INSERT INTO users(
                     username,password_hash,full_name,email,role_id,department,phone,active,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)''',
                (
                    username,
                    hash_password(password),
                    'Outbox Observer Technician',
                    f'{username}@example.test',
                    role['id'],
                    'QA',
                    '',
                    1,
                    now(),
                ),
            )

        login = client.post(
            '/api/auth/login',
            json={'username': username, 'password': password},
        )
        assert login.status_code == 200, login.text
        response = client.get(
            '/api/events/outbox/status',
            headers=_bearer(login.json()['token']),
        )
        assert response.status_code == 403


def test_metrics_exposes_snapshot_consistent_outbox_lease_gauges():
    with TestClient(app) as client:
        token = _admin_token(client)
        with db() as conn:
            snapshot = outbox_operational_snapshot(conn)

        response = client.get('/api/metrics', headers=_bearer(token))
        assert response.status_code == 200, response.text
        text = response.text
        queue = snapshot['queue']
        assert _metric_value(text, 'euas_outbox_retryable') == queue['retryable']
        assert _metric_value(text, 'euas_outbox_queued') == queue['queued']
        assert _metric_value(text, 'euas_outbox_failed_retryable') == queue['failed_retryable']
        assert _metric_value(text, 'euas_outbox_active_leases') == queue['active_leases']
        assert _metric_value(text, 'euas_outbox_stale_leases') == queue['stale_leases']
        assert _metric_value(text, 'euas_outbox_unresolved') == queue['unresolved']
        expected_age = float(snapshot['oldest_retryable_age_seconds'] or 0)
        observed_age = _metric_value(text, 'euas_outbox_oldest_retryable_age_seconds')
        assert observed_age >= expected_age
        assert observed_age - expected_age <= 5


def test_metrics_preserves_existing_outbox_gauges_without_duplicates():
    with TestClient(app) as client:
        token = _admin_token(client)
        response = client.get('/api/metrics', headers=_bearer(token))
        assert response.status_code == 200, response.text
        text = response.text

        # Existing gauges remain present while each new gauge is emitted once.
        assert len([x for x in text.splitlines() if x.startswith('euas_outbox_pending ')]) == 1
        assert len([x for x in text.splitlines() if x.startswith('euas_outbox_attempt_exhausted ')]) == 1
        for name in (
            'euas_outbox_retryable',
            'euas_outbox_queued',
            'euas_outbox_failed_retryable',
            'euas_outbox_active_leases',
            'euas_outbox_stale_leases',
            'euas_outbox_unresolved',
            'euas_outbox_oldest_retryable_age_seconds',
        ):
            _metric_value(text, name)
