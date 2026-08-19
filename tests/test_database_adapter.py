import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import _pg_insert_or_ignore, _postgresize_schema


def test_postgres_sql_translation_contract():
    sql = _pg_insert_or_ignore('INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)')
    assert sql == 'INSERT INTO role_permissions(role_id,permission_id) VALUES(%s,%s) ON CONFLICT DO NOTHING'
    schema = _postgresize_schema('CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, value REAL);')
    assert 'SERIAL PRIMARY KEY' in schema
    assert 'DOUBLE PRECISION' in schema
