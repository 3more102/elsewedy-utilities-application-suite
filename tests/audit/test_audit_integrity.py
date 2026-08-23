import sqlite3

from app.audit_store import (
    audit,
    reconstruct_audit_history,
    replay_verify_audit_chain,
    verify_audit_chain,
)


def audit_conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        '''CREATE TABLE audit_logs(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             user_id INTEGER NOT NULL,
             action TEXT NOT NULL,
             module TEXT NOT NULL,
             record_id TEXT NOT NULL,
             old_value TEXT DEFAULT '',
             new_value TEXT DEFAULT '',
             created_at TEXT NOT NULL,
             prev_hash TEXT DEFAULT '',
             audit_hash TEXT DEFAULT ''
           )'''
    )
    return conn


def test_audit_record_creation_and_actor_hash_evidence():
    conn = audit_conn()
    digest = audit(conn, 42, 'CREATE', 'Assets', 'AST-42', '', {'status': 'Operating'})
    row = conn.execute('SELECT * FROM audit_logs').fetchone()
    assert row['user_id'] == 42
    assert row['record_id'] == 'AST-42'
    assert row['prev_hash'] == ''
    assert row['audit_hash'] == digest
    assert len(digest) == 64


def test_audit_chain_integrity_replay_and_history_reconstruction():
    conn = audit_conn()
    hashes = [
        audit(conn, 1, 'CREATE', 'Work Management', 'WO-1', '', 'Draft'),
        audit(conn, 2, 'SUBMIT', 'Work Management', 'WO-1', 'Draft', 'Submitted'),
        audit(conn, 3, 'APPROVE', 'Work Management', 'WO-1', 'Submitted', 'Approved'),
    ]
    rows = conn.execute('SELECT prev_hash,audit_hash FROM audit_logs ORDER BY id').fetchall()
    assert rows[1]['prev_hash'] == hashes[0]
    assert rows[2]['prev_hash'] == hashes[1]
    assert verify_audit_chain(conn) == {
        'valid': True,
        'checked': 3,
        'first_invalid_id': None,
        'head_hash': hashes[2],
    }
    replay = replay_verify_audit_chain(conn)
    assert replay['valid'] is True and replay['replayed'] == 3
    history = reconstruct_audit_history(conn)
    assert [item['valid'] for item in history] == [True, True, True]
    assert history[-1]['expected_hash'] == hashes[-1]


def test_audit_tamper_detection_identifies_first_invalid_record():
    conn = audit_conn()
    audit(conn, 1, 'CREATE', 'Inventory', 'TX-1', '', '10')
    audit(conn, 1, 'UPDATE', 'Inventory', 'TX-1', '10', '8')
    second = conn.execute('SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1').fetchone()['id']
    conn.execute("UPDATE audit_logs SET new_value='999' WHERE id=?", (second,))
    result = verify_audit_chain(conn)
    assert result['valid'] is False
    assert result['first_invalid_id'] == second
    assert result['checked'] == 2
