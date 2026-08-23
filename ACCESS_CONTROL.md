# EUAS Fine-Grained Access Control

EUAS v4.6.0 adds administered fine-grained permissions on top of the existing role model. The server remains the source of truth: UI visibility is only a convenience and cannot grant authority.

## Authorization model

Every active user has a role. Roles carry permission grants from the permission catalog. A user can also have an explicit, optionally expiring override for one permission.

For a permission evaluated at request time, precedence is:

1. active user **Deny** override;
2. active user **Allow** override;
3. current role grant;
4. deny by default.

Expired overrides no longer participate in authorization. The `/api/auth/me/permissions` endpoint returns the effective decision and source so the client can render the interface consistently with the server.

## Permission catalog

v4.6 defines fine-grained permissions for access administration, assets, work management, inventory, procurement, HSE, projects, documents, telemetry, alarm operations, approval decisions, retention execution, legal holds, automation and integration-key administration. Permissions carry a category, risk level and description.

Existing seeded roles receive a compatible baseline during migration. Default grants are inserted only when a permission is first introduced; restarting the application does **not** silently restore a grant an administrator intentionally removed.

## Administration APIs

Administrators with `admin.permissions.manage` can use:

- `GET /api/admin/access-control` — permission catalog, roles, grants, users and active overrides;
- `GET /api/admin/roles/{role_code}/permissions` — current grants for a role;
- `PUT /api/admin/roles/{role_code}/permissions` — replace the role grant set;
- `GET /api/admin/users/{user_id}/permission-overrides` — user-specific overrides;
- `POST /api/admin/users/{user_id}/permission-overrides` — Allow, Deny or Inherit a permission;
- `PATCH /api/admin/users/{user_id}/role` — change a user's role;
- `GET /api/exports/access-control.csv` — governance export of the active access model.

The Administration UI exposes the same role-permission matrix, user overrides and role-change controls.

## High-risk change controls

Role-grant replacement, user override changes and role changes require:

- the administrator's current password;
- a non-empty reason;
- the exact confirmation phrase `UPDATE ACCESS`.

Changes are audited and emitted to the event outbox. Access-control metrics expose role-grant volume and active Allow/Deny override counts.

## Lockout protection

EUAS prevents the reference deployment from removing its last usable access-administration path:

- the administrator role cannot lose `admin.users.manage` or `admin.permissions.manage`;
- those core administrator permissions cannot be explicitly Denied for an administrator;
- the last active administrator cannot be moved to another role.

These guards reduce accidental lockout. Production identity governance should still provide emergency access, dual control, periodic recertification and independent identity administration.

## Approval and domain constraints still apply

A permission answers whether a caller may attempt an operation. It does not bypass domain constraints. For example, `approvals.decide` does not let a user approve an arbitrary record: assignment, role routing, active delegation and four-eyes rules still apply. Technician work execution remains constrained by work assignment where applicable.

## Security boundary

The v4.6 implementation is application authorization, not a replacement for enterprise IAM. It does not claim OIDC/SSO, MFA, SCIM provisioning, privileged-access management, segregation-of-duties certification or tenant isolation. Production deployments should integrate corporate identity, MFA, access reviews and centralized security monitoring.
