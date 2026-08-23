# Elsewedy Utilities Application Suite — EUAS

<p align="center">
  <strong>One Platform. Every Asset. Every Operation.</strong><br>
  <sub>Enterprise Asset, Maintenance, Utility Operations & Field Service Platform</sub>
</p>

<p align="center">
  <a href="https://github.com/3more102/elsewedy-utilities-application-suite/actions/workflows/ci.yml"><img alt="EUAS CI" src="https://github.com/3more102/elsewedy-utilities-application-suite/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-4.9.0-0A6EBD">
  <img alt="Schema" src="https://img.shields.io/badge/schema-v23-334155">
  <img alt="API Endpoints" src="https://img.shields.io/badge/API_endpoints-242-2563EB">
  <img alt="Tests" src="https://img.shields.io/badge/regression_tests-109-16A34A">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB">
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#core-capabilities">Capabilities</a> ·
  <a href="COMMAND_CENTER.md">Command Center</a> ·
  <a href="ACCESS_CONTROL.md">Access Control</a> ·
  <a href="RELIABILITY_FMEA.md">Reliability / FMEA</a> ·
  <a href="RCM_STRATEGIES.md">RCM Strategies</a> ·
  <a href="ARCHITECTURE.md">Architecture</a> ·
  <a href="MODULAR_ARCHITECTURE.md">Modular Architecture</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

**Developed by Omar & Seif**

EUAS is an original enterprise asset, maintenance, utility operations and field-service platform designed for electrical, water, infrastructure and facilities operations. It is inspired by the category of capabilities found in enterprise EAM/CMMS suites, but uses its own branding, data model, UI, workflows and implementation.

> This repository is a runnable reference application, not a static mockup. It includes authentication, a relational database, business workflows, APIs, responsive UI, demo data, automated regression tests and container/local deployment options.

## UI

### Application Launchpad

![EUAS Application Launchpad](docs/screenshots/02-launchpad.png)

### Executive Dashboard

![EUAS Executive Dashboard](docs/screenshots/03-dashboard.png)

### GIS / Location Management

![EUAS GIS View](docs/screenshots/04-map.png)

### Login

![EUAS Login](docs/screenshots/01-login.png)

The screenshots are representative management-demo captures from the EUAS interface. **v4.9.0** adds governed Reliability-Centered Maintenance strategy models on top of the v4.8 FMEA, v4.7 CBM and existing preventive-maintenance/work-governance layers.

---


## v4.9 Reliability-Centered Maintenance Strategy Engine

- One governed RCM strategy per asset FMEA record, preserving the FMEA as the failure-analysis source of truth
- Consequence classes: Safety, Environmental, Operational, Non-Operational and Hidden
- Strategy types: Condition-Based, Time-Based, Run-to-Failure, Failure-Finding and Redesign
- Deterministic strategy guards: Safety/Environmental consequences cannot use Run-to-Failure; Condition-Based and Time-Based strategies require valid same-asset active CBM/PM links before submission/activation
- Workflow: Draft → Review → electronically evidenced Approval → Active, with formal Continue / Revise / Retire reviews
- Four-eyes approval through the existing Approval Center plus the critical `reliability.rcm.approve` permission
- Risk-based default review cadence derived from the linked FMEA risk band
- RCM coverage/overdue/critical-gap KPIs, Prometheus-style metrics, CSV export and Reliability UI register
- Revision returns the strategy to Draft and requires a new independent approval cycle before reactivation

The RCM decision logic is transparent reference-application governance. EUAS v4.9 does **not** claim SAE JA1011/JA1012 certification, IEC/ISO conformance, or a validated safety-case methodology. See [RCM_STRATEGIES.md](RCM_STRATEGIES.md).

## v4.8 Reliability & FMEA Linkage

- Hierarchical failure-mode taxonomy with cycle prevention
- Asset-specific FMEA records with effects, causes, controls and recommended actions
- Server-calculated Severity × Occurrence × Detectability RPN and deterministic risk bands
- Formal FMEA review history preserving old/new ratings and reviewer evidence
- Governed FMEA-to-work conversion through the existing approval workflow
- Optional same-asset CBM→FMEA→event→work traceability
- Fine-grained `reliability.fmea.manage` permission
- Reliability/FMEA UI, CSV exports and operational metrics

