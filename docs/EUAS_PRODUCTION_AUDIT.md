# EUAS Production Audit

Audit date: 2026-08-27
Repository: `C:\Users\omar\elsewedy-utilities-application-suite`
Canonical branch: `oxalpha/session-hardening-wave`
Canonical HEAD: `6fc1aea`
Application version: 3.9.0

## Security Headers

### Dev Mode (application.py)

Applied by the `security_headers` HTTP middleware in `app/application.py:48-78`:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(self), geolocation=(self), microphone=()` |
| `X-Request-ID` | Sanitized caller-supplied or UUID fallback |
| `Cache-Control` | `no-store` for `/api/*`; `no-cache` otherwise |

Note: The dev CSP includes `'unsafe-inline'` for both `style-src` and `script-src` to support legacy inline scripts and styles during development.

### Production Mode (production.py)

Applied by `ProductionSecurityHeaders` in `app/production.py:103-145`, which replaces the dev CSP and emits deployment-only headers:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; img-src 'self' data: blob:; font-src 'self'; media-src 'self'; style-src 'self'; style-src-elem 'self'; style-src-attr 'none'; script-src 'self'; script-src-elem 'self'; script-src-attr 'none'; worker-src 'self'; manifest-src 'self'; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'` |
| `Strict-Transport-Security` | `max-age=31536000` (HTTPS only) |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), geolocation=(), microphone=()` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Cross-Origin-Embedder-Policy` | `require-corp` |
| `Origin-Agent-Cluster` | `?1` |
| `X-Permitted-Cross-Domain-Policies` | `none` |
| `Cache-Control` (API only) | `no-store, private, max-age=0` |
| `Pragma` (API only) | `no-cache` |
| `Expires` (API only) | `0` |

Production CSP differences from dev: no `'unsafe-inline'` on any directive; explicit `font-src`, `media-src`, `style-src-elem`, `style-src-attr 'none'`, `script-src-elem`, `script-src-attr 'none'`, `worker-src`, `manifest-src`, `frame-src`, `form-action`. HSTS is only emitted when the ASGI scheme is `https`.

### Additional Production Middleware

Defined in `app/production.py` and composed at module level (`app/production.py:297-307`):

- **TrustedHostMiddleware** (`app/production.py:281-290`): Rejects requests with unrecognized `Host` headers. `EUAS_ALLOWED_HOSTS` must list deployment hostnames; `*` wildcard is rejected.
- **ProductionPrivateIntrospectionBoundary** (`app/production.py:179-201`): Returns 404 for `/api/docs`, `/openapi.json`, `/docs/oauth2-redirect`, and `/api/docs/*`.
- **ProductionPrivateUploadBoundary** (`app/production.py:204-226`): Returns 404 for `/uploads` and `/uploads/*`, blocking unauthenticated download of stored attachments.
- **ProductionRequestIdBoundary** (`app/production.py:148-176`): Normalizes `X-Request-ID` to a single compact ASCII token (regex-validated); missing, duplicated, overlong, or malformed values are replaced with a server-generated 128-bit hex identifier.
- **TrustedProxyScheme** (`app/production.py:229-278`): Resolves `X-Forwarded-Proto` only from peers within `EUAS_TRUSTED_PROXY_CIDRS`.

## Secret Management

### Environment Variables

Secrets are sourced from environment variables, never hardcoded (`app/config.py:39-40`):

- `EUAS_EVENT_WEBHOOK_URL` -- outbound webhook target
- `EUAS_EVENT_WEBHOOK_SECRET` -- HMAC-SHA256 signing key for webhook payloads

The production readiness gate (`scripts/production_readiness.py:102-107`) validates that a webhook secret is set whenever a webhook URL is configured.

### Password Hashing

Implemented in `app/auth.py:12-43`:

- Algorithm: PBKDF2-HMAC-SHA256
- Work factor: 600,000 rounds (`PBKDF2_ROUNDS`)
- Legacy work factor: 180,000 rounds (`LEGACY_PBKDF2_ROUNDS`)
- Salt: 16-byte random via `secrets.token_hex(16)`
- Format: `pbkdf2_sha256$<rounds>$<salt>$<digest>`
- Constant-time comparison: `secrets.compare_digest` (`app/auth.py:74`)
- Password hash upgrade: automatic on successful login when legacy format or insufficient rounds detected (`app/auth.py:77-90`)

### Session Tokens

Implemented in `app/auth_store.py:12,186-218`:

- Generation: `secrets.token_urlsafe(48)` (48 bytes of cryptographic randomness)
- Storage: only SHA-256 digest retained (`app/auth_store.py:29-32`)
- Raw bearer tokens are never included in session-list responses, audit events, or metrics
- Legacy raw-token sessions are lazily migrated to digest-only on access

### Login Throttling

Three-tier database-backed throttling (`app/auth_store.py:14-22,344-426`):

| Scope | Threshold | Description |
|---|---|---|
| Account + Client | 5 failures / 5 min | Per-(username, host) backoff |
| Client-wide | 50 failures / 5 min | Per-host rotation limit |
| Account-global | 30 failures / 5 min | Host-independent account ceiling |

Progressive backoff: 30s base, capped at 5 minutes. Successful login clears account/client scope; client-wide scope persists. Missing/disabled principals execute a dummy PBKDF2 verification to prevent account enumeration timing leaks (`app/main.py:72`).

## Production Mode

Setting `EUAS_ENV=production` triggers the following enforced behaviors:

| Control | Source | Effect |
|---|---|---|
| Demo credential rejection | `app/migrations.py:339-353,440-483` | Packaged demo passwords are rejected; admin bootstrap password must be at least 16 characters and must not match any demo credential |
| Mandatory admin password rotation | `app/migrations.py:347-353` | Bootstrap password is derived from a deployment secret and rotated at startup |
| Strict CSP + HSTS | `app/production.py:56-76` | No inline scripts/styles; HSTS on HTTPS |
| Upload path blocked | `app/production.py:204-226` | `/uploads/*` returns 404 |
| Swagger blocked | `app/production.py:179-201` | `/api/docs`, `/openapi.json`, `/docs/oauth2-redirect` return 404 |
| Trusted host validation | `app/production.py:281-290` | Rejects unrecognized `Host` headers |
| API cache headers | `app/production.py:90-94,128-139` | `no-store, private, max-age=0` on all `/api/*` responses |

## Input Validation

### Request Models (app/application.py)

| Field | Constraint | Source |
|---|---|---|
| Login username | `min_length=1, max_length=150` | `LoginIn` (line 943) |
| Login password | `min_length=1, max_length=1024` | `LoginIn` (line 944) |
| User creation password | `min_length=8, max_length=128` | `UserIn` (line 984) |
| Password change new_password | `min_length=10, max_length=128` | `PasswordChange` (line 992) |
| Telemetry batch | `min_length=1, max_length=500` readings | `TelemetryIngestIn` (line 1033) |
| Upload size | Configurable, default 25 MB | `config.py:16-18` (`EUAS_MAX_UPLOAD_MB`) |
| Work order title | `max_length=200` | `WorkOrderIn` (line 957) |
| Work order description | `max_length=2000` | `WorkOrderIn` (line 957) |
| Work order safety_requirements | `max_length=1000` | `WorkOrderIn` (line 958) |
| Work order instructions | `max_length=2000` | `WorkOrderIn` (line 958) |
| Work order checklist | `max_length=2000` | `WorkOrderIn` (line 958) |
| Asset name | `max_length=200` | `AssetIn` (line 946) |
| Asset description | `max_length=2000` | `AssetIn` (line 946) |
| HSE incident title | `max_length=200` | `HSEIn` (line 981) |
| HSE description | `max_length=2000` | `HSEIn` (line 981) |
| HSE corrective_action | `max_length=2000` | `HSEIn` (line 981) |
| Note text | `max_length=2000` | `NoteIn` (line 967) |
| Telemetry client_ref | `max_length=128` | `TelemetryReadingItem` (line 1031) |

### Password Policy

Enforced at password change (`app/application.py:1122-1123`, `app/main.py:325-328`):

- Minimum 10 characters
- Must include uppercase letter
- Must include lowercase letter
- Must include digit
- Must include special character (not purely alphanumeric)

## CORS

No CORS middleware is configured. The application relies on same-origin policy by default, which is the most restrictive cross-origin stance. No `Access-Control-Allow-Origin` headers are emitted.

## CSV Injection Protection

Implemented in `app/application.py:823` (`_csv_safe_cell`). Formula prefix characters (`=`, `+`, `-`, `@`, `\t`, `\r`) are neutralized by prepending a single quote before the value is written to CSV exports. This prevents spreadsheet applications from interpreting cell content as formulas.

Tested in `tests/test_export_csv_injection.py`.

## Production Readiness Gate

Script: `scripts/production_readiness.py`

Validates deployment configuration before production launch:

| Check | Description |
|---|---|
| `environment` | `EUAS_ENV` value; strict mode requires `production` |
| `database_backend` | PostgreSQL vs SQLite detection |
| `allowed_hosts` | `EUAS_ALLOWED_HOSTS` must list explicit hostnames; `*` rejected |
| `webhook_signing` | `EUAS_EVENT_WEBHOOK_SECRET` required when webhook URL is set |
| `automation_scheduler` | Whether in-process scheduler is enabled |
| `session_lifetime` | Session hours within 1-24 range |
| `upload_limit` | Upload size within 1-100 MB range |

Database checks (with `--check-db`):

| Check | Description |
|---|---|
| `critical_tables` | All 13 required tables present |
| `seed_integrity` | Users and assets exist |
| `schema_contract` | Schema version matches application expectation |
| `schema_migrations` | No pending/invalid/future migrations |
| `default_credentials` | No packaged demo passwords remain (FAIL in production) |
| `audit_chain_integrity` | Tamper-evident audit chain validates |
| `database_connectivity` | Database adapter operational |

Usage:

```powershell
python scripts/production_readiness.py
python scripts/production_readiness.py --strict-production --require-postgres --check-db
python scripts/production_readiness.py --json
```
