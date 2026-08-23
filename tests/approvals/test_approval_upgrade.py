import sqlite3

from apps.identity import hash_password
from core.configuration import SCHEMA_VERSION
from core.database import runtime as database_runtime


def test_schema21_approval_records_upgrade_in_place_with_snapshot_and_delegation_scope(monkeypatch, tmp_path):
    path = tmp_path / 'schema21-approvals.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(
            '''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations(version,applied_at) VALUES(21,'2026-08-23T00:00:00');
            CREATE TABLE approval_requests(
              id INTEGER PRIMARY KEY AUTOINCREMENT, approval_no TEXT UNIQUE NOT NULL, module TEXT NOT NULL, record_type TEXT NOT NULL,
              record_id INTEGER NOT NULL, record_code TEXT NOT NULL, title TEXT NOT NULL, requested_by INTEGER NOT NULL,
              assigned_role TEXT, assigned_user_id INTEGER, status TEXT NOT NULL DEFAULT 'Pending', requested_at TEXT NOT NULL,
              decided_at TEXT, decided_by INTEGER, comments TEXT DEFAULT ''
            );
            INSERT INTO approval_requests(approval_no,module,record_type,record_id,record_code,title,requested_by,status,requested_at)
            VALUES('APR-UPGRADE','Work Management','work_order',99,'WO-UPGRADE','Preserve me',1,'Pending','2026-08-23T00:00:00');
            CREATE TABLE approval_delegations(
              id INTEGER PRIMARY KEY AUTOINCREMENT, delegator_user_id INTEGER NOT NULL, delegate_user_id INTEGER NOT NULL,
              module TEXT NOT NULL DEFAULT '*', start_at TEXT NOT NULL, end_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO approval_delegations(delegator_user_id,delegate_user_id,module,start_at,end_at,active,created_by,created_at)
            VALUES(1,2,'Work Management','2026-08-23T00:00:00','2026-08-24T00:00:00',1,1,'2026-08-23T00:00:00');
            '''
        )
    monkeypatch.setattr(database_runtime, 'DB_PATH', path)
    monkeypatch.setattr(database_runtime, 'DB_BACKEND', 'sqlite')
    database_runtime.init_db(hash_password)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        approval_cols = {row['name'] for row in conn.execute('PRAGMA table_info(approval_requests)')}
        delegation_cols = {row['name'] for row in conn.execute('PRAGMA table_info(approval_delegations)')}
        assert {'request_snapshot_json', 'request_snapshot_hash', 'request_resource_version', 'correlation_id'} <= approval_cols
        assert {'record_type', 'resource_id', 'reason', 'revoked_at', 'revoked_by'} <= delegation_cols
        preserved = conn.execute("SELECT * FROM approval_requests WHERE approval_no='APR-UPGRADE'").fetchone()
        assert preserved['title'] == 'Preserve me' and preserved['status'] == 'Pending'
        delegation = conn.execute('SELECT * FROM approval_delegations WHERE id=1').fetchone()
        assert delegation['record_type'] == '*' and delegation['resource_id'] == 0 and delegation['revoked_at'] is None
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='approval_evidence_events'").fetchone()[0] == 1
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