The default risk bands are reference application thresholds, not a claim of conformance to an OEM or industry-specific FMEA scoring standard. See [RELIABILITY_FMEA.md](RELIABILITY_FMEA.md).

## v4.7 Condition-Based Maintenance Rule Editor

- Governed CBM rule authoring against existing telemetry channels
- Deterministic operators: `>=`, `>`, `<=`, `<`, inside range and outside range
- **Good-quality telemetry only**; Uncertain/Bad samples never advance CBM trigger state
- Configurable consecutive-reading filter to reduce single-sample noise
- Configurable cooldown to prevent repeated maintenance generation
- Recommendation-only or governed **Condition-Based Maintenance work-order** action
- Auto-generated work enters `Submitted` state and routes through the existing Maintenance Manager approval workflow
- Traceable CBM event lifecycle with acknowledge, manual resolve and automatic resolve when the condition clears
- Side-effect-free rule test endpoint for authoring validation
- CBM metrics, CSV exports, ingestion-batch outcome counts and Telemetry UI queues

This is deterministic rules-based condition monitoring, not a predictive-maintenance/ML claim. See [CBM_RULES.md](CBM_RULES.md).

## v4.6 Fine-Grained Permission Administration

- Server-enforced permission catalog with category, risk level and descriptions
- Dynamic role-permission grants administered without code changes
- Explicit per-user **Allow / Deny / Inherit** overrides, with optional expiry
- Deterministic precedence: active user override before role grant; deny by default
- Current-password re-authentication, change reason and exact `UPDATE ACCESS` confirmation for access changes
- Lockout guards for the last active administrator and core access-administration permissions
- Effective-permission API, Administration role matrix, user override UI, metrics and CSV governance export
- Audit and outbox evidence for role, grant and user-override changes

Permissions control whether an operation may be attempted; they do not bypass record assignment, approval routing, delegation, four-eyes or other domain rules. See [ACCESS_CONTROL.md](ACCESS_CONTROL.md).

## v4.5 Governed Retention Execution

- Preview and explicit destructive retention runs with durable run history
- Current-password re-authentication plus exact `EXECUTE RETENTION` confirmation for destructive execution
- Class-wide or record-scoped legal holds, with credential-verified release
- Protected evidence classes are never purged by the application retention executor
- Transaction-safe purge support for notifications and integration-event outbox records
- Document binary lifecycle is deliberately blocked until coordinated object-storage deletion exists
- Hash-chained canonical run manifests with integrity verification
- Downloadable evidence ZIP containing manifest, item counts and chain verification
- CSV retention-run export for governance reporting

The evidence package is suitable for transfer to an external immutable/WORM repository, but the reference application does **not** claim that its local SQLite/PostgreSQL storage is WORM.

## v4.4 Approval Signature Evidence

- Approval/rejection requires current-password re-authentication for the acting signer
- Explicit signer-intent statement bound to the approval record code
- Decision-time record snapshot preserved inside canonical evidence payload
- Global SHA-256 evidence chain with previous-hash linkage and verifier
- Direct versus delegated approval authority recorded in the evidence
- Approval Center displays signed decisions and evidence details
- Governance dashboard verifies both audit and approval-signature chains
- Protected CSV evidence export and Prometheus-style signature metrics
- Approval-signature retention class is protected by policy metadata

This is application-level **electronic approval evidence**, not a PKI digital signature, qualified e-signature, WORM store, or a claim of regulatory certification. See [APPROVAL_SIGNATURES.md](APPROVAL_SIGNATURES.md).

## v4.3 Offline Field Synchronization

- Authenticated field bootstrap snapshots with deterministic entity hashes
- Durable user-scoped browser queue for offline technician changes
- Idempotent operation IDs so reconnect retries never duplicate a mutation
- Conflict-safe work transitions, checklist updates, asset readings/condition and dispatch transitions
- Append-only offline field notes
- Safe ordered-batch rebase for sequential offline edits created from one snapshot
- Explicit **Keep Server** / **Retry Mine** conflict resolution with current-hash verification
- Session-expiry-bounded offline reopen using the last authenticated field snapshot
- Field sync metrics and protected CSV evidence export

