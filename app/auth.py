import hashlib
import secrets
from datetime import datetime
from typing import Optional
from fastapi import Header, HTTPException, Depends
from .database import PostgresCursor, db

PBKDF2_ROUNDS = 180_000


def _postgres_cursor_iter(cursor):
    """Provide the sqlite cursor iteration contract used throughout EUAS.

    EUAS query code historically iterates directly over sqlite cursors. The
    PostgreSQL adapter exposes HybridRow through fetchone/fetchall, so bridge
    the same behavior here for all application/auth/bootstrap paths.
    """
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row


# Keep the adapter behavior compatible with sqlite without changing callers.
if '__iter__' not in PostgresCursor.__dict__:
    PostgresCursor.__iter__ = _postgres_cursor_iter


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
    return f'{salt}${digest}'


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split('$', 1)
    candidate = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
    return secrets.compare_digest(candidate, digest)


def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(401, 'Authentication required')
    token = authorization.split(' ', 1)[1]
    with db() as conn:
        row = conn.execute('''
            SELECT u.id,u.username,u.full_name,u.email,r.code role,r.name role_name,u.active
            FROM sessions s JOIN users u ON u.id=s.user_id JOIN roles r ON r.id=u.role_id
            WHERE s.token=? AND s.expires_at>?
        ''', (token, datetime.now().isoformat())).fetchone()
        if not row or not row['active']:
            raise HTTPException(401, 'Invalid or expired session')
        return dict(row)


def require_roles(*roles):
    def check(user=Depends(current_user)):
        if user['role'] not in roles:
            raise HTTPException(403, 'Insufficient permissions')
        return user
    return check
