# EUAS Security Guide

EUAS includes security controls appropriate for a runnable reference deployment. A public production deployment should still be placed behind enterprise identity, TLS termination, a WAF/API gateway and centralized observability.

## Implemented controls

- Versioned PBKDF2-HMAC-SHA256 password hashes with per-user random salts.
- New and changed passwords use a 600,000-iteration work factor.
- Legacy EUAS password hashes remain verifiable and are upgraded after a successful login without requiring a password reset.
- Malformed password-hash values fail closed instead of raising authentication-path parsing exceptions.
- Bearer tokens are generated with cryptographically secure randomness and only a SHA-256 digest is retained in the current server-side session store.
- Sessions have non-secret IDs, creation/last-seen/expiry timestamps, revocation state and a coarse client label.
- Configurable server-side session expiry (`EUAS_SESSION_HOURS`).
- Logout, single-session revocation, revoke-other-sessions and revoke-all-sessions are enforced server-side.
- Password changes revoke all other sessions for that user; account deactivation revokes all active sessions.
- Login throttling is database-backed with one-way account/client and client-wide scopes, and uses temporary progressive backoff instead of permanent account locking.
- Missing or disabled login principals execute a current-work-factor dummy password verification to reduce simple account-enumeration timing differences.
- Role-based authorization is enforced at API endpoints.
- User-controlled profile fields are server validated.
- Upload allow-list and configurable maximum attachment size.
- Randomized stored attachment names; original names are retained only as metadata/download names.
- Foreign-key enforcement and transactional rollback for database writes.
- Audit records for authentication and important business changes; passwords and raw bearer tokens are not included in those events.
- Security response headers: request ID, `nosniff`, frame denial, referrer policy, permissions policy and Content Security Policy.
- API responses default to `Cache-Control: no-store`.
- GitHub CodeQL scans Python with the `security-extended` query suite on pushes, pull requests and a weekly schedule.
- `pip-audit` checks the resolved Python dependency environment for known vulnerability advisories.
- Trivy scans the built EUAS container image and fails on fixable high/critical OS or library vulnerabilities.
- The production container is smoke-tested through `/api/health` before vulnerability scanning.
- Dependabot monitors Python packages, GitHub Actions and the pinned Docker base image for version updates.

## Password-hash migration contract

Current password hashes use the self-describing format:

```text
pbkdf2_sha256$600000$<salt>$<digest>
```

EUAS also recognizes the historical format:

```text
<salt>$<digest>
```

Historical hashes are verified with the legacy 180,000-iteration work factor. `password_needs_upgrade()` identifies obsolete representations and `verify_password_with_upgrade()` returns a replacement current hash only after the supplied password verifies successfully.

The login flow persists that replacement in the same successful authentication transaction. New users and password changes are always written using the current versioned format. No plaintext password is persisted or logged.

## Session lifecycle and storage

Current sessions are stored in `auth_sessions`. The bearer value returned to the client is high-entropy random material; EUAS stores only its SHA-256 digest because random session tokens do not require a slow password-hashing function. Authentication hashes the presented bearer and looks up the digest. Raw bearer tokens are not included in session-list responses, audit events or metrics.

Each current session includes a non-secret session ID, user ID, creation time, last-seen time, expiry, optional revocation time and a coarse client label. The label is derived from the User-Agent for usability and is not intended as a fingerprint. EUAS does not persist client IP addresses as session metadata.

Schema v10 retains the historical `sessions` table only as a compatibility landing zone for rolling upgrades. Startup migrates legacy rows into `auth_sessions` by hashing the existing bearer value and deleting the raw legacy row. Session resolution also supports one-row lazy migration so a valid session created by an older application instance can survive a mixed-version deployment without forced logout. The migration insert is conflict-safe so concurrent replicas can observe the same historical row without creating duplicate digest sessions. New logins never write raw bearer tokens into the legacy table.