Inventory issue and file upload remain online-only because they require current server authority. Closed-app OS background sync is still a roadmap item. See [FIELD_SYNC.md](FIELD_SYNC.md).

## v4.2 Topology-Aware Root-Cause Correlation

- Directed asset-topology registry (`TPL-*`) with upstream → downstream operational dependencies
- Cycle prevention for active topology links
- Alarm incidents can grow across directly connected assets within the existing 30-minute correlation window
- Multi-hop incidents form incrementally as adjacent alarmed assets join the same incident
- Explainable deterministic root-cause candidate based on upstream reachability, alarm onset and severity
- Incident evidence stores correlation mode, root-cause score, reason and topology hop count
- Incident-generated corrective work targets the current root-cause candidate instead of only the first alarmed asset
- Command Center topology view, topology KPIs, Prometheus metrics and protected CSV exports
- Browser runtime fix for v4.1 actionable/shelved Command Center queue bindings

The root-cause result is an **operator decision-support candidate**, not an ML diagnosis. The reasoning remains transparent and traceable. See [COMMAND_CENTER.md](COMMAND_CENTER.md).

## v4.1 Governed Alarm Shelving

- Four-eyes approval for temporary alarm shelves
- Critical-alarm duration cap and automatic expiry
- Actionable versus shelved operator queues without changing alarm lifecycle state
- Audit/outbox evidence, metrics and CSV export

## v4.0 Utility Command Center

- Utility Command Center combining incidents, alarms, outages, field dispatch and telemetry quality
- Idempotent telemetry batches (`TIB-*`) and per-reading external IDs
- Good / Uncertain / Bad data-quality handling; non-good readings are retained but do not drive threshold alarms
- Time-bounded alarm suppression windows (`SUP-*`) for maintenance, testing and commissioning
- Deterministic asset/time-window alarm correlation into operational incidents (`INC-*`)
- Incident acknowledgement, automatic resolution when member alarms clear, and one-click corrective-work generation
- Bucketed telemetry series and 24-hour data-quality KPIs
- `telemetry:write` integration API keys for SCADA/API gateways; plaintext shown once, digest stored, expiry/revoke/last-use supported
- Command-center metrics and CSV evidence for incidents, suppressions and ingestion batches
- `scripts/scada_gateway_demo.py` for authenticated M2M telemetry demonstration

See [COMMAND_CENTER.md](COMMAND_CENTER.md) for the current control-room, shelving and topology-correlation model.

## v3.9 Utility Operations Intelligence

- Asset-linked telemetry channels with metric, unit and source-system metadata
- Timestamped bulk telemetry ingestion with quality/source tracking
- Warning/critical high and low thresholds
- Operational alarm lifecycle: Open → Acknowledged → Cleared → Closed
- Repeated threshold violations update the existing active alarm instead of duplicating it
- Alarm → corrective work-order generation with SLA, approval, audit and integration-event linkage
- Executive and Operations KPIs for active/critical alarms and stale telemetry
- Asset detail telemetry/alarm history, global search, metrics and CSV exports

The delivered ingestion interface is **SCADA-style API ingestion**, not a live OPC-UA, Modbus, IEC 61850 or vendor-SCADA connector. Threshold evaluation is deterministic rules-based logic, not ML anomaly detection. See `UTILITY_INTELLIGENCE.md`.

## v3.8 Execution Coordination

- Work-order material reservations with partial issue and release lifecycle
- Reserved-stock protection against unrelated generic issue/transfer transactions
- Technician dispatch board with ETA, acceptance, en-route and on-site milestones
- One-active-dispatch-per-technician guard
- Technician assignment enforcement for material consumption
- Explicit planned/forced asset outages with lost-capacity context
- Outage-driven MTBF, MTTR and availability when outage evidence exists
- Dispatch, outage and reservation CSV exports, search results and operational metrics

See `EXECUTION_COORDINATION.md` for the execution model.

## Core Capabilities

EUAS currently integrates these applications inside one suite:

- **Home / Application Launchpad** — unified entry point for all operational applications.
- **Executive Dashboard** — assets, availability, work, PM, inventory, procurement, HSE, cost, MTBF and MTTR KPIs with interactive charts, site/date filters and recent activity.
- **Asset Management** — asset registry, hierarchy, condition, criticality, location, vendor, warranty, meter data, rule-based health score/history, maintenance history, cost ledger, lifecycle timeline, dossier snapshots and CRUD/export.
- **Work Management** — corrective, preventive, emergency, inspection and project work with lifecycle, assignment, checklists, labor, materials, notes, signatures, attachments and reports.
- **Preventive Maintenance & Planning** — calendar, meter/runtime and condition-oriented plans, automatic due-work generation and a 90-day workload/capacity forecast with parts and craft readiness.
- **Workforce Planning** — technician profiles, crafts, home sites, shift assignments, approved absences, productive-efficiency factors and weekly capacity.
- **Inventory** — item master, warehouses, available/reserved stock, reorder controls, issue/return/receipt/adjustment/transfer and transaction history.
- **Procurement** — purchase requisitions, Draft → Submit → Approval, supplier quotations/RFQ stage, purchase orders and receipt into stock.
- **Approval Center** — unified work-order and procurement approval queue with assigned role/user, temporary approval delegation, approve/reject, comments and workflow history.
- **Automation & Reports** — auditable PM/reorder/alert/SLA routines, run history, protected CSV exports, observability, retention preview, audit-chain verification and SQLite backup tooling.
- **Governance & Lifecycle** — SHA-256 linked audit records, retention-policy registry/eligibility preview, backup evidence, maintenance cost ledger, asset timeline and tamper-evident dossier snapshots.
- **Service Levels (SLA)** — priority-based response/resolution policies, per-work-order due clocks, compliance states, breach events and escalation alerts.
- **Integration Event Outbox** — durable workflow/SLA events with retry state and optional signed webhook delivery for external ESB/iPaaS integrations.
- **Utilities Operations** — operational portfolio view across electrical, water and infrastructure domains with telemetry intelligence, warnings, active alarms and outage context.
- **Telemetry & Alarms** — asset telemetry channels, timestamped readings, threshold evaluation, alarm acknowledgement/clear/close and corrective-work generation.
- **GIS / Locations** — site map and Region → City → Site → Building → Floor → Room → Asset hierarchy.
- **Field Service** — mobile/tablet-oriented technician work queue with start/pause/complete, readings, condition updates, parts, notes, checklist, photo/document attachment and technician signature.
- **Inspections** — configurable digital inspection forms; failed inspections can generate corrective work automatically.
- **Safety & HSE** — hazards, incidents, near misses, inspections/corrective actions with severity, probability and calculated risk score.
- **Projects** — project manager, dates, budget, actual cost, progress and tasks.
- **Vendors & Contracts** — supplier/OEM master and linked commercial agreements.
- **Documents** — documents linked to assets, work orders, locations, projects and vendors.
- **Analytics** — per-asset/site MTBF, MTTR and availability, work trends, backlog, workforce capacity, maintenance cost, procurement spend, inventory health, approval outcomes and HSE analytics.
- **Notifications** — user, role and global notifications with unread tracking.
- **Global Search** — connected results for assets, related work orders, documents and inspections.
- **Administration** — users, roles and audit trail.

---

## Required Demo Scenario

The seed data contains the management demonstration requested for **New Cairo Substation**:

- **Asset:** `TR-001 — 33/11 kV Power Transformer`
- **Condition:** `Warning`
- **Work Order:** `WO-10025 — Investigate Transformer Oil Temperature`
- **Priority:** `High`
- **Assigned Technician:** Mahmoud Ali (`tech1`)
- **Supervisor:** Ahmed Nabil (`supervisor`)
- **Inspection:** `INS-5001 — Transformer Inspection`
- **PM:** `PM-TR-001 — Transformer Quarterly Inspection`
- **Linked documents:** transformer datasheet and quarterly inspection report metadata
- **Inventory:** transformer oil-filter spare stock at New Cairo warehouse

This data is connected across Dashboard → GIS → Asset → Work → Field Service → Inventory → Inspection → Audit → Analytics.

