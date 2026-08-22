import hashlib
import secrets
from datetime import datetime
from typing import Optional
from fastapi import Header, HTTPException, Depends
from .database import db
from .postgres_compat import apply_postgres_compat

PBKDF2_ROUNDS = 180_000

# The application imports auth during startup, before database-backed endpoint
# work begins, making this the compatibility bootstrap for both API and CLI
# execution paths.
apply_postgres_compat()


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
