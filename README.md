# Elsewedy Utilities Application Suite — EUAS

**One Platform. Every Asset. Every Operation.**

**Developed by Omar & Seif**

EUAS is an original **suite of integrated enterprise applications** for asset management, maintenance, utility operations, field service, inventory, procurement, HSE, projects, governance and operational intelligence across electrical, water, infrastructure and facilities environments. It is a runnable multi-app reference suite—not one monolithic application—and includes authentication, relational data, business workflows, APIs, responsive UI, realistic demo data, automated regression coverage and container/local deployment paths.

> EUAS uses its own branding, data model, UI, workflows and source implementation. It is not based on proprietary EAM/CMMS source code.

---

## Current Engineering Status

The current **`main` head** is:

```text
main: 56796502e4e5be61ea612a7d63e7ea24428cf7f1
```

This is the merge of **PR #143 — expand canonical maintenance trend and WHY adapters**, validated on exact PR head `bb8892f64aedfaec06cfdf7924bc7e9825908170` by **EUAS CI #415** and **EUAS Security #380** before merge.

Immediately preceding production hardening on `main` also includes:

- **PR #144 — canonicalize production request IDs**: preserves one compact valid client `X-Request-ID`; missing, duplicated, overlong or malformed values are replaced with a server-generated 128-bit hexadecimal ID.
- **PR #142 — complete production cross-origin isolation**: adds COEP `require-corp` and `Origin-Agent-Cluster: ?1` alongside same-origin COOP/CORP policy.
- **PR #141 — hide FastAPI introspection routes in production**: Swagger/OpenAPI/OAuth introspection surfaces return non-cacheable `404` responses in production while development documentation remains available.
- **PR #139 — post-reconciliation operations integration**: DR/audit hardening, expedite bridge, command strip and scoped executive export integration.

### Generated engineering evidence

The repository-generated `ENGINEERING_EVIDENCE.json` at this checkpoint reports:

| Evidence | Current value |
|---|---:|
| Application version | `3.9.0` |
| API routes | `186` |
| API route-method registrations | `204` |
| Relational tables | `67` |
| Explicit indexes | `45` |
| Schema version | `12` |
| Source test definitions | `391` |
| Reference database backend | `sqlite` |

These values are generated from the repository tree; they are not manually estimated release claims.

### Production-hardening coverage

| Area | Hardening completed |
|---|---|
| Authentication | Digest-only bearer storage, legacy-session migration, persistent login throttling, password-hash upgrades, session metadata/revocation |
| Authorization | Capability overlays that can only narrow historical route-role access; structural mutation-contract coverage across migrated domains |
| Inventory | Lost-update protection, guarded stock issue, CAS adjustment, reservation serialization, deadlock-safe warehouse transfer and transfer idempotency |
| Audit | Serialized tamper-evident SHA-256 audit-chain appends, replay/verification support and PostgreSQL bootstrap protection |
| Procurement | Atomic requisition approval, PO creation and PO receipt; direct/unified approval coordination |
| Work / Dispatch | Atomic workflow transitions, one active technician dispatch, redispatch generation protection |
| Preventive Maintenance | Serialized due-plan generation with one generated work order/approval/workflow/audit side-effect set |
| Alarm → Work | One corrective work order per alarm, with globally serialized work-order number allocation |
| Inspections | Terminal/idempotent inspection submission and one corrective work order for concurrent failing submissions |
| Alarm Lifecycle | Atomic acknowledge/close transitions that cannot regress a closed alarm or duplicate close evidence |
| Business Numbers | Global deadlock-safe generated-number coordinator protecting `WO-`, `JOB-`, `PM-`, `APR-` and other `next_no()` families |
| Integration Outbox | Exact-generation delivery claims, generation-aware retry, post-commit webhook dispatch and explicit at-least-once crash semantics |
| Production HTTP | Request-ID canonicalization, production introspection suppression, security headers and cross-origin isolation |

`main` is the canonical integrated branch. Feature branches are promoted only after their exact heads pass the required validation gates and are merged.

---

## Executive & Maximo-Style KPI Intelligence

EUAS exposes one canonical executive KPI computation layer and reuses it across snapshots, drill-downs, trend views, WHY/explanation surfaces and exports. Adapter layers do **not** duplicate KPI formulas.

### Canonical executive KPI surface

Current routes include:

- `GET /api/kpi/executive` — scoped executive KPI snapshot with materialized caching and source-watermark freshness checks.
- `GET /api/kpi/backlog/risk` — explainable risk-weighted maintenance backlog.
- `GET /api/kpi/deterioration` — deterministic condition-deterioration signals.
- `GET /api/kpi/parts/shortages` — exact material shortages blocking open work.
- `GET /api/kpi/pm-risk` — high-criticality PM work landing in forecast over-capacity weeks.
- `GET /api/kpi/hse` — safety/incident KPIs from real HSE records; unavailable metrics are reported as unavailable instead of estimated.
- `GET /api/kpi/assets/{asset_id}` — per-asset KPI dossier.
- `GET /api/kpi/trend` — chronological samples for one canonical KPI over deterministic windows.
- `GET /api/kpi/explanation` — WHY/contributor view for supported canonical metrics.
- `GET /api/exports/executive-kpis.csv` — authorized scoped export using the same materialized snapshot pipeline.

### Maintenance intelligence available to trend / WHY adapters

The canonical maintenance family currently includes adapter coverage for:

- open work orders
- overdue work orders
- emergency work orders
- high-risk overdue work orders
- unassigned critical work orders
- backlog hours
- backlog weeks
- PM compliance
- schedule compliance
- MTBF
- MTTR
- repeat failure rate

Maintenance contributor logic is restricted to metrics where overdue-work drivers are semantically relevant, preventing misleading explanations for unrelated KPI families.

### Scope consistency

Executive filters are consistently applied across the canonical KPI service:

```text
period/window
site
region
asset type
criticality
```

Snapshot, trend, explanation and export paths reuse the same scoped computation contracts so management cards and drill-downs do not silently diverge.

---

## Verified CI / Security Gates

Every promoted hardening PR is validated against its exact head before merge. The required matrix includes:

- SQLite full regression on Python 3.11
- SQLite full regression on Python 3.12
- PostgreSQL 16 integration
- strict production-readiness checks
- inventory concurrency smoke
- audit-chain concurrency smoke
- procurement concurrency smoke
- reservation/material concurrency smoke
- workflow transition concurrency smoke
- dispatch assignment concurrency smoke
- dispatch redispatch concurrency smoke
- inventory transfer concurrency smoke
- preventive-maintenance generation concurrency smoke
- alarm work-order concurrency smoke
- inspection submission concurrency smoke
- alarm lifecycle concurrency smoke
- business-number concurrency smoke
- outbox-delivery concurrency smoke
- live PostgreSQL HTTP smoke
- CodeQL Python security analysis
- `pip-audit`
- production container build/health smoke
- Trivy HIGH/CRITICAL vulnerability gate
- built-image production security behavior checks

**Live PostgreSQL 16 is part of required CI evidence.**

---

## UI

### Application Launchpad

![EUAS Application Launchpad](docs/screenshots/02-launchpad.png)

### Executive Dashboard

![EUAS Executive Dashboard](docs/screenshots/03-dashboard.png)

### GIS / Location Management

![EUAS GIS View](docs/screenshots/04-map.png)

### Login

![EUAS Login](docs/screenshots/01-login.png)

The screenshots are representative management-demo captures from the EUAS v3.x interface.

---

## Application Suite

EUAS is organized as a **launchpad of integrated applications** sharing identity, authorization, data, workflows, audit, notifications and reporting while retaining distinct operational responsibilities.

The suite currently includes:

- **Home / Application Launchpad** — unified entry point for all EUAS apps.
- **Executive Dashboard** — assets, availability, work, PM, inventory, procurement, HSE, cost, MTBF, MTTR and operational-risk KPIs.
- **Asset Management** — asset hierarchy, condition, criticality, location, vendor, warranty, meter data, health score/history, maintenance history, lifecycle timeline and dossier snapshots.
- **Work Management** — corrective, preventive, emergency, inspection and project work with lifecycle, assignment, checklists, labor, materials, notes, attachments and signatures.
- **Preventive Maintenance & Planning** — calendar, meter/runtime and condition-oriented plans, automatic due-work generation and workload/capacity forecasting.
- **Workforce Planning** — technician profiles, crafts, shifts, absences, productive-efficiency factors and weekly capacity.
- **Inventory** — warehouses, available/reserved stock, reorder controls, issue/return/receipt/adjustment/transfer and transaction history.
- **Procurement** — requisitions, approvals, RFQ/quotation stage, purchase orders and receipt into stock.
- **Approval Center** — unified work-order and procurement approval queue with delegation, comments and workflow history.
- **Automation & Reports** — PM/reorder/alert/SLA routines, run history, protected exports, observability and governance evidence.
- **Governance & Lifecycle** — linked SHA-256 audit records, retention preview, backup evidence, cost ledger, timeline and tamper-evident report snapshots.
- **Service Levels** — priority-based response/resolution policies, due clocks, compliance states, breach events and escalation alerts.
- **Integration Event Outbox** — durable workflow/SLA events with retry state and optional signed webhook delivery.
- **Utilities Operations** — portfolio-level operational view across electrical, water and infrastructure domains.
- **Telemetry & Alarms** — asset telemetry channels, timestamped readings, threshold evaluation, alarm lifecycle and corrective-work generation.
- **Condition Intelligence** — deterministic condition and deterioration views connected to assets, maintenance risk and executive KPI context.
- **GIS / Locations** — Region → City → Site → Building → Floor → Room → Asset hierarchy.
- **Field Service** — mobile/tablet technician queue with lifecycle actions, readings, condition updates, parts, notes, checklist, attachments and signature.
- **Inspections** — configurable digital inspection forms with optional automatic corrective-work generation.
- **Safety & HSE** — hazards, incidents, near misses, inspections/corrective actions and risk scoring.
- **Projects** — project manager, dates, budget, actual cost, progress and tasks.
- **Vendors & Contracts** — supplier/OEM master and linked commercial agreements.
- **Documents** — documents linked to assets, work orders, locations, projects and vendors.
- **Analytics** — MTBF, MTTR, availability, backlog, schedule/PM compliance, workforce capacity, maintenance cost, procurement spend, inventory health, approvals and HSE analytics.
- **Notifications** — user, role and global notifications with unread tracking.
- **Global Search** — connected results across assets and related operational records.
- **Administration** — users, roles, permissions and audit trail.

---

## Utility Operations Intelligence

EUAS includes SCADA-style API telemetry ingestion with:

- asset-linked channels and source metadata
- timestamped readings with quality/source tracking
- warning/critical high and low thresholds
- operational alarm lifecycle: `Open → Acknowledged → Cleared → Closed`
- active-alarm occurrence tracking
- alarm → corrective work-order generation
- operational KPIs, search and CSV exports

The included interface is **SCADA-style API ingestion**, not a live OPC-UA, Modbus, IEC 61850 or vendor-SCADA connector. Threshold evaluation is deterministic rules-based logic, not ML anomaly detection. See [UTILITY_INTELLIGENCE.md](UTILITY_INTELLIGENCE.md).

---

## Execution Coordination

The execution layer includes:

- work-order material reservations with partial issue/release lifecycle
- reserved-stock protection
- technician dispatch with ETA, acceptance, en-route and on-site milestones
- one-active-dispatch-per-technician enforcement
- technician assignment checks for material consumption
- planned/forced asset outages
- outage-driven MTBF/MTTR/availability context
- PM capacity-risk linkage to forecast demand/capacity buckets

See [EXECUTION_COORDINATION.md](EXECUTION_COORDINATION.md).

---

## Required Demo Scenario

The seed data includes the connected management demonstration for **New Cairo Substation**:

| Item | Demo value |
|---|---|
| Asset | `TR-001 — 33/11 kV Power Transformer` |
| Condition | `Warning` |
| Work Order | `WO-10025 — Investigate Transformer Oil Temperature` |
| Priority | `High` |
| Technician | Mahmoud Ali (`tech1`) |
| Supervisor | Ahmed Nabil (`supervisor`) |
| Inspection | `INS-5001 — Transformer Inspection` |
| PM | `PM-TR-001 — Transformer Quarterly Inspection` |
| Inventory | transformer oil-filter spare stock at New Cairo warehouse |

The data is connected across Dashboard → GIS → Asset → Work → Field Service → Inventory → Inspection → Audit → Analytics.

---

## Architecture

```text
┌──────────────────────────────────────────────────────────┐
│                 EUAS Application Suite UI                │
│ Launchpad · Assets · Work · Inventory · HSE · GIS · ... │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTPS / JSON / Multipart
┌──────────────────────────▼───────────────────────────────┐
│              Shared FastAPI Service Layer                │
│ Auth · RBAC · Capabilities · Workflows · Reports        │
│ Notifications · Audit · SLA · Events · Analytics · KPI  │
└──────────────────────────┬───────────────────────────────┘
                           │ transactional repository access
┌──────────────────────────▼───────────────────────────────┐
│              Database Adapter / File Store               │
│ SQLite reference mode OR PostgreSQL production mode      │
│ Schema/indexes · concurrency controls · attachment store │
└──────────────────────────────────────────────────────────┘
```

