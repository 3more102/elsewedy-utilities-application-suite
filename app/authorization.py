from __future__ import annotations

from collections.abc import Iterable

# Additive capability catalog for sensitive operations. Existing role checks
# remain the compatibility boundary; these grants are an additional narrowing
# control and therefore cannot broaden access beyond the route's legacy role
# whitelist.
PERMISSION_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    'admin.permissions.manage': (
        'Manage role permission grants',
        ('admin',),
    ),
    'admin.users.manage': (
        'Manage user accounts and activation state',
        ('admin',),
    ),
    'admin.backup.download': (
        'Download administrative database backups',
        ('admin',),
    ),
    'operations.automation.run': (
        'Run the automation engine',
        ('admin', 'maintenance_manager'),
    ),
    'operations.automation.read': (
        'Read automation status and run history',
        ('admin', 'maintenance_manager', 'executive'),
    ),
    'observability.metrics.read': (
        'Read application observability metrics',
        ('admin', 'maintenance_manager', 'executive'),
    ),
    'audit.read': (
        'Read the audit trail',
        ('admin', 'maintenance_manager', 'executive'),
    ),
    'audit.export': (
        'Export audit records',
        ('admin', 'maintenance_manager', 'executive'),
    ),
}

# This map is intentionally limited to high-impact routes whose existing role
# semantics are stable and covered by regression tests. FastAPI's matched route
# template is used for parameterized paths so a single entry covers every user.
ROUTE_PERMISSION_OVERLAY: dict[tuple[str, str], str] = {
    ('POST', '/api/admin/users'): 'admin.users.manage',
    ('PATCH', '/api/admin/users/{user_id}/status'): 'admin.users.manage',
    ('GET', '/api/admin/backup'): 'admin.backup.download',
    ('POST', '/api/automation/run'): 'operations.automation.run',
    ('GET', '/api/automation/status'): 'operations.automation.read',
    ('GET', '/api/automation/runs'): 'operations.automation.read',
    ('GET', '/api/metrics'): 'observability.metrics.read',
    ('GET', '/api/audit'): 'audit.read',
    ('GET', '/api/exports/audit.csv'): 'audit.export',
}

PROTECTED_ADMIN_PERMISSION = 'admin.permissions.manage'


def ensure_permission_catalog(conn) -> dict:
    """Seed new capabilities exactly once without undoing later admin changes.

    A capability's default role grants are created only when that capability is
    first inserted. Once present, future startup runs leave its grants untouched
    so an administrator can deliberately revoke or reassign them and have that
    decision survive restarts.
    """
    created_permissions = 0
    created_grants = 0
    role_ids = {
        row['code']: row['id']
        for row in conn.execute('SELECT id,code FROM roles').fetchall()
    }

    for code, (name, default_roles) in PERMISSION_CATALOG.items():
        cur = conn.execute(
            'INSERT OR IGNORE INTO permissions(code,name) VALUES(?,?)',
            (code, name),
        )
        inserted = bool(int(cur.rowcount or 0))
        if not inserted:
            continue
        created_permissions += 1
        permission = conn.execute(
            'SELECT id FROM permissions WHERE code=?', (code,)
        ).fetchone()
        if not permission:
            raise RuntimeError(f'permission catalog insert failed for {code}')
        for role_code in default_roles:
            role_id = role_ids.get(role_code)
            if role_id is None:
                continue
            grant = conn.execute(
                'INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)',
                (role_id, permission['id']),
            )
            created_grants += int(bool(int(grant.rowcount or 0)))

    return {
        'permissions_created': created_permissions,
        'grants_created': created_grants,
    }


def permission_codes_for_user(conn, user_id: int) -> list[str]:
    rows = conn.execute(
        '''SELECT p.code
           FROM users u
           JOIN role_permissions rp ON rp.role_id=u.role_id
           JOIN permissions p ON p.id=rp.permission_id
           WHERE u.id=?
           ORDER BY p.code''',
        (user_id,),
    ).fetchall()
    return [str(row['code']) for row in rows]


def permission_codes_for_role(conn, role_code: str) -> list[str]:
    rows = conn.execute(
        '''SELECT p.code
           FROM roles r
           JOIN role_permissions rp ON rp.role_id=r.id
           JOIN permissions p ON p.id=rp.permission_id
           WHERE r.code=?
           ORDER BY p.code''',
        (role_code,),
    ).fetchall()
    return [str(row['code']) for row in rows]


def has_permissions(
    granted: Iterable[str], required: Iterable[str], *, require_all: bool = True
) -> bool:
    granted_set = set(granted)
    required_set = set(required)
    if not required_set:
        return True
    if require_all:
        return required_set <= granted_set
    return bool(granted_set.intersection(required_set))


def permission_for_route(method: str, route_path: str | None) -> str | None:
    if not route_path:
        return None
    return ROUTE_PERMISSION_OVERLAY.get((str(method).upper(), str(route_path)))


def authorization_snapshot(conn) -> dict:
    permissions = [
        {'code': row['code'], 'name': row['name']}
        for row in conn.execute('SELECT code,name FROM permissions ORDER BY code').fetchall()
    ]
    roles = []
    for role in conn.execute('SELECT code,name FROM roles ORDER BY code').fetchall():
        roles.append(
            {
                'code': role['code'],
                'name': role['name'],
                'permissions': permission_codes_for_role(conn, role['code']),
            }
        )
    return {'permissions': permissions, 'roles': roles}


def replace_role_permissions(conn, role_code: str, permission_codes: Iterable[str]) -> list[str]:
    role = conn.execute(
        'SELECT id,code FROM roles WHERE code=?', (role_code,)
    ).fetchone()
    if not role:
        raise KeyError('role_not_found')

    requested = sorted({str(code).strip() for code in permission_codes if str(code).strip()})
    if role_code == 'admin' and PROTECTED_ADMIN_PERMISSION not in requested:
        raise ValueError('protected_admin_permission')

    known_rows = conn.execute('SELECT id,code FROM permissions').fetchall()
    known = {str(row['code']): int(row['id']) for row in known_rows}
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise ValueError('unknown_permissions:' + ','.join(unknown))

    conn.execute('DELETE FROM role_permissions WHERE role_id=?', (role['id'],))
    for code in requested:
        conn.execute(
            'INSERT INTO role_permissions(role_id,permission_id) VALUES(?,?)',
            (role['id'], known[code]),
        )
    return requested