---

## Architecture

```text
┌──────────────────────────────────────────────────────────┐
│                 EUAS Responsive Web / PWA                │
│  Launchpad · Dashboard · Assets · Work · Field · HSE    │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTPS / JSON / Multipart
┌──────────────────────────▼───────────────────────────────┐
│                     FastAPI API Layer                    │
│ Auth · RBAC · Validation · Workflows · Search · Reports │
│ Notifications · Audit · SLA · Events · Analytics       │
└──────────────────────────┬───────────────────────────────┘
                           │ transactional repository access
┌──────────────────────────▼───────────────────────────────┐
│              Database Adapter / File Store               │
│ SQLite reference mode OR PostgreSQL production mode      │
│ Schema versioning · FK/indexes · attachment store        │
└──────────────────────────────────────────────────────────┘
```

EUAS defaults to **SQLite** so the delivered application is immediately runnable with zero external infrastructure. EUAS also implements a **PostgreSQL runtime adapter** selected with `EUAS_DATABASE_URL`, plus a PostgreSQL Docker Compose deployment. SQLite is fully regression-tested in this build; the PostgreSQL SQL-translation contract is unit-tested, while a live PostgreSQL server was not available in the build sandbox. See [DATABASE.md](DATABASE.md). Operational automation, observability and backup procedures are documented in [OPERATIONS.md](OPERATIONS.md). SLA/event integration is documented in [INTEGRATIONS.md](INTEGRATIONS.md). Governance and evidence controls are documented in [GOVERNANCE.md](GOVERNANCE.md). Planning, asset-health scoring, workforce capacity, parts readiness and approval delegation are documented in [PLANNING.md](PLANNING.md) and [WORKFORCE_RELIABILITY.md](WORKFORCE_RELIABILITY.md). The current operational correlation, topology, shelving, suppression and M2M ingestion layer is documented in [COMMAND_CENTER.md](COMMAND_CENTER.md).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deeper design.

---

## Technology Stack

| Layer | Implementation |
|---|---|
| Frontend | Responsive HTML5 + modern JavaScript + CSS design system |
| PWA | Web App Manifest + Service Worker shell caching |
| API | FastAPI 0.128+ |
| Validation | Pydantic |
| Authentication | PBKDF2-SHA256 passwords + random bearer sessions + configurable server-side expiry + login throttling |
| Authorization | Role-based access control enforced in API dependencies and UI capabilities |
| Database | SQLite reference mode + PostgreSQL runtime adapter, schema versioning, foreign keys and indexes |
| File storage | Local randomized attachment store for the reference deployment |
| Tests | Pytest + FastAPI TestClient / HTTPX |
| Container | Docker + Docker Compose |
| API docs | OpenAPI / Swagger at `/api/docs` |

The project contains **83 relational tables**, **76 explicit performance indexes** and **242 application HTTP routes** in v4.9.0 (241 under `/api/` plus the application root).

---

## Database Architecture

Major entity relationships:

