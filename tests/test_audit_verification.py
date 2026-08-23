from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit_store import append_audit
from app.audit_verification import (
    AuditIntegrityError,
    replay_audit_history,
    verify_audit_chain,
    verify_audit_chain_report,
)
from app.database import db
from app.main import app

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'


def _admin_id(conn):
    row = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    assert row is not None
    return int(row['id'])


def test_audit_chain_verification_and_replay():
    with TestClient(app):
        with db() as conn:
            user_id = _admin_id(conn)
            append_audit(conn, user_id, 'CREATE', 'AuditVerification', '1', '', {'state': 'created'})
            append_audit(conn, user_id, 'UPDATE', 'AuditVerification', '1', {'state': 'created'}, {'state': 'updated'})

        with db() as conn:
            assert verify_audit_chain(conn) is True
            history = replay_audit_history(conn)
            assert len(history) >= 2
            assert history[-1]['action'] == 'UPDATE'


def test_audit_tamper_is_detected():
    with TestClient(app):
        with db() as conn:
            user_id = _admin_id(conn)
            append_audit(conn, user_id, 'CREATE', 'AuditTamper', '1')
            row = conn.execute(
                "SELECT id,new_value FROM audit_logs WHERE module='AuditTamper'"
            ).fetchone()
            audit_id, original = int(row['id']), row['new_value']
            conn.execute("UPDATE audit_logs SET new_value='tampered' WHERE module='AuditTamper'")

        with db() as conn:
            with pytest.raises(AuditIntegrityError):
                verify_audit_chain(conn)

        # Restore the exact stored value so the shared regression database keeps a
        # valid chain for subsequent suite modules (same hygiene as the live-API
        # tamper/restore path in test_zzz_governance_lifecycle.py).
        with db() as conn:
            conn.execute(
                'UPDATE audit_logs SET new_value=? WHERE id=?', (original, audit_id)
            )

        with db() as conn:
            assert verify_audit_chain(conn) is True


def _login(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_integrity_api_and_replay_share_one_validator():
    with TestClient(app) as client:
        admin = _login(client)
        integrity = client.get('/api/audit/integrity', headers=admin)
        assert integrity.status_code == 200, integrity.text
        with db() as conn:
            report = verify_audit_chain_report(conn)
        assert integrity.json() == report


def test_replay_api_returns_verified_timeline():
    with TestClient(app) as client:
        admin = _login(client)
        with db() as conn:
            user_id = _admin_id(conn)
            append_audit(
                conn, user_id, 'CREATE', 'AuditReplayAPI', 'replay-1', '', {'k': 'v'}
            )
        replayed = client.get('/api/audit/replay', headers=admin)
        assert replayed.status_code == 200, replayed.text
        body = replayed.json()
        assert body['valid'] is True
        assert body['total'] >= 1 and body['returned'] == min(body['total'], 1000)
        events = body['events']
        assert events and events[-1]['audit_hash'] == body['head_hash']
        target = next(e for e in events if e['module'] == 'AuditReplayAPI')
        assert 'k' in target['new_value']

        limited = client.get('/api/audit/replay', headers=admin, params={'limit': 1})
        assert limited.status_code == 200
        lbody = limited.json()
        assert lbody['returned'] == 1 and lbody['total'] == body['total']
        assert lbody['events'][0]['audit_hash'] == body['head_hash']


def test_replay_api_rejects_tampered_chain_and_recovers():
    with TestClient(app) as client:
        admin = _login(client)
        assert client.get('/api/audit/replay', headers=admin).status_code == 200
        with sqlite3.connect(TEST_DB) as conn:
            row = conn.execute(
                'SELECT id,new_value FROM audit_logs ORDER BY id DESC LIMIT 1'
            ).fetchone()
            audit_id, original = row
            conn.execute(
                'UPDATE audit_logs SET new_value=? WHERE id=?',
                ('tampered-replay-regression', audit_id),
            )
            conn.commit()
        broken = client.get('/api/audit/replay', headers=admin)
        assert broken.status_code == 409
        assert str(audit_id) in broken.json()['detail']
        # The integrity endpoint must agree that the chain is broken.
        assert client.get('/api/audit/integrity', headers=admin).json()['valid'] is False
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute(
                'UPDATE audit_logs SET new_value=? WHERE id=?', (original, audit_id)
            )
            conn.commit()
        recovered = client.get('/api/audit/replay', headers=admin)
        assert recovered.status_code == 200 and recovered.json()['valid'] is True


def test_replay_api_authorization_matches_integrity_endpoint():
    with TestClient(app) as client:
        admin = _login(client)
        viewer = _login(client, 'exec', 'Viewer@2026')
        tech = _login(client, 'tech1', 'Tech@2026')
        for user in (admin, viewer):
            r = client.get('/api/audit/replay', headers=user)
            assert r.status_code == 200, r.text
        assert client.get('/api/audit/replay', headers=tech).status_code == 403
        assert client.get('/api/audit/replay').status_code == 401
