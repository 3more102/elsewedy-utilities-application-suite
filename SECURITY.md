# EUAS Security Guide

EUAS v4.9.0 includes security controls appropriate for a runnable reference deployment. A public production deployment should still be placed behind enterprise identity, TLS termination, a WAF/API gateway and centralized observability.

## Implemented controls

- PBKDF2-SHA256 password hashing with per-user random salts.
- Cryptographically random bearer sessions stored server-side.
- Configurable server-side session expiry (`EUAS_SESSION_HOURS`).
- Login throttling after repeated failed attempts for the same client/user pair.
- Server-side role and fine-grained permission authorization enforced at API endpoints.
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

## Production hardening checklist

1. Terminate TLS using an approved ingress/reverse proxy and redirect all HTTP to HTTPS.
2. Replace demo accounts/passwords before exposing the service to any network.
3. Integrate corporate SSO (OIDC/SAML/Entra ID or equivalent) and MFA.
4. Move persistence to PostgreSQL HA and managed/object attachment storage.
5. Use Redis or another shared store for distributed sessions/rate limiting when running multiple API replicas.
6. Run malware scanning and content inspection on uploaded documents.
7. Store secrets in a secrets manager instead of `.env` files.
8. Restrict database/storage network access to application identities only.
9. Forward audit logs, request IDs, metrics and authentication events to centralized SIEM/observability platforms.
10. Add dependency/container/SAST/DAST scanning to CI before internet-facing deployment.
11. Review CSP and remove `unsafe-inline` by migrating the remaining generated inline event handlers to delegated listeners before strict CSP enforcement.
12. Define backup, restore, retention and disaster-recovery RPO/RTO policies.

## Demo credentials

Credentials in the seed data exist only to make the delivered reference application immediately demonstrable. They are not suitable for a production environment.


## v4.6 fine-grained access administration

EUAS evaluates fine-grained permissions server-side. An active user-specific override takes precedence over the role grant; Deny and Allow overrides can expire, after which role inheritance resumes. The client consumes effective permissions only to improve usability and is not an authorization boundary.

Access-administration mutations require the acting administrator's current password, an explicit reason and the exact `UPDATE ACCESS` confirmation. The reference application protects the two core administrator permissions from removal/explicit denial and prevents changing the last active administrator away from the admin role. Every access change is written to audit/outbox evidence.

A permission does not bypass business constraints such as assigned approver, active delegation, four-eyes shelving approval or technician assignment. Production deployments should still use corporate SSO/MFA, privileged-access governance, access recertification and centralized security monitoring. See `ACCESS_CONTROL.md`.

## Approval authorization and electronic evidence

Approval decisions are enforced server-side. A decision is accepted only from an administrator/maintenance manager or from the user/role explicitly assigned to the approval (including an active delegation where applicable). Technician work execution is also constrained to work assigned to that technician unless the caller has an elevated planning/supervisory role.

For generic Approval Center decisions, v4.4 additionally requires the acting signer to re-enter the current account password and confirm an exact intent statement bound to the record code. Successful decisions create hash-chained `approval_signature_evidence`; passwords are never persisted in that evidence. Re-authentication proves possession of the current application credential at decision time but is not MFA, PKI signing or legal non-repudiation.


## Operational endpoint authorization

Automation execution is limited to System Administrator and Maintenance Manager roles. Automation status/run history and metrics are management-only. Audit export is management-only and SQLite backup download is administrator-only. The inactive internal `system` principal owns externally scheduled automation audit records but cannot authenticate interactively.

Backup bundles can contain operational data and uploaded documents; protect them with the same controls as the primary database and use encryption-at-rest in production backup storage.


## Integration webhook security

Outbound event delivery is disabled by default. If `EUAS_EVENT_WEBHOOK_URL` is configured, set a strong `EUAS_EVENT_WEBHOOK_SECRET`. EUAS signs the exact JSON request body with HMAC-SHA256 in `X-EUAS-Signature` and identifies the event using `X-EUAS-Event` and `X-EUAS-Event-ID`. The destination URL is deployment configuration, not end-user input. Production deployments should restrict egress at the network layer and rotate the webhook secret through the platform secret manager.


## Tamper-evident audit records

EUAS links audit rows with deterministic SHA-256 hashes and exposes an integrity verification endpoint/CLI. The mechanism detects persisted content changes inside the chain, but it does not prevent a sufficiently privileged database operator from rewriting both records and hashes. Environments with regulatory evidence requirements should anchor the chain externally or replicate audit events into access-controlled immutable/WORM storage.

Report snapshots use a similar stored-content SHA-256 verification model. They are append-only through the EUAS API, but database-level immutability is an infrastructure responsibility.

## Retention execution security

v4.5 separates policy eligibility from destructive execution. Preview runs are non-destructive. Execute runs are admin-only, require current-password verification and require the exact `EXECUTE RETENTION` confirmation. Legal-hold release also requires current-password verification and a release reason. Protected data classes remain non-destructive regardless of cutoff.

Retention manifests are linked by SHA-256 and can detect stored-content modification. This evidence chain does not prevent a privileged database operator from rewriting both data and hashes; regulated deployments should export evidence packages to external object-lock/WORM storage and govern retention policy changes through formal change control.

## v4.0 integration-key controls

Telemetry machine credentials use a separate path from human bearer sessions. The application:

- generates high-entropy secrets with the `euas_` prefix;
- returns plaintext only once at creation;
- stores only SHA-256 digests;
- limits current machine scope to `telemetry:write`;
- supports expiry and immediate revocation;
- records `last_used_at`;
- attributes machine ingestion to the internal service principal for audit continuity.

For production, place the telemetry API behind TLS/API gateway controls, rotate integration keys, use source-network restrictions where appropriate, and prefer a dedicated secrets manager for gateway-side storage.


## v4.3 offline field security boundary

Field sync clients are user-bound. Technician synchronization is limited to assigned work, linked assets and owned dispatches. Mutable offline changes require deterministic base hashes; stale changes become explicit conflicts. The browser cache is scoped by user and explicit logout removes the cached field snapshot, queue and conflicts. Offline reopen is bounded by the server-issued session expiry cached at login. Production mobile deployments should use native secure storage, MDM/device encryption and enterprise identity rather than treating browser local storage as a hardened credential vault.
