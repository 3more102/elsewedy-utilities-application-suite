from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from .database import now

BASE_SCHEMA_VERSION = 9
AUTH_SCHEMA_VERSION = 10
SESSION_TOKEN_BYTES = 48
SESSION_TOUCH_INTERVAL_SECONDS = 300
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5
CLIENT_LOGIN_MAX_FAILURES = 50
# Host-independent ceiling per account. Per-(account,host) and per-host scopes
# alone let a distributed attacker rotate source addresses indefinitely and
# keep guessing one account's password without ever tripping a lockout.
ACCOUNT_LOGIN_MAX_FAILURES = 30
LOGIN_LOCK_BASE_SECONDS = 30
LOGIN_LOCK_MAX_SECONDS = 5 * 60


def _stamp(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).isoformat(timespec='seconds')


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ValueError('session token must be a non-empty string')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def login_scope_digest(username: str, client_host: str) -> str:
    normalized = (username or '').strip().casefold()
    host = (client_host or 'unknown').strip().casefold()
    return hashlib.sha256(f'account\0{normalized}\0{host}'.encode('utf-8')).hexdigest()


def client_login_scope_digest(client_host: str) -> str:
    host = (client_host or 'unknown').strip().casefold()
    return hashlib.sha256(f'client\0{host}'.encode('utf-8')).hexdigest()


def account_global_scope_digest(username: str) -> str:
    """Host-independent throttle scope covering every source address at once."""
    normalized = (username or '').strip().casefold()
    return hashlib.sha256(f'account-global\0{normalized}'.encode('utf-8')).hexdigest()


def client_label(user_agent: str) -> str:
    ua = (user_agent or '').strip()
    low = ua.casefold()
    browser = 'Browser'
    if 'edg/' in low:
        browser = 'Edge'
    elif 'chrome/' in low or 'chromium/' in low:
        browser = 'Chrome'
    elif 'firefox/' in low:
        browser = 'Firefox'
    elif 'safari/' in low and 'chrome/' not in low:
        browser = 'Safari'
    elif 'curl/' in low:
        browser = 'curl'
    elif 'python-httpx/' in low:
        browser = 'HTTPX'

    platform = ''
    if 'windows' in low:
        platform = 'Windows'
    elif 'android' in low:
        platform = 'Android'
    elif 'iphone' in low or 'ipad' in low:
        platform = 'iOS'
    elif 'mac os' in low or 'macintosh' in low:
        platform = 'macOS'
    elif 'linux' in low:
        platform = 'Linux'
    return f'{browser} on {platform}' if platform else browser


def _ensure_legacy_digest_row(conn, legacy) -> tuple[int, bool]:
    """Idempotently materialize one historical raw session as a digest row.

    ``INSERT OR IGNORE`` is translated to PostgreSQL ``ON CONFLICT DO NOTHING``
    by the repository compatibility layer. That makes concurrent startup/lazy
    migration safe when two replicas observe the same legacy token.
    """
    digest = token_digest(legacy['token'])
    cur = conn.execute(
        '''INSERT OR IGNORE INTO auth_sessions(
             user_id,token_digest,created_at,last_seen_at,expires_at,revoked_at,client_label,user_agent
           ) VALUES(?,?,?,?,?,NULL,?,?)''',
        (
            legacy['user_id'],
            digest,
            legacy['created_at'],
            legacy['created_at'],
            legacy['expires_at'],
            'Migrated session',
            '',
        ),
    )
    row = conn.execute(
        'SELECT id FROM auth_sessions WHERE token_digest=?', (digest,)
    ).fetchone()
    if not row:
        raise RuntimeError('legacy session digest migration did not materialize a row')
    return int(row['id']), bool(int(cur.rowcount or 0))


