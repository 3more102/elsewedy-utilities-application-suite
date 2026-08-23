import sqlite3

from apps.notifications import notify_once


def test_notification_intent_is_deduplicated_while_unread():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,role_code TEXT,title TEXT,message TEXT,severity TEXT,
        link_module TEXT,link_id TEXT,is_read INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL)''')
    assert notify_once(conn, 'Alert', 'first', 'Warning', 7, None, 'assets', 'A-1') is True
    assert notify_once(conn, 'Alert', 'second', 'Warning', 7, None, 'assets', 'A-1') is False
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    conn.execute('UPDATE notifications SET is_read=1')
    assert notify_once(conn, 'Alert', 'third', 'Warning', 7, None, 'assets', 'A-1') is True