```mermaid
erDiagram
    ROLES ||--o{ USERS : grants
    USERS ||--o{ SESSIONS : owns
    SITES ||--o{ LOCATIONS : contains
    LOCATIONS ||--o{ LOCATIONS : parent
    LOCATIONS ||--o{ ASSETS : hosts
    ASSET_TYPES ||--o{ ASSETS : classifies
    ASSETS ||--o{ ASSETS : parent
    VENDORS ||--o{ ASSETS : supports
    ASSETS ||--o{ METERS : has
    METERS ||--o{ METER_READINGS : records
    ASSETS ||--o{ WORK_ORDERS : receives
    WORK_ORDERS ||--o{ WORK_ORDER_TASKS : contains
    WORK_ORDERS ||--o{ LABOR_ENTRIES : consumes
    WORK_ORDERS ||--o{ WORK_ORDER_MATERIALS : consumes
    ASSETS ||--o{ MAINTENANCE_PLANS : schedules
    WAREHOUSES ||--o{ INVENTORY_ITEMS : stocks
    INVENTORY_ITEMS ||--o{ INVENTORY_TRANSACTIONS : records
    PURCHASE_REQUISITIONS ||--o{ PURCHASE_REQUISITION_ITEMS : contains
    PURCHASE_REQUISITIONS ||--o{ QUOTATIONS : receives
    PURCHASE_REQUISITIONS ||--o{ PURCHASE_ORDERS : sources
    PURCHASE_ORDERS ||--o{ PURCHASE_ORDER_ITEMS : contains
    ASSETS ||--o{ INSPECTIONS : inspected
    INSPECTIONS ||--o{ INSPECTION_ITEMS : contains
    PROJECTS ||--o{ PROJECT_TASKS : contains
    VENDORS ||--o{ CONTRACTS : signs
    USERS ||--o{ NOTIFICATIONS : receives
    USERS ||--o{ AUDIT_LOGS : creates
    USERS ||--o| TECHNICIAN_PROFILES : extends
    CRAFTS ||--o{ TECHNICIAN_PROFILES : qualifies
    SITES ||--o{ TECHNICIAN_PROFILES : homes
    USERS ||--o{ TECHNICIAN_SHIFT_ASSIGNMENTS : scheduled
    SHIFT_TEMPLATES ||--o{ TECHNICIAN_SHIFT_ASSIGNMENTS : defines
    USERS ||--o{ TECHNICIAN_ABSENCES : unavailable
    WORK_ORDERS ||--o{ WORK_ORDER_REQUIREMENTS : plans
    INVENTORY_ITEMS ||--o{ WORK_ORDER_REQUIREMENTS : supplies
    WORK_ORDERS ||--o{ WORK_ORDER_CRAFT_REQUIREMENTS : needs
    CRAFTS ||--o{ WORK_ORDER_CRAFT_REQUIREMENTS : supplies
```

The implemented schema also links documents to assets, work orders, locations, projects and vendors, and links procurement back to work/projects where applicable. The operations layer stores `sla_policies`, `work_order_sla`, `sla_events` and `event_outbox` as first-class records. Governance stores `maintenance_cost_ledger`, `report_snapshots`, `backup_records` and `retention_policies`, while `audit_logs` carries a linked SHA-256 chain. v3.6.0 added `approval_delegations` and `asset_health_snapshots`; v3.7.0 adds workforce crafts/profiles/shifts/absences plus planned work-order material and craft requirements.

---

## Work Order Lifecycle

```text
Draft → Submitted → Approved → Assigned → In Progress → Completed → Closed
                                            ↘ pause ↗
```

Transition rules are validated server-side. Work execution can update labor, material consumption, checklist state, readings, condition, notes, completion notes and technician signature. Inventory consumption is transactional and updates actual work cost.

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

The automatic reorder scan detects stock at/below the reorder threshold and creates purchase requisitions while preventing duplicate active replenishment PRs for the same item.

---

## Preventive Maintenance

EUAS supports maintenance-plan triggers for:

- Calendar interval
- Runtime / meter interval
- Usage-style meter rules
- Condition-oriented planning fields

The generation service finds due plans, creates preventive work orders, advances the calendar due date when appropriate, records `last_generated`, creates audit records and produces planning notifications.

---

## Authentication & RBAC

Demo passwords are hashed at seed time using PBKDF2-SHA256. Plain-text passwords are not stored in the database. Authenticated sessions use cryptographically random bearer tokens and configurable server-side expiry. EUAS also throttles repeated failed logins, supports profile updates/password changes, can revoke other sessions, and immediately removes sessions when an administrator deactivates a user.

Example roles included:

- System Administrator
- Asset Manager
- Maintenance Manager
- Maintenance Planner
- Supervisor
- Technician
- Storekeeper
- Procurement Officer
- HSE Officer
- Project Manager
- Executive Viewer

Write APIs are role-protected. The executive account is read-oriented and is blocked from asset creation and other restricted writes. Important API responses also include request IDs and security headers, while document uploads use a configurable extension allow-list and size limit.

> The included credentials are demo credentials only. Replace them and integrate corporate SSO/IdP before any real deployment. See [SECURITY.md](SECURITY.md) for implemented controls and the enterprise hardening checklist.

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

---

## Quick Start

### Windows

1. Extract the project.
2. Double-click:

