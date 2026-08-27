# EUAS — Elsewedy Utilities Application Suite

**One Platform. Every Asset. Every Operation.**

Enterprise utility operations platform for asset management, work management, reliability analytics, telemetry, operations intelligence, maintenance, and utility KPIs across electrical, water, infrastructure, and facilities environments.

> EUAS is an independent suite of integrated applications. It does not contain proprietary source code, protected graphics, or copied layouts from other commercial products.

---

## Key Capabilities

| Domain | Capabilities |
|--------|-------------|
| **Asset Management** | Hierarchy, condition, criticality, health scoring, lifecycle timeline, dossier snapshots |
| **Work Management** | Corrective, preventive, emergency, inspection, project work with full lifecycle |
| **Operations Command Center** | Situation fusion, WHY-red intelligence, restoration progress, recommendations, inbox |
| **Reliability Analytics** | SAIFI / SAIDI / CAIDI / ASAI, outage intelligence, bad actors, trend adapters |
| **APM / CBM / FMEA** | Condition-based maintenance, deterioration signals, risk-weighted backlog |
| **Telemetry & Alarms** | SCADA-style ingestion, threshold evaluation, alarm lifecycle, corrective work generation |
| **Inventory & Materials** | Warehouses, reservations, reorder scan, issue/return/transfer, shortage expedite |
| **Procurement** | Requisitions, RFQ, purchase orders, receipt into stock |
| **Workforce Planning** | Crafts, technician profiles, shifts, absences, capacity forecasting |
| **Approvals** | Unified approval queue with delegation and workflow history |
| **HSE & Safety** | Incidents, hazards, near misses, risk scoring, corrective actions |
| **Projects** | Budget, progress, task tracking |
| **Documents** | Linked to assets, work orders, locations, projects, vendors |
| **Analytics** | MTBF, MTTR, availability, cost, procurement, inventory health |
| **Automation & Reports** | PM/reorder/SLA routines, exports, governance evidence, backup |
| **Authorization** | RBAC with capability overlays, site scoping, 40+ permission codes |
| **Audit Chain** | Tamper-evident SHA-256 hash chain with serialized appends |
| **Production Security** | CSP, HSTS, trusted hosts, input validation, IDOR protections |

---

## Architecture

```text
User
  |
EUAS Web UI (SPA)
  |  Responsive HTML5 + JavaScript + CSS design system
  |  CSP-safe event delegation, PWA shell caching
  |
FastAPI Application Layer
  |  Authentication (PBKDF2-SHA256, 600k rounds)
  |  RBAC + Capability authorization
  |  Workflow engine, SLA governance
  |  Audit chain, event outbox
  |
Domain Services
  |-- Assets          |-- Work Orders       |-- Operations
  |-- Reliability/APM |-- Telemetry/Alarms   |-- Inventory
  |-- KPI Engine      |-- Procurement        |-- Workforce
  |-- Approvals       |-- HSE/Safety         |-- Documents
  |
Persistence Layer
  |  SQLite (reference) / PostgreSQL (production)
  |  Schema v12, 67 tables, 45+ indexes
  |  Concurrency controls, WAL mode, busy_timeout
  |
Audit & Integration
  |  SHA-256 hash chain audit log
  |  Event outbox with webhook delivery
  |  Report snapshots with content hashing
```

---

## Screenshots

> Screenshots are representative management-demo captures from the EUAS v3.x interface.
> Place actual screenshots in `docs/screenshots/` and reference them here.

| View | Path |
|------|------|
| Login | `docs/screenshots/01-login.png` |
| Application Launchpad | `docs/screenshots/02-launchpad.png` |
| Executive Dashboard | `docs/screenshots/03-dashboard.png` |
| GIS / Locations | `docs/screenshots/04-map.png` |

---

## Current Quality Status

| Metric | Value |
|--------|-------|
| Tests passing | 553 |
| Tests failing | 0 |
| API routes | 225 |
| Schema version | 12 |
| Application version | 3.9.0 |
| Branch | `oxalpha/session-hardening-wave` |

---

## Security

| Control | Implementation |
|---------|---------------|
| Authentication | PBKDF2-SHA256, 600k rounds, automatic hash upgrade |
| Session tokens | 48-byte cryptographic randomness, SHA-256 digest storage |
| Login throttling | 3-tier database-backed (account+client, client-wide, account-global) |
| Authorization | Role-based + capability-overlay (40+ codes, narrowing only) |
| Production CSP | No inline scripts/styles, HSTS, COOP/COEP/COI |
| Trusted hosts | `EUAS_ALLOWED_HOSTS` required, wildcard `*` rejected |
| Input validation | `max_length` on all unbounded text fields |
| IDOR protection | Site-scoped queries, role-gated mutations |
| Concurrency | CAS on work orders, audit chain lock, inventory serialization |
| Audit chain | SHA-256 hash chain, row-level lock, tamper-evident |
| Secrets | Environment-sourced only, never hardcoded |
| Webhook signing | HMAC-SHA256 on outbound events |

---

## Reliability / KPI Support

| Metric | Description |
|--------|-------------|
| **SAIFI** | System Average Interruption Frequency Index |
| **SAIDI** | System Average Interruption Duration Index |
| **CAIDI** | Customer Average Interruption Duration Index |
| **ASAI** | Average Service Availability Index |

Contributor logic provides ranked interruption sources by customer-hours. WHY-red intelligence explains any red KPI through its contributing records with resolvable drill targets.

