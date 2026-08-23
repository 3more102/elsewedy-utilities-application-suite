from __future__ import annotations

from core.configuration import APP_NAME, APP_VERSION, AUTOMATION_INTERVAL_MINUTES, DB_BACKEND, SCHEMA_VERSION
from core.database import db


def health_snapshot() -> dict:
    with db() as conn:
        conn.execute('SELECT 1').fetchone()
        row = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()
        schema = row[0] if row and row[0] is not None else 0
        last = conn.execute('SELECT run_no,status,finished_at FROM job_runs ORDER BY id DESC LIMIT 1').fetchone()
    return {
        'status': 'ok',
        'application': APP_NAME,
        'version': APP_VERSION,
        'database_backend': DB_BACKEND,
        'schema_version': schema,
        'automation_interval_minutes': AUTOMATION_INTERVAL_MINUTES,
        'last_automation_run': dict(last) if last else None,
    }


def readiness_snapshot() -> dict:
    with db() as conn:
        counts = {
            'users': conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'assets': conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0],
        }
    return {
        'status': 'ready',
        'database_backend': DB_BACKEND,
        'schema_version': SCHEMA_VERSION,
        'checks': counts,
    }