```text
run_windows.bat
```

3. Open:

```text
http://127.0.0.1:8000
```

### Linux / macOS

```bash
chmod +x run_linux_mac.sh
./run_linux_mac.sh
```

Then open `http://127.0.0.1:8000`.

### Manual Development Start

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

Swagger/OpenAPI documentation:

```text
http://127.0.0.1:8000/api/docs
```

Health endpoint:

```text
http://127.0.0.1:8000/api/health
```

---

## Environment Variables

Copy `.env.example` if you want a custom local configuration.

| Variable | Purpose | Default |
|---|---|---|
| `EUAS_DB_PATH` | SQLite database path | `./euas.db` |
| `EUAS_DATABASE_URL` | PostgreSQL connection URL; when set, PostgreSQL mode is selected | unset |
| `EUAS_HOST` | documented host preference | `127.0.0.1` |
| `EUAS_PORT` | documented port preference | `8000` |
| `EUAS_ENV` | deployment environment label | `development` |
| `EUAS_VERSION` | reported application version | `4.8.0` |
| `EUAS_SESSION_HOURS` | server-side session lifetime | `12` |
| `EUAS_MAX_UPLOAD_MB` | maximum uploaded document size | `25` |
| `EUAS_AUTOMATION_INTERVAL_MINUTES` | optional single-node scheduler interval; `0` disables | `0` |
| `EUAS_ALLOWED_DOC_SUFFIXES` | comma-separated document extension allow-list | common engineering/document formats |
| `EUAS_EVENT_WEBHOOK_URL` | optional integration-event destination | unset |
| `EUAS_EVENT_WEBHOOK_SECRET` | HMAC-SHA256 shared secret for outbound event signing | unset |
| `EUAS_OUTBOX_MAX_ATTEMPTS` | maximum automated delivery attempts for a failed event | `5` |

The included runner scripts use `127.0.0.1:8000` directly.

---

## Database Setup / Reset

No manual schema command is needed. On first application startup EUAS:

1. Creates the schema and indexes.
2. Creates roles and permissions.
3. Seeds demo users.
4. Seeds interconnected utility sites, locations and assets.
5. Seeds work, PM, inventory, procurement, approvals/workflow history, HSE, projects, contracts, documents and notifications.
6. Records the current database schema version in `schema_migrations`.

To reset the demo locally, stop the app and delete `euas.db`, then start EUAS again.

---

## Automated Testing

Run:

```bash
pytest -q
```

The regression suite includes operations-center, SLA/outbox and governance/lifecycle coverage. `test_zzz_governance_lifecycle.py` validates cost posting, asset timeline, report snapshots, retention preview, backup evidence and audit tamper detection.

The regression test validates the required connected workflows, including:

1. Login → asset → work order → lifecycle → technician start → labor → part issue → note → meter/condition → completion/signature → supervisor close → work-order report.
2. Low stock → reorder detection → automatic purchase requisition → approval → supplier quotation → PO → receipt.
3. Overdue PM → generated preventive work order.
4. Failed inspection → generated corrective work order.
5. Full checklist persistence.
6. Warehouse-to-warehouse stock transfer.
7. Asset CRUD.
8. HSE creation/risk score.
9. Vendor/contract flow.
10. Global search linkage for `TR-001`.
11. Viewer RBAC denial.
12. Server-side session expiration.
13. Read access across the main integrated applications.
14. Profile update, strong password change and other-session revocation.
15. Administrator activation/deactivation with login denial for inactive users.
16. HSE lifecycle/risk update.
17. Project task creation/update with automatic project-progress recalculation.
18. Upload allow-list rejection for executable files.
19. Login throttling after repeated failures.
20. Security response headers and request IDs.
21. Unified Approval Center for work orders and purchase requisitions.
22. Approval reject → correction → resubmit workflow.
23. Technician execution guard for work assigned to another user.
26. Database readiness/schema-version and release-consistency checks.
25. PostgreSQL SQL/DDL adapter translation contract.
26. SLA breach escalation and durable/signed event-outbox delivery.
27. Maintenance cost ledger posting for labor and material usage.
28. Asset lifecycle timeline and asset-dossier report snapshot generation.
29. Report-snapshot SHA-256 verification.
30. Retention-policy eligibility preview and RBAC.
31. Backup evidence registry with returned ZIP SHA-256.
32. Audit hash-chain validation and deliberate tamper detection/recovery.
33. Shift/absence/efficiency-driven workforce capacity.
34. Workforce-profile RBAC and site/craft assignment.
35. Work-order planned-spares readiness: Ready → Shortage.
36. Craft-hour demand versus craft capacity in the 90-day forecast.
37. Per-asset and per-site 365-day MTBF/MTTR/availability calculations.