Bad-actor identification surfaces assets with repeated corrective completions. Deterioration signals track condition decline patterns.

---

## Quick Start

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

### Docker

```bash
# SQLite reference stack
docker compose up --build

# PostgreSQL stack
docker compose -f docker-compose.postgres.yml up --build
```

---

## Production Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `EUAS_ENV` | Deployment environment | `development` |
| `EUAS_DATABASE_URL` | PostgreSQL connection URL | unset (SQLite) |
| `EUAS_ALLOWED_HOSTS` | Trusted hostnames (comma-separated) | `localhost,127.0.0.1` |
| `EUAS_BOOTSTRAP_ADMIN_PASSWORD` | Admin bootstrap password | required in production |
| `EUAS_EVENT_WEBHOOK_URL` | Outbound webhook target | unset |
| `EUAS_EVENT_WEBHOOK_SECRET` | HMAC-SHA256 signing key | unset |
| `EUAS_SESSION_HOURS` | Session lifetime | `12` |
| `EUAS_MAX_UPLOAD_MB` | Upload size limit | `25` |
| `EUAS_AUTOMATION_INTERVAL_MINUTES` | Scheduler interval | `0` (disabled) |
| `EUAS_TRUSTED_PROXY_CIDRS` | Trusted proxy CIDRs | unset |

Production mode (`EUAS_ENV=production`) enforces: demo credential rejection, mandatory admin password rotation, strict CSP + HSTS, upload/introspection path blocking, trusted host validation.

---

## Testing

```bash
# Full regression suite
python -m pytest tests/ -q --tb=short

# Production readiness check
python scripts/production_readiness.py --strict-production --check-db

# HTTP smoke test
python scripts/smoke_test.py
```

---

## Repository Structure

```text
app/
  main.py              # FastAPI application, lifespan, route composition
  application.py       # Legacy route definitions (being decomposed)
  auth.py              # Password hashing, session management
  auth_store.py        # Session store, login throttling
  audit_store.py       # Tamper-evident audit chain
  authorization.py     # RBAC + capability overlay
  config.py            # Environment configuration
  database.py          # Database adapter, schema, migrations
  migrations.py        # Schema migrations, credential management
  production.py        # Production security middleware
  kpi_store.py         # KPI routes, snapshot management
  kpi_service.py       # KPI computation, explanation, risk scoring
  telemetry_store.py   # Telemetry channels, alarm lifecycle
  workflow_store.py    # Work-order transitions (CAS)
  reservation_store.py # Inventory reservations (locked)
  operations_store.py  # Operations command center
  ...                  # Additional domain stores
static/
  index.html           # SPA shell
  app.js               # Core application logic (25+ modules)
  styles.css           # Base design system
  ui-refresh.css       # Visual polish layer
  enterprise-ui.css    # Enterprise enhancement layer
  *.js                 # Enhancement modules (accessibility, UX, dashboard, etc.)
tests/
  test_*.py            # 553 tests covering all domains
docs/
  EUAS_CURRENT_STATUS.md
  EUAS_PRODUCTION_AUDIT.md
  EUAS_TEST_INTEGRITY.md
  EUAS_ARCHIVE_MANIFEST.md
  RELEASE_READINESS.md
  DATABASE_DEPLOYMENT_GUIDANCE.md
  POSTGRESQL_PORTABILITY.md
```

---

## Documentation

| Document | Purpose |
|----------|---------|
| [EUAS_CURRENT_STATUS.md](docs/EUAS_CURRENT_STATUS.md) | Current baseline and hardening changes |
| [EUAS_PRODUCTION_AUDIT.md](docs/EUAS_PRODUCTION_AUDIT.md) | Security headers, secrets, production mode |
| [EUAS_TEST_INTEGRITY.md](docs/EUAS_TEST_INTEGRITY.md) | Test contract verification |
| [RELEASE_READINESS.md](docs/RELEASE_READINESS.md) | Release-readiness assessment |
| [DATABASE_DEPLOYMENT_GUIDANCE.md](docs/DATABASE_DEPLOYMENT_GUIDANCE.md) | SQLite WAL, busy_timeout, migration safety |
| [POSTGRESQL_PORTABILITY.md](docs/POSTGRESQL_PORTABILITY.md) | PostgreSQL portability assessment |
| [APPLICATION_DECOMPOSITION_PLAN.md](docs/APPLICATION_DECOMPOSITION_PLAN.md) | application.py extraction plan |

---

## Roadmap / Remaining Technical Debt

- PostgreSQL migration path hardening
- `application.py` decomposition (23-phase extraction plan exists)
- Corporate OIDC/SAML + MFA integration
- Managed object storage and malware scanning
- HA/background-worker topology
- External observability and monitoring
- Enterprise GIS/SCADA adapters
- Controlled DR/retention procedures
- Richer frontend coverage for advanced analytics

---

## License

See repository root for license information.

---

## Status

**Release-readiness classification: CONDITIONAL — MERGE_READY with caveats**

The codebase passes 553 tests with 0 failures, has comprehensive production security hardening, and the UI has been enhanced with enterprise-grade design patterns. Single-tenant deployment model. All concurrency gaps are closed. Audit chain is tamper-evident.

Remaining blockers for production deployment: corporate SSO, managed infrastructure, external observability. These are deployment-environment concerns, not code-quality blockers.
