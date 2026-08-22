import hashlib
import secrets
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException

from .database import db
from .postgres_compat import apply_postgres_compat

PBKDF2_ALGORITHM = 'pbkdf2_sha256'
PBKDF2_ROUNDS = 600_000
LEGACY_PBKDF2_ROUNDS = 180_000
SALT_BYTES = 16

# The application imports auth during startup, before database-backed endpoint
# work begins, making this the compatibility bootstrap for both API and CLI
# execution paths.
apply_postgres_compat()


def _pbkdf2(password: str, salt: str, rounds: int) -> str:
    return hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        rounds,
    ).hex()


def hash_password(password: str, salt: Optional[str] = None, rounds: int = PBKDF2_ROUNDS) -> str:
    """Create a self-describing PBKDF2-HMAC-SHA256 password hash.

    Format: pbkdf2_sha256$<rounds>$<salt>$<digest>
    """
    if not isinstance(password, str):
        raise TypeError('password must be a string')
    if rounds < 1:
        raise ValueError('rounds must be positive')
    salt = salt or secrets.token_hex(SALT_BYTES)
    digest = _pbkdf2(password, salt, rounds)
    return f'{PBKDF2_ALGORITHM}${rounds}${salt}${digest}'


def _parse_password_hash(stored: str):
    """Return (algorithm, rounds, salt, digest, legacy) or None for bad input."""
    if not isinstance(stored, str) or not stored:
        return None

    parts = stored.split('$')
    if len(parts) == 4 and parts[0] == PBKDF2_ALGORITHM:
        try:
            rounds = int(parts[1])
        except (TypeError, ValueError):
            return None
        if rounds < 1 or not parts[2] or not parts[3]:
            return None
        return PBKDF2_ALGORITHM, rounds, parts[2], parts[3], False

    # Backward compatibility with EUAS <= v3.9 hashes: <salt>$<digest>.
    if len(parts) == 2 and parts[0] and parts[1]:
        return PBKDF2_ALGORITHM, LEGACY_PBKDF2_ROUNDS, parts[0], parts[1], True

    return None


def verify_password(password: str, stored: str) -> bool:
    parsed = _parse_password_hash(stored)
    if parsed is None or not isinstance(password, str):
        return False
    _, rounds, salt, digest, _ = parsed
    candidate = _pbkdf2(password, salt, rounds)
    return secrets.compare_digest(candidate, digest)


def password_needs_upgrade(stored: str) -> bool:
    """Return True for legacy/weak hashes that should be replaced at login."""
    parsed = _parse_password_hash(stored)
    if parsed is None:
        return True
    algorithm, rounds, _salt, _digest, legacy = parsed
    return legacy or algorithm != PBKDF2_ALGORITHM or rounds < PBKDF2_ROUNDS


def verify_password_with_upgrade(password: str, stored: str) -> tuple[bool, Optional[str]]:
    """Verify a password and return a replacement hash when an upgrade is due."""
    if not verify_password(password, stored):
        return False, None
    return True, hash_password(password) if password_needs_upgrade(stored) else None


def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(401, 'Authentication required')
    token = authorization.split(' ', 1)[1]
    if not token:
        raise HTTPException(401, 'Authentication required')
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
