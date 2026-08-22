"""PostgreSQL compatibility contract for EUAS' SQLite-shaped query layer.

The reference application uses sqlite qmark binds and allows direct cursor
iteration. psycopg uses pyformat-style binds, where a literal percent sign in
SQL must be escaped as ``%%`` whenever parameters are supplied.

Keeping these conversions centralized prevents endpoint-specific PostgreSQL
workarounds while EUAS transitions toward a dedicated migration/query layer.
"""
from __future__ import annotations

import re

from . import database


def _pg_sql(sql: str) -> str:
    """Translate EUAS qmark SQL to psycopg while preserving literal `%`."""
    converted = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
    # psycopg treats every percent sign in a parameterized query as part of its
    # placeholder syntax. EUAS SQL uses qmark binds, so existing percent signs
    # are literals (primarily LIKE wildcards) and must be doubled first.
    converted = converted.replace('%', '%%')
    return converted.replace('?', '%s')


def _pg_insert_or_ignore(sql: str) -> str:
    marker = 'INSERT OR IGNORE INTO'
    if marker not in sql.upper():
        return _pg_sql(sql)
    converted = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.I)
    converted = converted.replace('%', '%%').replace('?', '%s').rstrip().rstrip(';')
    return converted + ' ON CONFLICT DO NOTHING'


def _postgres_cursor_iter(cursor):
    """Expose sqlite-style direct iteration over PostgreSQL cursor rows."""
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row


def apply_postgres_compat() -> None:
    """Install compatibility functions on the database module exactly once."""
    database._pg_sql = _pg_sql
    database._pg_insert_or_ignore = _pg_insert_or_ignore
    if '__iter__' not in database.PostgresCursor.__dict__:
        database.PostgresCursor.__iter__ = _postgres_cursor_iter
