import sqlite3

from app.event_store import emit_event, process_outbox, rearm_outbox_event


def event_conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        '''CREATE TABLE event_outbox(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             event_no TEXT UNIQUE NOT NULL,
             event_type TEXT NOT NULL,
             aggregate_type TEXT NOT NULL,
             aggregate_id TEXT NOT NULL,
             payload_json TEXT NOT NULL,
             status TEXT NOT NULL DEFAULT 'Pending',
             attempts INTEGER NOT NULL DEFAULT 0,
             current_attempt_no INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             available_at TEXT NOT NULL DEFAULT '',
             processed_at TEXT,
             last_error TEXT DEFAULT '',
             correlation_id TEXT NOT NULL DEFAULT '',
             lease_owner TEXT,
             lease_expires_at TEXT,
             updated_at TEXT NOT NULL DEFAULT ''
           )'''
    )
    conn.execute(
        '''CREATE TABLE event_delivery_attempts(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             event_id INTEGER NOT NULL REFERENCES event_outbox(id) ON DELETE CASCADE,
             attempt_no INTEGER NOT NULL,
             worker_id TEXT NOT NULL,
             status TEXT NOT NULL,
             started_at TEXT NOT NULL,
             finished_at TEXT,
             error_message TEXT DEFAULT '',
             UNIQUE(event_id,attempt_no)
           )'''

    )
    return conn


def test_event_app_enqueues_skips_and_rearms_without_duplicate_logic():
    conn = event_conn()
    created = emit_event(conn, 'test.created', 'test', 'A-1', {'value': 1})
    row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (created['id'],)).fetchone()
    assert row['status'] == 'Pending' and row['attempts'] == 0

    result = process_outbox(
        conn,
        webhook_url='',
        webhook_secret='',
        max_attempts=3,
        app_version='test',
    )
    assert result == {'delivered': 0, 'failed': 0, 'dead_lettered': 0, 'skipped': 1, 'processed': 1}
    skipped = conn.execute('SELECT * FROM event_outbox WHERE id=?', (created['id'],)).fetchone()
    assert skipped['status'] == 'Skipped' and skipped['attempts'] == 1

    previous = rearm_outbox_event(conn, created['id'])
    assert previous['status'] == 'Skipped'
    rearmed = conn.execute('SELECT * FROM event_outbox WHERE id=?', (created['id'],)).fetchone()
    assert rearmed['status'] == 'Pending'
    assert rearmed['attempts'] == 0
    assert rearmed['processed_at'] is None
    assert rearmed['last_error'] == ''
