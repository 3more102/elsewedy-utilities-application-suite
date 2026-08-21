"""EUAS PostgreSQL connectivity preflight.

Usage:
    EUAS_DATABASE_URL=postgresql://user:pass@host:5432/euas python scripts/postgres_preflight.py
"""
from __future__ import annotations
import os
import sys

url = os.getenv('EUAS_DATABASE_URL', '').strip()
if not url.startswith(('postgresql://', 'postgres://')):
    print('FAIL: EUAS_DATABASE_URL is not set to a PostgreSQL URL.', file=sys.stderr)
    raise SystemExit(2)

try:
    import psycopg
except ImportError:
    print('FAIL: psycopg is not installed. Install requirements.txt in the target environment.', file=sys.stderr)
    raise SystemExit(3)

try:
    with psycopg.connect(url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database(), current_user, version()')
            db, user, version = cur.fetchone()
            print(f'PASS PostgreSQL preflight: database={db} user={user}')
            print(version)
except Exception as exc:
    print(f'FAIL PostgreSQL connectivity: {exc}', file=sys.stderr)
    raise SystemExit(4)
