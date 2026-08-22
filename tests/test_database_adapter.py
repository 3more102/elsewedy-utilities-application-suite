import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import auth to install the application-wide PostgreSQL cursor compatibility
# contract before exercising the adapter directly.
import app.auth  # noqa: F401
from app.database import PostgresCursor, _pg_insert_or_ignore, _postgresize_schema


def test_postgres_sql_translation_contract():
    sql = _pg_insert_or_ignore('INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)')
    assert sql == 'INSERT INTO role_permissions(role_id,permission_id) VALUES(%s,%s) ON CONFLICT DO NOTHING'
    schema = _postgresize_schema('CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, value REAL);')
    assert 'SERIAL PRIMARY KEY' in schema
    assert 'DOUBLE PRECISION' in schema


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