The current reference implementation deploys the integrated apps through one shared web/API runtime, but the **product model is a suite of apps**, not one application. The shared runtime provides common identity, permissions, data consistency, audit, workflows and integrations across those apps.

EUAS defaults to **SQLite** for zero-infrastructure local use. Setting `EUAS_DATABASE_URL` selects the PostgreSQL runtime adapter. The repository includes a PostgreSQL Docker Compose deployment and required live PostgreSQL 16 CI validation.

See [ARCHITECTURE.md](ARCHITECTURE.md), [DATABASE.md](DATABASE.md), [OPERATIONS.md](OPERATIONS.md), [GOVERNANCE.md](GOVERNANCE.md), [AUTHORIZATION.md](AUTHORIZATION.md) and [SECURITY.md](SECURITY.md).

---

## Authorization Model

Effective protected access follows this invariant:

```text
authenticated
AND historical route role allowed
AND mapped capability allowed
AND existing resource/workflow/business rules allowed
```

Capabilities are **narrowing controls only**. Granting a capability does not allow a role to exceed the historical route-role ceiling.

Structural tests verify migrated mutation routes remain covered and that permission defaults match legacy role access.

---

## Concurrency / Transaction Integrity

Production hardening deliberately targets PostgreSQL read-committed races and multi-session behavior. Important invariants include:

- conditional state-transition claims instead of stale read → unconditional write
- canonical database lock ordering for multi-row mutations
- one active dispatch per technician
- one PM work order per due generation cycle
- one corrective work order per alarm/inspection terminal transition
- atomic stock/reservation/material issue accounting
- deadlock-safe inventory transfer
- serialized audit-chain head updates
- globally serialized generated business-number allocation
- post-commit integration-event delivery with one sender per event generation
- business mutation and its audit/workflow/approval/inventory side effects remain in the same transaction

The regression suite contains both deterministic SQLite tests and real PostgreSQL multi-session concurrency smokes with bounded thread joins to expose deadlocks.

---

## Production HTTP Security

Production-mode behavior includes:

- canonical `X-Request-ID` handling with generated fallback for malformed/missing values
- production suppression of `/api/docs`, OpenAPI and OAuth introspection surfaces
- same-origin cross-origin policies plus COEP `require-corp`
- `Origin-Agent-Cluster: ?1`
- non-cacheable blocked introspection responses
- built-container security behavior validation in CI

Development mode retains local API documentation where intended.

---

## Technology Stack

| Layer | Implementation |
|---|---|
| Frontend | Responsive HTML5 + modern JavaScript + CSS design system |
| PWA | Web App Manifest + Service Worker shell caching |
| API | FastAPI |
| Validation | Pydantic |
| Authentication | PBKDF2-SHA256 passwords + hardened bearer-session lifecycle |
| Authorization | Legacy RBAC ceiling + capability-based narrowing controls |
| Database | SQLite reference mode + PostgreSQL production runtime adapter |
| File storage | Local randomized attachment store for reference deployment |
| Tests | Pytest + FastAPI TestClient/HTTPX + PostgreSQL concurrency smokes |
| Container | Docker + Docker Compose |
| API docs | OpenAPI / Swagger in development; introspection surfaces hidden in production |

---

## Work Order Lifecycle

```text
Draft → Submitted → Approved → Assigned → In Progress → Completed → Closed
                                            ↘ pause ↗
```

Transitions are validated server-side and hardened with atomic expected-state claims where concurrency can produce duplicate or conflicting side effects.

---

## Procurement Flow

```text
Purchase Requisition
        ↓
     Approval
        ↓
Supplier RFQ / Quotations
        ↓
  Purchase Order
        ↓
      Receipt
        ↓
 Inventory increase
```

Requisition submission/approval, PO creation and PO receipt use atomic transition claims so stock and workflow side effects cannot be duplicated by concurrent requests.

---

## Quick Start

### Windows

```text
run_windows.bat
```

Open `http://127.0.0.1:8000`.

### Linux / macOS

```bash
chmod +x run_linux_mac.sh
./run_linux_mac.sh
```