Expired and revoked sessions are rejected. Revoked rows remain available for database-level operational evidence until normal retention/maintenance removes them; the bearer itself cannot be recovered from the stored digest.

## Login throttling

Failed-login state is stored in `auth_login_throttle`, so it survives process restarts and is shared by application workers or replicas using the same database. EUAS maintains two one-way scopes: an account-plus-client scope for repeated attacks against one principal and a wider client-only scope that limits username rotation from the same client. Both keys are SHA-256 digests; the throttle table does not retain the raw account identifier or client address.

EUAS uses a rolling five-minute failure window. The account/client scope begins temporary backoff after five failures, while the wider client scope has a higher threshold to reduce denial-of-service risk for shared client networks. Repeated failures increase the temporary backoff up to a bounded maximum instead of permanently locking an account. A successful authentication clears its account/client failure state but deliberately does not erase the wider client abuse history.

Authentication failures continue to use the same generic invalid-credentials response so the API does not disclose whether an account exists. Missing and disabled principals still execute one PBKDF2 verification at the current work factor against a fixed non-secret dummy verifier, reducing the obvious timing difference between unknown accounts and valid-account password checks.

For very large multi-region deployments, a purpose-built shared rate-limit service such as Redis or a gateway-native limiter may provide better throughput and abuse analytics, but process-local memory is not the source of truth for the current EUAS limiter.

## Authentication transport and CSRF

The current API authenticates with an `Authorization: Bearer` header. It does not place the authentication bearer in a browser cookie. Traditional cookie-CSRF tokens are therefore not part of the current authentication design. If EUAS later introduces cookie-based authentication, the cookie and state-changing request model must be reviewed together for `HttpOnly`, `Secure`, `SameSite`, expiry/domain/path policy and CSRF protection before that mode is enabled.

Production traffic must still be protected with TLS so bearer credentials cannot be observed in transit.

## Continuous security validation

The dedicated `EUAS Security` workflow contains three independent controls:

- **CodeQL / Python** performs static security analysis and publishes supported findings to GitHub code scanning.
- **Python dependency audit** installs the application dependency set and runs `pip-audit`; known vulnerable resolved dependencies fail the job.
- **Container build, smoke and image scan** builds the hardened production Dockerfile, boots the non-root image, verifies the health endpoint, and runs Trivy against OS and library packages; fixable high/critical findings fail the job.

The Trivy GitHub Action is pinned to a full release commit SHA rather than a movable tag. The workflow runs for `main`, pull requests targeting `main`, manual dispatches, and on a weekly schedule. Dependabot separately proposes updates for pip dependencies, GitHub Actions and the Docker base image.

The production image installs a runtime-only dependency set and copies only the application and static assets needed by FastAPI, excluding test tooling and repository engineering metadata from the runtime filesystem.

The PostgreSQL integration smoke exercises the live authentication path and validates digest-backed session persistence plus legacy-session migration on the configured PostgreSQL target. Security scanning complements, but does not replace, penetration testing, runtime monitoring, infrastructure hardening, secret management, malware scanning for uploads, or deployment-specific threat modeling.

## Production hardening checklist

1. Terminate TLS using an approved ingress/reverse proxy and redirect all HTTP to HTTPS.
2. Replace demo accounts/passwords before exposing the service to any network.
3. Integrate corporate SSO (OIDC/SAML/Entra ID or equivalent) and MFA.
4. Move persistence to PostgreSQL HA and managed/object attachment storage.
5. For high-scale or multi-region deployments, consider a dedicated shared rate-limit service while retaining server-side session revocation semantics.
6. Run malware scanning and content inspection on uploaded documents.
7. Store secrets in a secrets manager instead of `.env` files.
8. Restrict database/storage network access to application identities only.
9. Forward audit logs, request IDs, metrics and authentication events to centralized SIEM/observability platforms.
10. Add deployment-target DAST and periodic penetration testing alongside the existing CodeQL, dependency and container-image scans before internet-facing deployment.
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