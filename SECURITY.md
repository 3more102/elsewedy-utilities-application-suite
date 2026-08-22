# EUAS Security Guide

EUAS includes security controls appropriate for a runnable reference deployment. A public production deployment should still be placed behind enterprise identity, TLS termination, a WAF/API gateway and centralized observability.

## Implemented controls

- Versioned PBKDF2-HMAC-SHA256 password hashes with per-user random salts.
- New and changed passwords use a 600,000-iteration work factor.
- Legacy EUAS password hashes remain verifiable during migration; the authentication library detects hashes that require an upgrade.
- Malformed password-hash values fail closed instead of raising authentication-path parsing exceptions.
- Cryptographically random bearer sessions stored server-side.
- Configurable server-side session expiry (`EUAS_SESSION_HOURS`).
- Login throttling after repeated failed attempts for the same client/user pair.
- Role-based authorization enforced at API endpoints.
- User activation/deactivation with immediate session revocation.
- Password change requires the current password and a stronger replacement password.
- Changing a password revokes all other sessions for that user.
- User-controlled profile fields are server validated.
- Upload allow-list and configurable maximum attachment size.
- Randomized stored attachment names; original names are retained only as metadata/download names.
- Foreign-key enforcement and transactional rollback for database writes.
- Audit records for authentication and important business changes.
- Security response headers: request ID, `nosniff`, frame denial, referrer policy, permissions policy and Content Security Policy.
- API responses default to `Cache-Control: no-store`.

## Password-hash migration contract

Current password hashes use the self-describing format:

```text
pbkdf2_sha256$600000$<salt>$<digest>
```

EUAS also recognizes the historical format:

```text
<salt>$<digest>
```

Historical hashes are verified with the legacy 180,000-iteration work factor. The authentication library exposes `password_needs_upgrade()` and `verify_password_with_upgrade()` so login flows can replace valid legacy or lower-work-factor hashes without requiring a password reset.

The repository currently preserves compatibility with existing credentials. New users and password changes are written using the current versioned format. Login-time persistence of upgrade hashes is a separate integration step and must be validated across both SQLite and PostgreSQL before activation.

## Session-storage status

Bearer sessions are currently server-side and expire according to `EUAS_SESSION_HOURS`. The current database schema stores the bearer token value directly in the session table so the existing web/API clients remain compatible.

For internet-facing or multi-replica production deployments, the next session-hardening step is to store only a one-way token digest, preserve a non-secret session identifier for user-facing session management, and move rate-limit state to a shared persistent store. These controls should be migrated atomically with login, logout, password-change and session-revocation paths so active-session semantics remain correct.

## Production hardening checklist

1. Terminate TLS using an approved ingress/reverse proxy and redirect all HTTP to HTTPS.
2. Replace demo accounts/passwords before exposing the service to any network.
3. Integrate corporate SSO (OIDC/SAML/Entra ID or equivalent) and MFA.
4. Move persistence to PostgreSQL HA and managed/object attachment storage.
5. Store session token digests rather than raw bearer tokens and use Redis or another shared store for distributed rate limiting when running multiple API replicas.
6. Run malware scanning and content inspection on uploaded documents.
7. Store secrets in a secrets manager instead of `.env` files.
8. Restrict database/storage network access to application identities only.
9. Forward audit logs, request IDs, metrics and authentication events to centralized SIEM/observability platforms.
10. Add dependency/container/SAST/DAST scanning to CI before internet-facing deployment.
11. Review CSP and remove `unsafe-inline` by migrating the remaining generated inline event handlers to delegated listeners before strict CSP enforcement.
12. Maintain tested backup, restore, retention and disaster-recovery RPO/RTO policies.

## Demo credentials

Credentials in the seed data exist only to make the delivered reference application immediately demonstrable. They are not suitable for a production environment.

## Approval authorization

Approval decisions are enforced server-side. A decision is accepted only from an administrator/maintenance manager or from the user/role explicitly assigned to the approval. Technician work execution is also constrained to work assigned to that technician unless the caller has an elevated planning/supervisory role.

## Operational endpoint authorization

Automation execution is limited to System Administrator and Maintenance Manager roles. Automation status/run history and metrics are management-only. Audit export is management-only and SQLite backup download is administrator-only. The inactive internal `system` principal owns externally scheduled automation audit records but cannot authenticate interactively.

Backup bundles can contain operational data and uploaded documents; protect them with the same controls as the primary database and use encryption-at-rest in production backup storage.

## Integration webhook security

Outbound event delivery is disabled by default. If `EUAS_EVENT_WEBHOOK_URL` is configured, set a strong `EUAS_EVENT_WEBHOOK_SECRET`. EUAS signs the exact JSON request body with HMAC-SHA256 in `X-EUAS-Signature` and identifies the event using `X-EUAS-Event` and `X-EUAS-Event-ID`. The destination URL is deployment configuration, not end-user input. Production deployments should restrict egress at the network layer and rotate the webhook secret through the platform secret manager.

## Tamper-evident audit records

EUAS links audit rows with deterministic SHA-256 hashes and exposes an integrity verification endpoint/CLI. The mechanism detects persisted content changes inside the chain, but it does not prevent a sufficiently privileged database operator from rewriting both records and hashes. Environments with regulatory evidence requirements should anchor the chain externally or replicate audit events into access-controlled immutable/WORM storage.

Report snapshots use a similar stored-content SHA-256 verification model. They are append-only through the EUAS API, but database-level immutability is an infrastructure responsibility.
