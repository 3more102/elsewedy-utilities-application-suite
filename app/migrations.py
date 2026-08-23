from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Callable, Optional

from .config import SCHEMA_VERSION
from .database import now

BASELINE_VERSION = 9
POSTGRES_MIGRATION_LOCK_KEY = 0x455541530010
MIN_PRODUCTION_BOOTSTRAP_PASSWORD = 16
DEMO_DEFAULT_CREDENTIALS = {
    'omar': 'EUAS@2026',
    'seif': 'EUAS@2026',
    'planner': 'Planner@2026',
    'supervisor': 'Supervisor@2026',
    'tech1': 'Tech@2026',
    'tech2': 'Tech2@2026',
    'store': 'Store@2026',
    'proc': 'Proc@2026',
    'hse': 'HSE@2026',
    'exec': 'Viewer@2026',
}


class MigrationError(RuntimeError):
    """Raised when the persisted schema cannot be safely advanced."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[object], Optional[dict]]
    validate: Callable[[object, str], bool]
    repairable: bool = False


def _auth_v10_valid(conn, backend: str) -> bool:
    required = {
        'auth_sessions': {
            'id', 'user_id', 'token_digest', 'created_at', 'last_seen_at',
            'expires_at', 'revoked_at', 'client_label', 'user_agent',
        },
        'auth_login_throttle': {
            'scope_digest', 'failure_count', 'window_started_at',
            'last_failure_at', 'blocked_until', 'updated_at',
        },
    }
    if backend == 'sqlite':
        for table, columns in required.items():
            present = {
                str(row['name'] if hasattr(row, 'keys') else row[1])
                for row in conn.execute(f'PRAGMA table_info({table})').fetchall()
            }
            if not columns <= present:
                return False
        return True

    if backend == 'postgresql':
        for table, columns in required.items():
            rows = conn.execute(
                '''SELECT column_name FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name=?''',
                (table,),
            ).fetchall()
            present = {str(row['column_name']) for row in rows}
            if not columns <= present:
                return False
        return True

    raise MigrationError(f'unsupported database backend: {backend}')


def registered_migrations() -> tuple[Migration, ...]:
    from . import auth_store

    return (
        Migration(
            10,
            'auth_session_hardening',
            auth_store.ensure_auth_schema,
            _auth_v10_valid,
            repairable=True,
        ),
    )


def _applied_versions(conn) -> list[int]:
    try:
        rows = conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version'
        ).fetchall()
    except Exception as exc:
        raise MigrationError(
            'schema_migrations is unavailable; bootstrap the base EUAS schema first'
        ) from exc
    return [int(row['version'] if hasattr(row, 'keys') else row[0]) for row in rows]


def _acquire_migration_lock(conn, backend: str) -> None:
    if backend == 'postgresql':
        conn.execute(
            'SELECT pg_advisory_xact_lock(?)', (POSTGRES_MIGRATION_LOCK_KEY,)
        ).fetchone()
        return
    if backend == 'sqlite':
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception as exc:
            if 'within a transaction' not in str(exc).lower():
                raise
        return
    raise MigrationError(f'unsupported database backend: {backend}')


def migration_status(
    conn,
    *,
    backend: str,
    target_version: int = SCHEMA_VERSION,
) -> dict:
    applied = _applied_versions(conn)
    applied_set = set(applied)
    migrations = registered_migrations()
    registry = {migration.version: migration for migration in migrations}

    future = sorted(version for version in applied if version > target_version)
    pending = sorted(
        version
        for version in registry
        if BASELINE_VERSION < version <= target_version and version not in applied_set
    )
    invalid = sorted(
        migration.version
        for migration in migrations
        if migration.version in applied_set
        and migration.version <= target_version
        and not migration.validate(conn, backend)
    )
    unregistered = sorted(
        version
        for version in range(BASELINE_VERSION + 1, target_version + 1)
        if version not in registry and version not in applied_set
    )
    baseline_present = BASELINE_VERSION in applied_set
    current = max(applied, default=0)
    ready = bool(
        baseline_present
        and not future
        and not pending
        and not invalid
        and not unregistered
        and target_version in applied_set
    )
    return {
        'baseline_version': BASELINE_VERSION,
        'target_version': int(target_version),
        'current_version': current,
        'applied_versions': applied,
        'pending_versions': pending,
        'invalid_versions': invalid,
        'future_versions': future,
        'unregistered_versions': unregistered,
        'ready': ready,
    }


def run_pending_migrations(
    conn,
    *,
    backend: str,
    target_version: int = SCHEMA_VERSION,
) -> dict:
    _acquire_migration_lock(conn, backend)
    before = migration_status(conn, backend=backend, target_version=target_version)

    if BASELINE_VERSION not in set(before['applied_versions']):
        raise MigrationError(
            f'base schema v{BASELINE_VERSION} is not recorded; bootstrap is required'
        )
    if before['future_versions']:
        raise MigrationError(
            'database schema is newer than this application: '
            + ','.join(str(v) for v in before['future_versions'])
        )
    if before['unregistered_versions']:
        raise MigrationError(
            'migration registry is incomplete for versions: '
            + ','.join(str(v) for v in before['unregistered_versions'])
        )

    applied_now: list[int] = []
    repaired_now: list[int] = []
    skipped: list[int] = []
    details: dict[int, dict] = {}
    applied_set = set(before['applied_versions'])

    for migration in registered_migrations():
        if not (BASELINE_VERSION < migration.version <= target_version):
            continue

        already_recorded = migration.version in applied_set
        valid = migration.validate(conn, backend) if already_recorded else False
        if already_recorded and valid:
            skipped.append(migration.version)
            continue
        if already_recorded and not migration.repairable:
            raise MigrationError(
                f'migration v{migration.version} ({migration.name}) is recorded but invalid'
            )

        result = migration.apply(conn) or {}
        if not migration.validate(conn, backend):
            raise MigrationError(
                f'migration v{migration.version} ({migration.name}) failed validation'
            )
        details[migration.version] = dict(result)

        persisted = set(_applied_versions(conn))
        if already_recorded:
            repaired_now.append(migration.version)
        else:
            if migration.version not in persisted:
                conn.execute(
                    'INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',
                    (migration.version, now()),
                )
            applied_set.add(migration.version)
            applied_now.append(migration.version)

    after = migration_status(conn, backend=backend, target_version=target_version)
    if not after['ready']:
        raise MigrationError(f'migration run did not reach schema v{target_version}: {after}')

    return {
        'applied': applied_now,
        'repaired': repaired_now,
        'skipped': skipped,
        'details': details,
        'status': after,
    }


def _production_environment() -> bool:
    return os.getenv('EUAS_ENV', 'development').strip().lower() == 'production'


def _bootstrap_secret(required: bool) -> str:
    secret = os.getenv('EUAS_BOOTSTRAP_ADMIN_PASSWORD', '').strip()
    if not required and not secret:
        return ''
    if len(secret) < MIN_PRODUCTION_BOOTSTRAP_PASSWORD:
        raise MigrationError(
            'production bootstrap requires EUAS_BOOTSTRAP_ADMIN_PASSWORD '
            f'with at least {MIN_PRODUCTION_BOOTSTRAP_PASSWORD} characters'
        )
    if secret in set(DEMO_DEFAULT_CREDENTIALS.values()):
        raise MigrationError('production bootstrap password must not reuse a packaged demo password')
    return secret


def _database_has_users(database_module) -> bool:
    try:
        with database_module.db() as conn:
            return bool(int(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]))
    except Exception:
        return False


def _derived_seed_password(secret: str, discriminator: str) -> str:
    return hmac.new(
        secret.encode('utf-8'),
        f'euas-production-seed:{discriminator}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _secure_seed_hasher(hash_password, secret: str):
    defaults = set(DEMO_DEFAULT_CREDENTIALS.values())

    def secure_hash(password: str) -> str:
        if password in defaults:
            password = _derived_seed_password(secret, password)
        return hash_password(password)

    return secure_hash


def find_insecure_demo_users(conn, verify_password) -> list[str]:
    insecure: list[str] = []
    for username, packaged_password in DEMO_DEFAULT_CREDENTIALS.items():
        row = conn.execute(
            'SELECT password_hash,active FROM users WHERE username=?', (username,)
        ).fetchone()
        if row and int(row['active'] or 0) and verify_password(packaged_password, row['password_hash']):
            insecure.append(username)
    return insecure


def _rotate_insecure_demo_credentials(conn, hash_password, verify_password, secret: str) -> list[str]:
    rotated: list[str] = []
    stamp = now()
    for username, packaged_password in DEMO_DEFAULT_CREDENTIALS.items():
        row = conn.execute(
            'SELECT id,password_hash,active FROM users WHERE username=?', (username,)
        ).fetchone()
        if not row or not int(row['active'] or 0):
            continue
        if not verify_password(packaged_password, row['password_hash']):
            continue
        replacement = secret if username == 'omar' else _derived_seed_password(
            secret, f'{username}:{packaged_password}'
        )
        conn.execute(
            'UPDATE users SET password_hash=? WHERE id=?',
            (hash_password(replacement), int(row['id'])),
        )
        try:
            conn.execute(
                'UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',
                (stamp, int(row['id'])),
            )
        except Exception:
            pass
        conn.execute('DELETE FROM sessions WHERE user_id=?', (int(row['id']),))
        rotated.append(username)
    return rotated


def initialize_database(hash_password) -> dict:
    """Bootstrap the historical v9 base, then migrate to the app contract.

    Production bootstraps never commit the packaged demo passwords. A fresh
    production database requires ``EUAS_BOOTSTRAP_ADMIN_PASSWORD``; all packaged
    seed passwords are replaced before the base seed transaction commits, then
    the ``omar`` administrator is assigned the operator-provided secret. Existing
    production databases are scanned for packaged demo credentials and those
    credentials are rotated (with sessions revoked) when the secret is supplied.
    """
    from . import database as database_module

    production = _production_environment()
    had_users = _database_has_users(database_module) if production else False
    secret = _bootstrap_secret(required=production and not had_users) if production else ''
    bootstrap_hasher = _secure_seed_hasher(hash_password, secret) if production and secret else hash_password

    target_version = int(database_module.SCHEMA_VERSION)
    database_module.SCHEMA_VERSION = BASELINE_VERSION
    try:
        database_module.init_db(bootstrap_hasher)
    finally:
        database_module.SCHEMA_VERSION = target_version

    with database_module.db() as conn:
        result = run_pending_migrations(
            conn,
            backend=database_module.DB_BACKEND,
            target_version=target_version,
        )

        credential_hardening = {
            'production': production,
            'fresh_admin_initialized': False,
            'rotated_users': [],
        }
        if production:
            from .auth import verify_password

            insecure = find_insecure_demo_users(conn, verify_password)
            if insecure and not secret:
                raise MigrationError(
                    'packaged demo credentials remain active for: '
                    + ','.join(insecure)
                    + '; set EUAS_BOOTSTRAP_ADMIN_PASSWORD to rotate them before startup'
                )
            if insecure:
                credential_hardening['rotated_users'] = _rotate_insecure_demo_credentials(
                    conn, hash_password, verify_password, secret
                )
            if not had_users:
                admin = conn.execute(
                    "SELECT id FROM users WHERE username='omar' AND active=1"
                ).fetchone()
                if not admin:
                    raise MigrationError('production bootstrap administrator was not created')
                conn.execute(
                    'UPDATE users SET password_hash=? WHERE id=?',
                    (hash_password(secret), int(admin['id'])),
                )
                credential_hardening['fresh_admin_initialized'] = True

            remaining = find_insecure_demo_users(conn, verify_password)
            if remaining:
                raise MigrationError(
                    'packaged demo credentials remain active after hardening: '
                    + ','.join(remaining)
                )

        result['credential_hardening'] = credential_hardening
        return result
