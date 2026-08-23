import sqlite3

from apps.identity import hash_password
from core.database import runtime as database_runtime
from core.configuration import SCHEMA_VERSION


def test_schema20_outbox_upgrades_in_place_with_dispatch_columns(monkeypatch, tmp_path):
    path = tmp_path / 'schema20.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(
            '''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations(version,applied_at) VALUES(20,'2026-08-23T00:00:00');
            CREATE TABLE event_outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_no TEXT UNIQUE NOT NULL,
              event_type TEXT NOT NULL,
              aggregate_type TEXT NOT NULL,
              aggregate_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'Pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              processed_at TEXT,
              last_error TEXT DEFAULT ''
            );
            INSERT INTO event_outbox(event_no,event_type,aggregate_type,aggregate_id,payload_json,status,attempts,created_at)
            VALUES('EVT-UPGRADE','test.upgrade','test','A-1','{}','Pending',0,'2026-08-23T00:00:00');
            '''
        )
    monkeypatch.setattr(database_runtime, 'DB_PATH', path)
    monkeypatch.setattr(database_runtime, 'DB_BACKEND', 'sqlite')
    database_runtime.init_db(hash_password)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(event_outbox)')}
        assert {'current_attempt_no', 'available_at', 'correlation_id', 'lease_owner', 'lease_expires_at', 'updated_at'} <= columns
        row = conn.execute("SELECT * FROM event_outbox WHERE event_no='EVT-UPGRADE'").fetchone()
        assert row['status'] == 'Pending' and row['available_at'] == row['created_at']
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='event_delivery_attempts'").fetchone()[0] == 1
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
