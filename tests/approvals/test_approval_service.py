import sqlite3

from apps.approvals import create_approval, resolve_approval


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE approval_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,approval_no TEXT UNIQUE NOT NULL,module TEXT NOT NULL,record_type TEXT NOT NULL,
        record_id INTEGER NOT NULL,record_code TEXT NOT NULL,title TEXT NOT NULL,requested_by INTEGER NOT NULL,assigned_role TEXT,
        assigned_user_id INTEGER,status TEXT NOT NULL,requested_at TEXT NOT NULL,decided_at TEXT,decided_by INTEGER,comments TEXT DEFAULT '',
        request_snapshot_json TEXT NOT NULL DEFAULT '',request_snapshot_hash TEXT NOT NULL DEFAULT '',request_resource_version TEXT NOT NULL DEFAULT '',correlation_id TEXT NOT NULL DEFAULT '');
      CREATE TABLE work_orders(id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT,title TEXT);
      INSERT INTO work_orders(id,status,updated_at,title) VALUES(5,'Submitted','2026-08-23T10:00:00','WO-5');
      CREATE TABLE approval_evidence_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,evidence_no TEXT UNIQUE NOT NULL,approval_id INTEGER,delegation_id INTEGER,event_type TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL,effective_actor_user_id INTEGER NOT NULL,decision TEXT DEFAULT '',resource_type TEXT DEFAULT '',resource_id INTEGER,
        resource_fingerprint TEXT DEFAULT '',correlation_id TEXT NOT NULL,payload_json TEXT NOT NULL,prev_hash TEXT DEFAULT '',evidence_hash TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL);
      CREATE TABLE notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,role_code TEXT,title TEXT,message TEXT,severity TEXT,
        link_module TEXT,link_id TEXT,is_read INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
    ''')
    return conn


def test_approval_request_is_idempotent_and_resolvable():
    conn = _conn()
    first = create_approval(conn, 'Work Management', 'work_order', 5, 'WO-5', 'Approve WO-5', 1, assigned_role='maintenance_manager')
    replay = create_approval(conn, 'Work Management', 'work_order', 5, 'WO-5', 'Approve WO-5 again', 1, assigned_role='maintenance_manager')
    assert replay['approval_no'] == first['approval_no']
    assert conn.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0] == 1
    resolved = resolve_approval(conn, 'Work Management', 'work_order', 5, 'approve', 2, 'reviewed')
    assert resolved['status'] == 'Approved'
    row = conn.execute('SELECT * FROM approval_requests WHERE id=?', (first['id'],)).fetchone()
    assert row['status'] == 'Approved' and row['decided_by'] == 2 and row['comments'] == 'reviewed'
