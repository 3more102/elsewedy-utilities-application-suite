from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from fastapi import Header, HTTPException

from core.database import db, now
from apps.audit import audit
from apps.identity import current_user
from core.shared import next_no

TELEMETRY_WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor','technician')


class IntegrationKeyNotFound(Exception):
    pass


def integration_key_digest(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def list_integration_api_keys(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(
        """SELECT k.id,k.key_no,k.name,k.scope,k.active,k.created_at,k.last_used_at,k.expires_at,u.full_name created_by_name
           FROM integration_api_keys k JOIN users u ON u.id=k.created_by ORDER BY k.id DESC"""
    ).fetchall()]


def create_integration_api_key(conn, *, name: str, created_by: int, expires_at: Optional[str] = None) -> dict:
    raw = 'euas_' + secrets.token_urlsafe(32)
    digest = integration_key_digest(raw)
    key_no = next_no(conn, 'integration_api_keys', 'key_no', 'KEY-', 7001)
    cur = conn.execute(
        "INSERT INTO integration_api_keys(key_no,name,key_hash,scope,active,created_by,created_at,expires_at) VALUES(?,?,?,'telemetry:write',1,?,?,?)",
        (key_no, name, digest, created_by, now(), expires_at),
    )
    audit(conn, created_by, 'CREATE API KEY', 'Integrations', key_no, '', {
        'name': name, 'scope': 'telemetry:write', 'expires_at': expires_at,
    })
    return {
        'id': cur.lastrowid,
        'key_no': key_no,
        'name': name,
        'scope': 'telemetry:write',
        'api_key': raw,
        'warning': 'This plaintext key is shown once. Store it securely.',
    }


def revoke_integration_api_key(conn, *, key_id: int, actor_id: int) -> dict:
    key = conn.execute('SELECT * FROM integration_api_keys WHERE id=?', (key_id,)).fetchone()
    if not key:
        raise IntegrationKeyNotFound('Integration API key not found')
    if not key['active']:
        return {'ok': True, 'active': False}
    conn.execute('UPDATE integration_api_keys SET active=0 WHERE id=?', (key_id,))
    audit(conn, actor_id, 'REVOKE API KEY', 'Integrations', key['key_no'], 'Active', 'Revoked')
    return {'ok': True, 'active': False}


def telemetry_ingest_principal(
    authorization: Optional[str] = Header(default=None),
    x_euas_integration_key: Optional[str] = Header(default=None, alias='X-EUAS-Integration-Key'),
):
    if x_euas_integration_key:
        digest = integration_key_digest(x_euas_integration_key)
        stamp = now()
        with db() as conn:
            key = conn.execute(
                "SELECT * FROM integration_api_keys WHERE key_hash=? AND active=1 AND scope='telemetry:write'",
                (digest,),
            ).fetchone()
            if not key or (key['expires_at'] and key['expires_at'] <= stamp):
                raise HTTPException(401, 'Invalid or expired integration API key')
            system = conn.execute("SELECT id,username,full_name FROM users WHERE username='system'").fetchone()
            if not system:
                raise HTTPException(503, 'Automation principal is unavailable')
            conn.execute('UPDATE integration_api_keys SET last_used_at=? WHERE id=?', (stamp, key['id']))
            return {
                'id': system['id'],
                'username': system['username'],
                'full_name': f"Integration: {key['name']}",
                'role': 'integration',
                'role_name': 'Integration Service',
                'integration_key_no': key['key_no'],
            }
    user = current_user(authorization)
    if user['role'] not in TELEMETRY_WRITE_ROLES:
        raise HTTPException(403, 'Insufficient permissions')
    return user
