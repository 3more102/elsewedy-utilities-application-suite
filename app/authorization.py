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
    'analytics.executive.read': (
        'Read executive utilities KPI dashboards',
        (
            'admin', 'maintenance_manager', 'executive',
            'asset_manager', 'planner', 'supervisor',
        ),
    ),
    'sites.customers.manage': (
        'Configure per-site customer population for reliability indices',
        ('admin',),
    ),
    'assets.create': (
        'Create assets',
        ('admin', 'asset_manager', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'assets.update': (
        'Update asset records',
        ('admin', 'asset_manager', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'assets.delete': (
        'Delete unreferenced assets',
        ('admin', 'asset_manager'),
    ),
    'assets.health.recalculate': (
        'Recalculate asset health portfolio scores',
        ('admin', 'asset_manager', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'assets.field.update': (
        'Update asset field condition and meter values',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'assets.meter.reading.write': (
        'Post asset meter readings',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'work.create': (
        'Create work orders',
        ('admin', 'asset_manager', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'work.update': (
        'Update work orders',
        ('admin', 'asset_manager', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'work.transition': (
        'Execute work-order workflow transitions',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'work.material.plan': (
        'Plan work-order material requirements',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'storekeeper'),
    ),
    'work.material.reserve': (
        'Reserve inventory for work orders',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'storekeeper'),
    ),
    'work.craft.plan': (
        'Plan craft requirements for work orders',
        ('admin', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'work.labor.post': (
        'Post labor against work orders',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'work.material.issue': (
        'Issue materials against work orders',
        ('admin', 'maintenance_manager', 'planner', 'storekeeper', 'technician'),
    ),
    'work.notes.write': (
        'Add work-order notes',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'work.tasks.manage': (
        'Update work-order task completion state',
        ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
    ),
    'work.dispatch': (
        'Dispatch technicians to work orders',
        ('admin', 'maintenance_manager', 'planner', 'supervisor'),
    ),
    'inventory.create': (
        'Create inventory items',
        ('admin', 'storekeeper', 'maintenance_manager'),
    ),
    'inventory.transaction.post': (
        'Post quantity-changing inventory transactions',
        ('admin', 'maintenance_manager', 'planner', 'storekeeper', 'technician'),
    ),
    'inventory.reorder.scan': (
        'Run the inventory reorder scan',
        ('admin', 'storekeeper', 'maintenance_manager', 'procurement'),
    ),
    'procurement.requisition.create': (
        'Create purchase requisitions',
        ('admin', 'storekeeper', 'maintenance_manager', 'procurement', 'planner'),
    ),
    'procurement.requisition.submit': (
        'Submit purchase requisitions for approval',
        ('admin', 'storekeeper', 'maintenance_manager', 'procurement', 'planner'),
    ),
    'procurement.requisition.approve': (
        'Approve submitted purchase requisitions',
        ('admin', 'maintenance_manager', 'procurement'),
    ),
    'procurement.quotation.create': (
        'Record procurement quotations',
        ('admin', 'maintenance_manager', 'procurement'),
    ),
    'procurement.purchase_order.create': (
        'Create purchase orders from approved requisitions',
        ('admin', 'maintenance_manager', 'procurement'),
    ),
    'procurement.purchase_order.receive': (
        'Receive purchase orders into inventory',
        ('admin', 'procurement', 'storekeeper'),
    ),
    'hse.incident.create': (
        'Create HSE incident records',
        ('admin', 'hse', 'maintenance_manager'),
    ),
    'hse.incident.update': (
        'Update HSE incident records and state',
        ('admin', 'hse', 'maintenance_manager'),
    ),
    'projects.create': (
        'Create projects',
        ('admin', 'project_manager', 'maintenance_manager'),
    ),
    'projects.tasks.create': (
        'Create project tasks',
        ('admin', 'project_manager', 'maintenance_manager'),
    ),
    'projects.tasks.update': (
        'Update project tasks and progress',
        ('admin', 'project_manager', 'maintenance_manager'),
    ),
    'sla.policy.manage': (
        'Update service-level policy thresholds and activation',
        ('admin', 'maintenance_manager'),
    ),
    'governance.retention.manage': (
        'Update data-retention governance policies',
        ('admin',),
    ),
    'integrations.outbox.retry': (
        'Retry failed integration outbox events',
        ('admin', 'maintenance_manager'),
    ),
}

# FastAPI's matched route template is used for parameterized paths. Each entry
# adds a capability requirement on top of the route's historical role check.
ROUTE_PERMISSION_OVERLAY: dict[tuple[str, str], str] = {
    ('POST', '/api/admin/users'): 'admin.users.manage',
    ('PATCH', '/api/admin/users/{user_id}/status'): 'admin.users.manage',
    ('GET', '/api/admin/backup'): 'admin.backup.download',
    ('POST', '/api/automation/run'): 'operations.automation.run',
    ('GET', '/api/automation/status'): 'operations.automation.read',
    ('GET', '/api/automation/runs'): 'operations.automation.read',
    ('GET', '/api/metrics'): 'observability.metrics.read',
    ('GET', '/api/audit'): 'audit.read',
    ('GET', '/api/audit/replay'): 'audit.read',
    ('GET', '/api/exports/audit.csv'): 'audit.export',
    ('GET', '/api/kpi/executive'): 'analytics.executive.read',
    ('GET', '/api/kpi/backlog/risk'): 'analytics.executive.read',
    ('GET', '/api/kpi/deterioration'): 'analytics.executive.read',
    ('GET', '/api/kpi/assets/{asset_id}'): 'analytics.executive.read',
    ('GET', '/api/kpi/parts/shortages'): 'analytics.executive.read',
    ('PATCH', '/api/sites/{site_id}'): 'sites.customers.manage',
    ('POST', '/api/assets'): 'assets.create',
    ('PATCH', '/api/assets/{asset_id}'): 'assets.update',
    ('DELETE', '/api/assets/{asset_id}'): 'assets.delete',
    ('POST', '/api/assets/health/recalculate'): 'assets.health.recalculate',
    ('POST', '/api/field/assets/{asset_id}/condition-meter'): 'assets.field.update',
    ('POST', '/api/meters/{meter_id}/readings'): 'assets.meter.reading.write',
    ('POST', '/api/work-orders'): 'work.create',
    ('PATCH', '/api/work-orders/{wo_id}'): 'work.update',
    ('POST', '/api/work-orders/{wo_id}/transition'): 'work.transition',
    ('POST', '/api/work-orders/{wo_id}/requirements'): 'work.material.plan',
    ('DELETE', '/api/work-orders/{wo_id}/requirements/{requirement_id}'): 'work.material.plan',
    ('POST', '/api/work-orders/{wo_id}/reservations'): 'work.material.reserve',
    ('POST', '/api/work-orders/{wo_id}/reserve-all'): 'work.material.reserve',
    ('POST', '/api/work-orders/{wo_id}/craft-requirements'): 'work.craft.plan',
    ('POST', '/api/work-orders/{wo_id}/labor'): 'work.labor.post',
    ('POST', '/api/work-orders/{wo_id}/materials'): 'work.material.issue',
    ('POST', '/api/work-orders/{wo_id}/notes'): 'work.notes.write',
    ('POST', '/api/work-orders/{wo_id}/tasks/{task_id}/toggle'): 'work.tasks.manage',
    ('POST', '/api/work-orders/{wo_id}/dispatch'): 'work.dispatch',
    ('POST', '/api/inventory'): 'inventory.create',
    ('POST', '/api/inventory/{item_id}/transaction'): 'inventory.transaction.post',
    ('POST', '/api/inventory/reorder-scan'): 'inventory.reorder.scan',
    ('POST', '/api/procurement/requisitions'): 'procurement.requisition.create',
    ('POST', '/api/procurement/requisitions/{pr_id}/submit'): 'procurement.requisition.submit',
    ('POST', '/api/procurement/requisitions/{pr_id}/approve'): 'procurement.requisition.approve',
    ('POST', '/api/procurement/quotations'): 'procurement.quotation.create',
    ('POST', '/api/procurement/purchase-orders'): 'procurement.purchase_order.create',
    ('POST', '/api/procurement/purchase-orders/{po_id}/receive'): 'procurement.purchase_order.receive',
    ('POST', '/api/hse'): 'hse.incident.create',
    ('PATCH', '/api/hse/{incident_id}'): 'hse.incident.update',
    ('POST', '/api/projects'): 'projects.create',
    ('POST', '/api/projects/{project_id}/tasks'): 'projects.tasks.create',
    ('PATCH', '/api/projects/{project_id}/tasks/{task_id}'): 'projects.tasks.update',
    ('PATCH', '/api/sla/policies/{policy_id}'): 'sla.policy.manage',
    ('PATCH', '/api/governance/retention/{policy_id}'): 'governance.retention.manage',
    ('POST', '/api/events/outbox/{event_id}/retry'): 'integrations.outbox.retry',
}

# Once a business route family is listed here, every state-changing route in
# that family must either have a capability overlay or an explicit narrowly
# documented exemption. This lets CI reject newly added unprotected mutations.
CAPABILITY_ENFORCED_MUTATION_PREFIXES: dict[str, tuple[str, ...]] = {
    'assets': ('/api/assets', '/api/field/assets', '/api/meters'),
    'work_management': ('/api/work-orders',),
    'inventory': ('/api/inventory',),
    'procurement': ('/api/procurement',),
    'hse': ('/api/hse',),
    'projects': ('/api/projects',),
    'sla_governance': ('/api/sla', '/api/governance', '/api/events/outbox'),
}

CAPABILITY_MUTATION_EXEMPTIONS: dict[tuple[str, str], str] = {
    ('POST', '/api/assets/{asset_id}/dossier'): (
        'Creates an immutable report snapshot for an authenticated reader; '
        'it does not mutate the asset business record.'
    ),
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
