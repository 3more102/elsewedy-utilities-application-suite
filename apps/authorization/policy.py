from __future__ import annotations

from datetime import datetime

from fastapi import Depends, HTTPException

from core.database import db
from apps.identity import current_user


def require_roles(*roles):
    def check(user=Depends(current_user)):
        if user['role'] not in roles:
            raise HTTPException(403, 'Insufficient permissions')
        return user
    return check


def _permission_allowed(conn, user_id: int, role_code: str, permission_code: str) -> tuple[bool, str]:
    stamp = datetime.now().isoformat(timespec='seconds')
    override = conn.execute(
        '''SELECT o.effect FROM user_permission_overrides o
           JOIN permissions p ON p.id=o.permission_id
           WHERE o.user_id=? AND p.code=?
             AND (o.expires_at IS NULL OR o.expires_at='' OR o.expires_at>?)
           LIMIT 1''',
        (user_id, permission_code, stamp),
    ).fetchone()
    if override:
        return override['effect'] == 'Allow', f"user_{override['effect'].lower()}"
    granted = conn.execute(
        '''SELECT 1 FROM role_permissions rp
           JOIN roles r ON r.id=rp.role_id
           JOIN permissions p ON p.id=rp.permission_id
           WHERE r.code=? AND p.code=? LIMIT 1''',
        (role_code, permission_code),
    ).fetchone()
    return bool(granted), 'role_grant' if granted else 'not_granted'


def permission_allowed(conn, user_id: int, role_code: str, permission_code: str) -> bool:
    exists = conn.execute('SELECT 1 FROM permissions WHERE code=?', (permission_code,)).fetchone()
    if not exists:
        return False
    allowed, _ = _permission_allowed(conn, int(user_id), role_code, permission_code)
    return allowed


def user_has_permission(conn, user_id: int, permission_code: str) -> bool:
    row = conn.execute('''SELECT u.id,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.active=1''', (user_id,)).fetchone()
    return bool(row) and permission_allowed(conn, int(user_id), row['role'], permission_code)


def has_permission(user: dict, permission_code: str) -> bool:
    with db() as conn:
        return permission_allowed(conn, int(user['id']), user['role'], permission_code)


def effective_permissions(user: dict) -> list[dict]:
    with db() as conn:
        stamp = datetime.now().isoformat(timespec='seconds')
        result = []
        for permission in conn.execute(
            'SELECT id,code,name,category,risk_level,description FROM permissions ORDER BY category,name'
        ).fetchall():
            allowed, source = _permission_allowed(
                conn, int(user['id']), user['role'], permission['code']
            )
            override = conn.execute(
                '''SELECT effect,reason,expires_at FROM user_permission_overrides
                   WHERE user_id=? AND permission_id=?
                     AND (expires_at IS NULL OR expires_at='' OR expires_at>?)''',
                (user['id'], permission['id'], stamp),
            ).fetchone()
            item = dict(permission)
            item.update(
                {
                    'allowed': allowed,
                    'source': source,
                    'override': dict(override) if override else None,
                }
            )
            result.append(item)
        return result


def require_permission(permission_code: str, *legacy_roles):
    """Permission-aware guard with a narrow legacy fallback for pre-v4.6 databases."""
    def check(user=Depends(current_user)):
        with db() as conn:
            exists = conn.execute('SELECT 1 FROM permissions WHERE code=?', (permission_code,)).fetchone()
            if exists:
                allowed, _ = _permission_allowed(
                    conn, int(user['id']), user['role'], permission_code
                )
                if not allowed:
                    raise HTTPException(403, f'Missing permission: {permission_code}')
                return user
        if legacy_roles and user['role'] in legacy_roles:
            return user
        raise HTTPException(403, f'Missing permission: {permission_code}')
    return check
