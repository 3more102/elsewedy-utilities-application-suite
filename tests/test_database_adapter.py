import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import auth to install the application-wide PostgreSQL compatibility contract
# before exercising adapter translation and cursor behavior directly.
import app.auth  # noqa: F401
from app.database import PostgresConnection, PostgresCursor, _pg_insert_or_ignore, _pg_sql, _postgresize_schema


def test_postgres_sql_translation_contract():
    sql = _pg_insert_or_ignore('INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)')
    assert sql == 'INSERT INTO role_permissions(role_id,permission_id) VALUES(%s,%s) ON CONFLICT DO NOTHING'
    schema = _postgresize_schema('CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, value REAL);')
    assert 'SERIAL PRIMARY KEY' in schema
    assert 'DOUBLE PRECISION' in schema


def test_postgres_translation_escapes_literal_percent_for_psycopg():
    sql = _pg_sql("SELECT id FROM work_orders WHERE work_type LIKE 'Corrective%' AND asset_id=?")
    assert sql == "SELECT id FROM work_orders WHERE work_type LIKE 'Corrective%%' AND asset_id=%s"
    ignored = _pg_insert_or_ignore("INSERT OR IGNORE INTO demo(name,pattern) VALUES(?, 'PM%')")
    assert ignored == "INSERT INTO demo(name,pattern) VALUES(%s, 'PM%%') ON CONFLICT DO NOTHING"


def test_postgres_translation_types_standalone_null_checks():
    sql = _pg_sql(
        'SELECT id FROM notifications WHERE ((user_id=?) OR (user_id IS NULL AND ? IS NULL)) '
        'AND ((role_code=?) OR (role_code IS NULL AND ? IS NULL))'
    )
    assert sql == (
        'SELECT id FROM notifications WHERE ((user_id=%s) OR '
        '(user_id IS NULL AND CAST(%s AS TEXT) IS NULL)) AND '
        '((role_code=%s) OR (role_code IS NULL AND CAST(%s AS TEXT) IS NULL))'
    )


def test_postgres_generated_id_is_statement_local_not_session_lastval():
    class RawCursor:
        rowcount = 1
        description = None

        def __init__(self, raw):
            self.raw = raw
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, args=()):
            self.raw.statements.append((sql, tuple(args or ())))
            if 'FROM information_schema.columns' in sql:
                table = args[0]
                if table in {'pg_ci_parent', 'pg_ci_audit'}:
                    self.result = (f"nextval('{table}_id_seq'::regclass)", 'NO')
                else:
                    self.result = None
            elif sql.startswith('INSERT INTO pg_ci_parent'):
                assert sql.endswith('RETURNING id')
                self.result = (41,)
            elif sql.startswith('INSERT INTO pg_ci_audit'):
                assert sql.endswith('RETURNING id')
                self.result = (900,)
            else:
                self.result = None
            return self

        def fetchone(self):
            value, self.result = self.result, None
            return value

        def fetchall(self):
            return []

    class RawConnection:
        def __init__(self):
            self.statements = []

        def cursor(self):
            return RawCursor(self)

    raw = RawConnection()
    conn = PostgresConnection(raw)
    parent = conn.execute('INSERT INTO pg_ci_parent(name) VALUES(?)', ('parent',))
    audit = conn.execute('INSERT INTO pg_ci_audit(action) VALUES(?)', ('CREATE',))
    bridge = conn.execute('INSERT INTO pg_ci_bridge(left_id,right_id) VALUES(?,?)', (1, 2))

    assert parent.lastrowid == 41
    assert audit.lastrowid == 900
    assert parent.lastrowid == 41
    assert bridge.lastrowid is None


def test_postgres_cursor_supports_sqlite_style_iteration():
    class Column:
        def __init__(self, name):
            self.name = name

    class RawCursor:
        rowcount = 2
        description = [Column('code'), Column('id')]

        def __init__(self):
            self.rows = [('admin', 1), ('planner', 2)]

        def fetchone(self):
            return self.rows.pop(0) if self.rows else None

    cursor = PostgresCursor(RawCursor(), None)
    rows = list(cursor)
    assert [row['code'] for row in rows] == ['admin', 'planner']
    assert rows[0][1] == 1