Latest release QA status is recorded in [TEST_REPORT.md](TEST_REPORT.md).

For a clean-process HTTP verification that starts a temporary Uvicorn server and fresh database:

```bash
python scripts/smoke_test.py
```

---

## Docker Deployment

SQLite reference container:

```bash
docker compose up --build
```

PostgreSQL stack:

```bash
docker compose -f docker-compose.postgres.yml up --build
```

The containers run EUAS as a non-root user and include HTTP readiness checks. PostgreSQL mode uses `EUAS_DATABASE_URL`; uploaded documents remain on a persistent volume. For production, place EUAS behind TLS/WAF, use enterprise SSO, secrets management, managed object storage, backups and centralized observability.

---

## Project Structure

```text
EUAS/
├── app/
│   ├── auth.py             # Password hashing, sessions, RBAC
│   ├── config.py           # Paths/application configuration
│   ├── database.py         # Relational schema + realistic demo seed
│   └── main.py             # API, workflows, analytics and reports
├── static/
│   ├── index.html          # EUAS shell/login/top navigation
│   ├── app.js              # Integrated module UI and interactions
│   ├── styles.css          # Original responsive EUAS design system
│   ├── manifest.webmanifest
│   └── sw.js               # PWA shell cache
├── tests/
│   ├── test_workflows.py   # Connected regression workflow suite
│   └── test_database_adapter.py # PostgreSQL translation contract
├── scripts/
│   ├── smoke_test.py       # Fresh-process HTTP startup/security smoke
│   ├── postgres_preflight.py # Live target PostgreSQL connectivity check
│   └── verify_audit.py      # Audit hash-chain verification CLI
├── docs/
│   ├── DEMO_SCRIPT.md
│   └── screenshots/
├── Dockerfile
├── docker-compose.yml
├── docker-compose.postgres.yml
├── requirements.txt
├── run_windows.bat
├── run_linux_mac.sh
├── ARCHITECTURE.md
├── DATABASE.md
├── OPERATIONS.md
├── GOVERNANCE.md
├── PLANNING.md
├── TEST_REPORT.md
├── SECURITY.md
└── README.md
```

---

## Production Hardening Roadmap

The reference application is intentionally self-contained. For a real Elsewedy enterprise rollout, the next hardening phase should include:

1. Validate the PostgreSQL runtime path against the target managed/HA PostgreSQL service and adopt a dedicated migration framework for controlled releases.
2. Azure AD / Entra ID or another corporate OIDC/SAML identity provider with MFA.
3. Object storage for attachments and malware scanning.
4. API gateway, TLS, distributed rate limiting and secret manager.
5. Redis-backed distributed sessions/cache/jobs.
6. Background scheduler for PM generation, escalations, SLA and notification delivery.
7. Offline-first field synchronization and conflict resolution.
8. Enterprise GIS provider integration and layer management.
9. SCADA/IoT ingestion adapters and time-series storage.
10. Advanced failure-code taxonomy, permits, risk assessments and safety workflows.
11. Approval matrices and delegation rules.
12. PostgreSQL row-level/multi-organization controls where needed.
13. Central logs, tracing, metrics, alerting, external audit anchoring/WORM storage where required, and approved retention/purge procedures.
14. CI/CD with SAST, dependency scanning, container scanning and environment promotion.
15. Backup, restore, DR, retention and compliance controls.

---

## Independence / Branding

EUAS is an independent application. It does not contain IBM Maximo proprietary source code, protected graphics, proprietary text or copied layouts. Its architecture, database, workflows, brand treatment and source implementation are original to this project.