def ensure_auth_schema(conn) -> dict:
    """Create auth schema v10 and migrate raw legacy session tokens transactionally.

    The legacy ``sessions`` table is intentionally retained as an empty
    compatibility landing zone for rolling upgrades. Any legacy rows found at
    startup are converted to one-way SHA-256 digests and then deleted. The v10
    marker is written only after all auth DDL/data migration work succeeds in
    this transaction.
    """
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS auth_sessions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_digest TEXT UNIQUE NOT NULL,
      created_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      revoked_at TEXT,
      client_label TEXT DEFAULT '',
      user_agent TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_state
      ON auth_sessions(user_id,revoked_at,expires_at);
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_digest
      ON auth_sessions(token_digest);
    CREATE TABLE IF NOT EXISTS auth_login_throttle(
      scope_digest TEXT PRIMARY KEY,
      failure_count INTEGER NOT NULL DEFAULT 0,
      window_started_at TEXT NOT NULL,
      last_failure_at TEXT NOT NULL,
      blocked_until TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_auth_login_throttle_updated
      ON auth_login_throttle(updated_at);
    ''')

    migrated = 0
    legacy_rows = conn.execute(
        'SELECT token,user_id,created_at,expires_at FROM sessions'
    ).fetchall()
    for legacy in legacy_rows:
        _session_id, inserted = _ensure_legacy_digest_row(conn, legacy)
        migrated += int(inserted)
        conn.execute('DELETE FROM sessions WHERE token=?', (legacy['token'],))

    conn.execute(
        'INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)',
        (AUTH_SCHEMA_VERSION, now()),
    )
    return {'legacy_sessions_migrated': migrated}


def initialize_auth_database(hash_password) -> dict:
    """Compatibility wrapper for the shared schema migration runner.

    ASGI startup historically imported this auth-specific helper. Keep that
    stable entrypoint, but route it through ``app.migrations`` so application
    startup, deployment readiness, workers and the migration CLI all use the
    same registry, structural validation and database locking semantics.

    The return shape remains compatible with callers that expect the historical
    ``legacy_sessions_migrated`` counter.
    """
    from .migrations import initialize_database

    result = initialize_database(hash_password)
    details = result.get('details', {}).get(AUTH_SCHEMA_VERSION, {})
    return {
        'legacy_sessions_migrated': int(details.get('legacy_sessions_migrated', 0))
    }


def create_session(
    conn,
    user_id: int,
    session_hours: int,
    user_agent: str = '',
    *,
    at: Optional[datetime] = None,
) -> dict:
    created = at or datetime.now()
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    digest = token_digest(token)
    expires = created + timedelta(hours=max(1, int(session_hours)))
    ua = (user_agent or '')[:255]
    cur = conn.execute(
        '''INSERT INTO auth_sessions(
             user_id,token_digest,created_at,last_seen_at,expires_at,revoked_at,client_label,user_agent
           ) VALUES(?,?,?,?,?,NULL,?,?)''',
        (
            user_id,
            digest,
            _stamp(created),
            _stamp(created),
            _stamp(expires),
            client_label(ua),
            ua,
        ),
    )
    return {
        'token': token,
        'session_id': int(cur.lastrowid),
        'created_at': _stamp(created),
        'expires_at': _stamp(expires),
    }


def _migrate_one_legacy_session(conn, token: str) -> Optional[int]:
    legacy = conn.execute(
        'SELECT token,user_id,created_at,expires_at FROM sessions WHERE token=?', (token,)
    ).fetchone()
    if not legacy:
        return None
    session_id, _inserted = _ensure_legacy_digest_row(conn, legacy)
    conn.execute('DELETE FROM sessions WHERE token=?', (token,))
    return session_id


def resolve_session(conn, token: str, *, at: Optional[datetime] = None) -> Optional[dict]:
    if not token:
        return None
    stamp = _stamp(at)
    digest = token_digest(token)
    row = conn.execute(
        '''SELECT s.id session_id,u.id,s.created_at,s.last_seen_at,s.expires_at,
                  s.revoked_at,s.client_label,
                  u.username,u.full_name,u.email,u.department,u.phone,u.active,
                  r.code role,r.name role_name
           FROM auth_sessions s
           JOIN users u ON u.id=s.user_id
           JOIN roles r ON r.id=u.role_id
           WHERE s.token_digest=?''',
        (digest,),
    ).fetchone()

    if not row:
        migrated_id = _migrate_one_legacy_session(conn, token)
        if migrated_id is not None:
            row = conn.execute(
                '''SELECT s.id session_id,u.id,s.created_at,s.last_seen_at,s.expires_at,
                          s.revoked_at,s.client_label,
                          u.username,u.full_name,u.email,u.department,u.phone,u.active,
                          r.code role,r.name role_name
                   FROM auth_sessions s
                   JOIN users u ON u.id=s.user_id
                   JOIN roles r ON r.id=u.role_id
                   WHERE s.id=?''',
                (migrated_id,),
            ).fetchone()

    if not row or not row['active'] or row['revoked_at'] or row['expires_at'] <= stamp:
        return None

    result = dict(row)
    try:
        last_seen = datetime.fromisoformat(str(row['last_seen_at']))
        current = at or datetime.now()
        if (current - last_seen).total_seconds() >= SESSION_TOUCH_INTERVAL_SECONDS:
            touched = _stamp(current)
            conn.execute(
                'UPDATE auth_sessions SET last_seen_at=? WHERE id=?',
                (touched, row['session_id']),
            )
            result['last_seen_at'] = touched
    except (TypeError, ValueError):
        touched = stamp
        conn.execute(
            'UPDATE auth_sessions SET last_seen_at=? WHERE id=?',
            (touched, row['session_id']),
        )
        result['last_seen_at'] = touched
    return result


def list_sessions(conn, user_id: int, *, at: Optional[datetime] = None) -> list[dict]:
    stamp = _stamp(at)
    return [
        dict(row) for row in conn.execute(
            '''SELECT id session_id,created_at,last_seen_at,expires_at,client_label
               FROM auth_sessions
               WHERE user_id=? AND revoked_at IS NULL AND expires_at>?
               ORDER BY created_at DESC,id DESC''',
            (user_id, stamp),
        ).fetchall()
    ]


def revoke_session(conn, user_id: int, session_id: int, *, at: Optional[datetime] = None) -> int:
    cur = conn.execute(
        '''UPDATE auth_sessions SET revoked_at=?
           WHERE id=? AND user_id=? AND revoked_at IS NULL''',
        (_stamp(at), session_id, user_id),
    )
    return int(cur.rowcount or 0)


def revoke_other_sessions(
    conn, user_id: int, current_session_id: int, *, at: Optional[datetime] = None
) -> int:
    cur = conn.execute(
        '''UPDATE auth_sessions SET revoked_at=?
           WHERE user_id=? AND id<>? AND revoked_at IS NULL''',
        (_stamp(at), user_id, current_session_id),
    )
    legacy = conn.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    return int(cur.rowcount or 0) + int(legacy.rowcount or 0)


def revoke_all_sessions(conn, user_id: int, *, at: Optional[datetime] = None) -> int:
    cur = conn.execute(
        '''UPDATE auth_sessions SET revoked_at=?
           WHERE user_id=? AND revoked_at IS NULL''',
        (_stamp(at), user_id),
    )
    legacy = conn.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    return int(cur.rowcount or 0) + int(legacy.rowcount or 0)


def active_session_count(conn, *, at: Optional[datetime] = None) -> int:
    stamp = _stamp(at)
    current = conn.execute(
        'SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL AND expires_at>?',
        (stamp,),
    ).fetchone()[0]
    legacy = conn.execute(
        'SELECT COUNT(*) FROM sessions WHERE expires_at>?', (stamp,)
    ).fetchone()[0]
    return int(current) + int(legacy)


def throttle_status(
    conn, scope_digest: str, *, at: Optional[datetime] = None
) -> dict:
    current = at or datetime.now()
    row = conn.execute(
        'SELECT * FROM auth_login_throttle WHERE scope_digest=?', (scope_digest,)
    ).fetchone()
    if not row:
        return {'blocked': False, 'retry_after': 0, 'failure_count': 0}

    try:
        window_start = datetime.fromisoformat(str(row['window_started_at']))
    except (TypeError, ValueError):
        window_start = current - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
    if (current - window_start).total_seconds() >= LOGIN_WINDOW_SECONDS:
        conn.execute('DELETE FROM auth_login_throttle WHERE scope_digest=?', (scope_digest,))
        return {'blocked': False, 'retry_after': 0, 'failure_count': 0}

    blocked_until = None
    if row['blocked_until']:
        try:
            blocked_until = datetime.fromisoformat(str(row['blocked_until']))
        except (TypeError, ValueError):
            blocked_until = None
    retry_after = max(0, int((blocked_until - current).total_seconds()) + 1) if blocked_until and blocked_until > current else 0
    return {
        'blocked': retry_after > 0,
        'retry_after': retry_after,
        'failure_count': int(row['failure_count'] or 0),
    }


def record_login_failure(
    conn,
    scope_digest: str,
    *,
    at: Optional[datetime] = None,
    max_failures: int = LOGIN_MAX_FAILURES,
) -> dict:
    current = at or datetime.now()
    threshold = max(1, int(max_failures))
    stamp = _stamp(current)
    cutoff = _stamp(current - timedelta(seconds=LOGIN_WINDOW_SECONDS))
    conn.execute(
        '''INSERT OR IGNORE INTO auth_login_throttle(
             scope_digest,failure_count,window_started_at,last_failure_at,blocked_until,updated_at
           ) VALUES(?,0,?,?,NULL,?)''',
        (scope_digest, stamp, stamp, stamp),
    )
    conn.execute(
        '''UPDATE auth_login_throttle
           SET failure_count=0,window_started_at=?,blocked_until=NULL,updated_at=?
           WHERE scope_digest=? AND window_started_at<=?''',
        (stamp, stamp, scope_digest, cutoff),
    )
    conn.execute(
        '''UPDATE auth_login_throttle
           SET failure_count=failure_count+1,last_failure_at=?,updated_at=?
           WHERE scope_digest=?''',
        (stamp, stamp, scope_digest),
    )
    row = conn.execute(
        'SELECT failure_count FROM auth_login_throttle WHERE scope_digest=?',
        (scope_digest,),
    ).fetchone()
    failures = int(row['failure_count'])
    blocked_until = None
    if failures >= threshold:
        exponent = max(0, failures - threshold)
        lock_seconds = min(LOGIN_LOCK_BASE_SECONDS * (2 ** exponent), LOGIN_LOCK_MAX_SECONDS)
        blocked_until = current + timedelta(seconds=lock_seconds)
        conn.execute(
            'UPDATE auth_login_throttle SET blocked_until=?,updated_at=? WHERE scope_digest=?',
            (_stamp(blocked_until), stamp, scope_digest),
        )
    return {
        'failure_count': failures,
        'blocked_until': _stamp(blocked_until) if blocked_until else None,
    }


def clear_login_failures(conn, scope_digest: str) -> None:
    conn.execute('DELETE FROM auth_login_throttle WHERE scope_digest=?', (scope_digest,))