from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .config import SCHEMA_VERSION
from .database import now

BASELINE_VERSION = 9
POSTGRES_MIGRATION_LOCK_KEY = 0x455541530010


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
    # Import lazily to keep auth_store free to call the runner during startup.
    from . import auth_store

    return (
        Migration(
            10,
            'auth_session_hardening',
            auth_store.apply_auth_schema_migration,
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
            # A caller may already have opened the transaction. In that case the
            # enclosing transaction is the serialization boundary for this run.
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
    """Apply registered migrations under one database transaction and lock.

    PostgreSQL uses a transaction-scoped advisory lock. SQLite upgrades take an
    immediate write transaction before the migration ledger is inspected. A
    persisted migration marker is not trusted blindly: each registered version
    can validate its structural contract, and explicitly repairable migrations
    may heal historical pre-claimed markers before deployment continues.
    """
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

        if already_recorded:
            repaired_now.append(migration.version)
        else:
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
