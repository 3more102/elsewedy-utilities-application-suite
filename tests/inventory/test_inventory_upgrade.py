import sqlite3

from apps.identity import hash_password
from core.configuration import SCHEMA_VERSION
from core.database import runtime as database_runtime


def test_schema22_inventory_transactions_upgrade_with_idempotency_columns(monkeypatch, tmp_path):
    path = tmp_path / 'schema22-inventory.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(
            '''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations(version,applied_at) VALUES(22,'2026-08-23T00:00:00');
            CREATE TABLE inventory_transactions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, tx_type TEXT NOT NULL,
              quantity REAL NOT NULL, from_warehouse_id INTEGER, to_warehouse_id INTEGER,
              work_order_id INTEGER, reference TEXT DEFAULT '', user_id INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO inventory_transactions(item_id,tx_type,quantity,reference,user_id,created_at)
            VALUES(7,'RECEIPT',2,'LEGACY-RECEIPT',1,'2026-08-23T00:00:00');
            '''
        )
    monkeypatch.setattr(database_runtime, 'DB_PATH', path)
    monkeypatch.setattr(database_runtime, 'DB_BACKEND', 'sqlite')
    database_runtime.init_db(hash_password)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(inventory_transactions)')}
        assert {'idempotency_key', 'operation_fingerprint'} <= columns
        legacy = conn.execute("SELECT * FROM inventory_transactions WHERE reference='LEGACY-RECEIPT'").fetchone()
        assert legacy['quantity'] == 2 and legacy['idempotency_key'] is None and legacy['operation_fingerprint'] == ''
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_inventory_transactions_idempotency'"
        ).fetchone()[0] == 1
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
