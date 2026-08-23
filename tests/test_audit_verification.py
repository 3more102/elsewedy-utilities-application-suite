from __future__ import annotations

import pytest

from app.audit_store import append_audit
from app.audit_verification import AuditIntegrityError, replay_audit_history, verify_audit_chain
from app.database import db


def _admin_id(conn):
    row = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    assert row is not None
    return int(row['id'])


def test_audit_chain_verification_and_replay():
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
    with db() as conn:
        user_id = _admin_id(conn)
        append_audit(conn, user_id, 'CREATE', 'AuditTamper', '1')
        conn.execute("UPDATE audit_logs SET new_value='tampered' WHERE module='AuditTamper'")

    with db() as conn:
        with pytest.raises(AuditIntegrityError):
            verify_audit_chain(conn)