### Manual development start

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Development Swagger/OpenAPI: `http://127.0.0.1:8000/api/docs`

Health: `http://127.0.0.1:8000/api/health`

---

## Docker

SQLite reference stack:

```bash
docker compose up --build
```

PostgreSQL stack:

```bash
docker compose -f docker-compose.postgres.yml up --build
```

Production deployments should place EUAS behind TLS/WAF, replace demo credentials, use managed secrets/object storage and integrate enterprise identity/observability.

---

## Automated Testing

Local regression:

```bash
pytest -q
```

Fresh-process HTTP smoke:

```bash
python scripts/smoke_test.py
```

Production readiness:

```bash
python scripts/production_readiness.py --strict-production --require-postgres --check-db
```

The CI workflow additionally runs the real PostgreSQL concurrency and built-image security smokes listed in **Verified CI / Security Gates** above.

---

## Demo Accounts

| Role | Username | Password |
|---|---|---|
| System Administrator | `omar` | `EUAS@2026` |
| Maintenance Manager | `seif` | `EUAS@2026` |
| Maintenance Planner | `planner` | `Planner@2026` |
| Supervisor | `supervisor` | `Supervisor@2026` |
| Technician | `tech1` | `Tech@2026` |
| Storekeeper | `store` | `Store@2026` |
| Procurement Officer | `proc` | `Proc@2026` |
| HSE Officer | `hse` | `HSE@2026` |
| Executive Viewer | `exec` | `Viewer@2026` |

> These credentials are demo-only. Replace them and integrate corporate SSO/IdP before real deployment.

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `EUAS_DB_PATH` | SQLite database path | `./euas.db` |
| `EUAS_DATABASE_URL` | PostgreSQL connection URL | unset |
| `EUAS_ENV` | deployment environment label | `development` |
| `EUAS_VERSION` | reported suite version | `3.9.0` |
| `EUAS_SESSION_HOURS` | server-side session lifetime | `12` |
| `EUAS_MAX_UPLOAD_MB` | upload size limit | `25` |
| `EUAS_AUTOMATION_INTERVAL_MINUTES` | scheduler interval; `0` disables | `0` |
| `EUAS_EVENT_WEBHOOK_URL` | optional event webhook destination | unset |
| `EUAS_EVENT_WEBHOOK_SECRET` | HMAC-SHA256 webhook signing secret | unset |
| `EUAS_OUTBOX_MAX_ATTEMPTS` | maximum automated delivery attempts | `5` |

See `.env.example` for the complete configuration surface.

---

## Repository Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — suite/system design
- [DATABASE.md](DATABASE.md) — database/runtime adapter
- [AUTHORIZATION.md](AUTHORIZATION.md) — capability and role model
- [SECURITY.md](SECURITY.md) — security controls and hardening guidance
- [OPERATIONS.md](OPERATIONS.md) — operations and automation
- [INTEGRATIONS.md](INTEGRATIONS.md) — SLA/event integration
- [GOVERNANCE.md](GOVERNANCE.md) — governance/evidence controls
- [PLANNING.md](PLANNING.md) — planning model
- [WORKFORCE_RELIABILITY.md](WORKFORCE_RELIABILITY.md) — workforce/capacity model
- [EXECUTION_COORDINATION.md](EXECUTION_COORDINATION.md) — dispatch/reservation/outage model
- [UTILITY_INTELLIGENCE.md](UTILITY_INTELLIGENCE.md) — telemetry/alarm model
- [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) — recovery documentation
- [TEST_REPORT.md](TEST_REPORT.md) — QA evidence
- [docs/EUAS_HARDENING_MEGA_WAVE_STATUS.md](docs/EUAS_HARDENING_MEGA_WAVE_STATUS.md) — hardening wave status/history

---

## Production Roadmap

Remaining enterprise-level work includes corporate OIDC/SAML + MFA, managed object storage/malware scanning, API gateway/TLS/distributed controls, production migration tooling, HA/background-worker topology, external observability, enterprise GIS/SCADA adapters, controlled DR/retention procedures and environment-promotion pipelines.

The hardening program continues to prioritize demonstrated integrity/security defects and operational-intelligence gaps with exact-head CI evidence before merge.

---

## Independence / Branding

EUAS is an independent **suite of integrated applications**. It does not contain proprietary source code, protected graphics, proprietary text or copied layouts from other commercial EAM/CMMS products. Its architecture, data model, workflows, branding and source implementation are original to this project.
