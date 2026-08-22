# EUAS Authorization Model

EUAS uses a staged authorization model that preserves the existing role-based contract while activating the permission tables that have existed in the database schema since the original application.

## Compatibility rule

Existing endpoints continue to declare their historical `require_roles(...)` allow-list. Selected routes additionally use a database-backed capability overlay.

A request must satisfy **both** controls:

1. the caller's role must still be allowed by the endpoint's historical role policy; and
2. the caller's role must currently hold the mapped permission code in `role_permissions`.

Business/resource rules remain an additional layer. The effective rule is therefore:

```text
authenticated
AND historical route role allowed
AND mapped capability allowed
AND existing resource/workflow/business rules allowed
```

This is intentionally asymmetric: a permission can remove access immediately, but assigning a permission to a role that was historically denied does **not** grant that role access. Capability administration can narrow the legacy authorization ceiling; it cannot raise it.

## Effective permissions

`current_user()` loads effective permission codes from the caller's current role on every authenticated request. Permissions are not copied into the session record or cached inside the bearer token.

Consequences:

- a permission revocation applies to an already-issued session on its next request;
- a grant restoration also applies without logout or token rotation;
- multiple API workers or replicas sharing the same database observe the same authorization state;
- session storage remains independent from authorization policy changes.

Users can inspect their effective permissions through:

```text
GET /api/auth/permissions
```

## Permission administration

Administrators with `admin.permissions.manage` can inspect the catalog and current role grants:

```text
GET /api/admin/permissions
```

They can replace one role's complete grant set with:

```text
PUT /api/admin/roles/{role_code}/permissions
Content-Type: application/json

{
  "permissions": [
    "admin.permissions.manage",
    "admin.users.manage"
  ]
}
```

The update is transactional and audit logged as `ROLE_PERMISSIONS_UPDATE`. Unknown capability codes are rejected.

The `admin` role is not allowed to lose `admin.permissions.manage`. This protected recovery grant prevents the system from locking its administrative role out of permission management.

## Capability catalog

Default grants are inserted only when a capability code is first created. Subsequent application startups do not re-add grants that an administrator deliberately revoked.

Existing broader seed codes such as `assets.manage`, `work.manage`, `inventory.manage`, `procurement.manage`, `hse.manage`, and `admin.manage` remain for compatibility. They are not silently reinterpreted as the newer route capabilities because their historical seed semantics can be broader than individual endpoint role policies.

### Security and operations

| Permission | Default roles |
| --- | --- |
| `admin.permissions.manage` | admin |
| `admin.users.manage` | admin |
| `admin.backup.download` | admin |
| `operations.automation.run` | admin, maintenance_manager |
| `operations.automation.read` | admin, maintenance_manager, executive |
| `observability.metrics.read` | admin, maintenance_manager, executive |
| `audit.read` | admin, maintenance_manager, executive |
| `audit.export` | admin, maintenance_manager, executive |

### Assets

| Permission | Default roles |
| --- | --- |
| `assets.create` | admin, asset_manager, maintenance_manager, planner, supervisor |
| `assets.update` | admin, asset_manager, maintenance_manager, planner, supervisor |
| `assets.delete` | admin, asset_manager |
| `assets.health.recalculate` | admin, asset_manager, maintenance_manager, planner, supervisor |
| `assets.field.update` | admin, maintenance_manager, planner, supervisor, technician |
| `assets.meter.reading.write` | admin, maintenance_manager, planner, supervisor, technician |

### Work management

| Permission | Default roles |
| --- | --- |
| `work.create` | admin, asset_manager, maintenance_manager, planner, supervisor |
| `work.update` | admin, asset_manager, maintenance_manager, planner, supervisor |
| `work.transition` | admin, maintenance_manager, planner, supervisor, technician |
| `work.material.plan` | admin, maintenance_manager, planner, supervisor, storekeeper |
| `work.material.reserve` | admin, maintenance_manager, planner, supervisor, storekeeper |
| `work.craft.plan` | admin, maintenance_manager, planner, supervisor |
| `work.labor.post` | admin, maintenance_manager, planner, supervisor, technician |
| `work.material.issue` | admin, maintenance_manager, planner, storekeeper, technician |
| `work.notes.write` | admin, maintenance_manager, planner, supervisor, technician |
| `work.tasks.manage` | admin, maintenance_manager, planner, supervisor, technician |
| `work.dispatch` | admin, maintenance_manager, planner, supervisor |

### Inventory and procurement

| Permission | Default roles |
| --- | --- |
| `inventory.create` | admin, storekeeper, maintenance_manager |
| `inventory.transaction.post` | admin, maintenance_manager, planner, storekeeper, technician |
| `inventory.reorder.scan` | admin, storekeeper, maintenance_manager, procurement |
| `procurement.requisition.create` | admin, storekeeper, maintenance_manager, procurement, planner |
| `procurement.requisition.submit` | admin, storekeeper, maintenance_manager, procurement, planner |
| `procurement.requisition.approve` | admin, maintenance_manager, procurement |
| `procurement.quotation.create` | admin, maintenance_manager, procurement |
| `procurement.purchase_order.create` | admin, maintenance_manager, procurement |
| `procurement.purchase_order.receive` | admin, procurement, storekeeper |

### HSE, projects, SLA and governance

| Permission | Default roles |
| --- | --- |
| `hse.incident.create` | admin, hse, maintenance_manager |
| `hse.incident.update` | admin, hse, maintenance_manager |
| `projects.create` | admin, project_manager, maintenance_manager |
| `projects.tasks.create` | admin, project_manager, maintenance_manager |
| `projects.tasks.update` | admin, project_manager, maintenance_manager |
| `sla.policy.manage` | admin, maintenance_manager |
| `governance.retention.manage` | admin |
| `integrations.outbox.retry` | admin, maintenance_manager |

## Business mutation coverage

Capability-enforced mutation families now cover:

- Assets, including field condition/meter writes;
- Work Management;
- Inventory;
- Procurement;
- HSE;
- Projects; and
- SLA/Governance, including integration outbox retry.

The authorization contract test enumerates FastAPI routes and fails CI when a `POST`, `PUT`, `PATCH`, or `DELETE` route appears inside a migrated family without a capability overlay or a documented exemption.

The only current business-mutation-prefix exemption is:

```text
POST /api/assets/{asset_id}/dossier
```

That endpoint creates an immutable report snapshot for an authenticated reader and does not mutate the asset business record.

## Structural authorization contract

CI verifies all of the following:

1. every overlay points to exactly one registered route;
2. every overlay references a defined capability;
3. every capability's default role set exactly equals the route's historical `require_roles(...)` set;
4. every migrated business domain remains present in the mutation-coverage map;
5. every state-changing route inside a migrated family has an overlay or a documented real-route exemption;
6. permission-management endpoints retain the protected `admin.permissions.manage` capability; and
7. the protected recovery capability is not reused as an ordinary route overlay.

This structural gate complements API regressions that prove an existing session immediately loses access after capability revocation and that assigning a capability to a historically forbidden role still yields `403`.

## Operational guidance

Treat role-permission changes as privileged configuration. Export and retain the audit event generated by each change, and review grant changes alongside user/role changes in production governance.

For enterprise deployments, this internal permission layer can remain the application authorization policy beneath corporate SSO/OIDC/SAML authentication. External identity should map authenticated users/groups to EUAS roles or future claims, while EUAS continues to enforce application permissions server-side.
