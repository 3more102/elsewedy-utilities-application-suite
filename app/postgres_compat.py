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

_SERIAL_ID_CACHE: dict[str, bool] = {}


def _type_null_check_binds(sql: str) -> str:
    """Give standalone NULL-test parameters an explicit PostgreSQL type.

    PostgreSQL cannot infer the type of an untyped NULL parameter when the
    parameter appears only in ``%s IS NULL``. Casting that parameter to TEXT is
    semantics-preserving because the expression tests nullness only; the value
    is never compared as text. This also keeps the original placeholder count
    and argument ordering intact.
    """
    return re.sub(r'%s\s+IS\s+NULL', 'CAST(%s AS TEXT) IS NULL', sql, flags=re.I)


def _pg_sql(sql: str) -> str:
    """Translate EUAS qmark SQL to psycopg while preserving literal `%`."""
    converted = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
    # psycopg treats every percent sign in a parameterized query as part of its
    # placeholder syntax. EUAS SQL uses qmark binds, so existing percent signs
    # are literals (primarily LIKE wildcards) and must be doubled first.
    converted = converted.replace('%', '%%').replace('?', '%s')
    return _type_null_check_binds(converted)


def _pg_insert_or_ignore(sql: str) -> str:
    marker = 'INSERT OR IGNORE INTO'
    if marker not in sql.upper():
        return _pg_sql(sql)
    converted = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.I)
    converted = converted.replace('%', '%%').replace('?', '%s').rstrip().rstrip(';')
    converted = _type_null_check_binds(converted)
    return converted + ' ON CONFLICT DO NOTHING'


def _postgres_cursor_iter(cursor):
    """Expose sqlite-style direct iteration over PostgreSQL cursor rows."""
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row


def _table_has_serial_id(raw, table: str) -> bool:
    cached = _SERIAL_ID_CACHE.get(table)
    if cached is not None:
        return cached

    # information_schema is safe for tables that intentionally have no `id`
    # column (for example schema_migrations and composite-key bridge tables).
    # pg_get_serial_sequence(table, 'id') raises when that column is absent.
    table_name = table.rsplit('.', 1)[-1]
    with raw.cursor() as probe:
        probe.execute(
            """SELECT column_default,is_identity
               FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=%s AND column_name='id'""",
            (table_name,),
        )
        row = probe.fetchone()
    result = bool(row and ((row[0] and 'nextval(' in str(row[0])) or str(row[1]).upper() == 'YES'))
    _SERIAL_ID_CACHE[table] = result
    return result


def _postgres_execute(connection, sql, args=()):
    """Execute SQL and capture generated IDs from the statement that made them.

    The old adapter implemented ``lastrowid`` lazily with PostgreSQL LASTVAL().
    That is session-global: if application code inserted an audit/event row
    before reading an earlier cursor's ``lastrowid``, the cursor returned the
    later table's sequence value. EUAS does exactly that in several endpoints.

    For INSERTs into tables whose ``id`` column owns a serial/identity sequence,
    append ``RETURNING id`` and snapshot the row immediately. This is
    transaction- and concurrency-safe, preserves ON CONFLICT DO NOTHING
    semantics, and makes the cursor behave like sqlite's statement-local
    ``lastrowid``.
    """
    translated = _pg_insert_or_ignore(sql)
    insert = re.match(r'^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_.]*)\b', translated, flags=re.I)
    return_id = bool(insert and _table_has_serial_id(connection.raw, insert.group(1)))
    if return_id:
        translated = translated.rstrip().rstrip(';') + ' RETURNING id'

    cur = connection.raw.cursor()
    cur.execute(translated, tuple(args or ()))
    generated_id = None
    if return_id:
        row = cur.fetchone()
        generated_id = row[0] if row else None

    wrapped = database.PostgresCursor(cur, connection.raw)
    wrapped._lastrowid = generated_id
    return wrapped


def _statement_local_lastrowid(cursor):
    """Return only the ID captured from this cursor's own INSERT statement."""
    return cursor._lastrowid


def apply_postgres_compat() -> None:
    """Install compatibility functions on the database module exactly once."""
    database._pg_sql = _pg_sql
    database._pg_insert_or_ignore = _pg_insert_or_ignore
    database.PostgresConnection.execute = _postgres_execute
    database.PostgresCursor.lastrowid = property(_statement_local_lastrowid)
    if '__iter__' not in database.PostgresCursor.__dict__:
        database.PostgresCursor.__iter__ = _postgres_cursor_iter
