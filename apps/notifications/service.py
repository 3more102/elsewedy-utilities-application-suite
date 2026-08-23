from __future__ import annotations

from core.database import now


def notify(conn, title: str, message: str, severity: str = 'Info', user_id=None, role_code=None, module: str = '', record_id: str = '') -> dict:
    cur = conn.execute(
        '''INSERT INTO notifications(user_id,role_code,title,message,severity,link_module,link_id,created_at)
           VALUES(?,?,?,?,?,?,?,?)''',
        (user_id, role_code, title, message, severity, module, record_id, now()),
    )
    return {'id': cur.lastrowid, 'created': True}


def notify_once(conn, title: str, message: str, severity: str = 'Info', user_id=None, role_code=None, module: str = '', record_id: str = '') -> bool:
    existing = conn.execute(
        '''SELECT id FROM notifications WHERE title=? AND link_module=? AND link_id=? AND is_read=0
           AND ((user_id=? ) OR (user_id IS NULL AND ? IS NULL))
           AND ((role_code=? ) OR (role_code IS NULL AND ? IS NULL)) LIMIT 1''',
        (title, module, record_id, user_id, user_id, role_code, role_code),
    ).fetchone()
    if existing:
        return False
    notify(conn, title, message, severity, user_id, role_code, module, record_id)
    return True
