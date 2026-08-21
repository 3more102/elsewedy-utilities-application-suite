# EUAS Architecture

## 1. Design Goals

EUAS is organized around a unified operational data model rather than disconnected application silos. An asset can be linked to a site/location, work history, preventive maintenance, inspections, documents, vendor, meter readings, project work and cost. Work can consume labor and inventory. Low stock can generate procurement. Failed inspections can create corrective work. Every important write creates audit history.

## 2. Logical Layers

### Client layer

The EUAS SPA is a dependency-light responsive interface. It provides the application launchpad, navigation, forms, tables, filters, charts, dialogs, notifications, global search and field-optimized views. The manifest/service worker make the shell installable as a PWA. v4.3 adds authenticated offline field snapshots, a durable browser operation queue, idempotent replay and explicit server-side conflict resolution for supported technician mutations; closed-app OS background synchronization remains a roadmap item.

### API / domain layer

FastAPI owns:

- authentication/session validation
- RBAC enforcement
- request validation
- work lifecycle rules
- inventory movements
- procurement state changes
- PM generation
- inspection-to-corrective-work automation
- notifications
- audit history
- analytics calculations
- report generation
- document metadata/storage coordination

The UI never directly edits the database.

### Persistence layer

EUAS v4.4.0 uses a database adapter with two modes: SQLite for the zero-configuration reference deployment and PostgreSQL when `EUAS_DATABASE_URL` is supplied. Both modes share the same service/API boundary, relational business identifiers and workflow model. SQLite uses foreign keys/WAL/indexes; the PostgreSQL adapter translates bind syntax and reference DDL while preserving transactions and generated identifiers.

## 3. Module Boundaries

- **Foundation:** users, roles, permissions, sessions, sites, locations, vendors.
- **Assets:** asset types, assets, meters, meter readings.
- **Maintenance:** work orders, work tasks, labor, materials, maintenance plans.
- **Materials:** warehouses, inventory items, inventory transactions.
- **Procurement:** requisitions, requisition lines, quotations, purchase orders, PO lines.
- **Assurance:** inspections, inspection items, HSE incidents.
- **Delivery:** projects and project tasks.
- **Knowledge:** documents.
- **Platform:** notifications and audit logs.

## 4. Key Cross-Module Transactions

### Work execution

`Work Order → Labor Entry → Cost update`

`Work Order → Material Issue → Inventory decrease → Work actual cost update → Low-stock notification`

### Preventive maintenance

`Maintenance Plan due → Preventive Work Order → Plan next-due/last-generated update → Notification + Audit`

### Inspection failure

`Inspection submit → Failed item(s) → Corrective Work Order → Inspection corrective_wo_id → Notification + Audit`

### Replenishment

`Available stock <= reorder point → Reorder scan → Purchase Requisition → Approval → Supplier quotation → Purchase Order → Receipt → Inventory increase`

## 5. Authentication and Authorization

- Passwords are PBKDF2-SHA256 hashed with per-user random salts.
- Session tokens are cryptographically random.
- Sessions expire server-side after a configurable duration (12 hours by default).
- API routes apply role checks before writes.
- Repeated failed login attempts are throttled per client/user principal.
- Password changes require the existing password and revoke other sessions.
- Administrators can deactivate users, immediately revoking their sessions.
- API responses include correlation IDs and browser-oriented security headers.
- Attachment uploads use an extension allow-list, configurable size ceiling and randomized stored names.
- The UI mirrors permissions for usability, but API enforcement is authoritative.
- Audit records capture user, action, module, record, old/new value and timestamp.

## 6. Data Integrity

- Foreign keys are enabled for every connection.
- Business identifiers such as asset/work/PR/PO/document numbers are unique.
- Inventory changes are stored as transaction records.
- Parent-child asset and location relationships are explicit.
- Work order line items are normalized into task/labor/material tables.
- Database writes are transactionally committed/rolled back by the DB context manager.

## 7. Deployment Evolution

Reference:

```text
Browser/PWA → FastAPI → Database Adapter → SQLite OR PostgreSQL + attachment directory
```

Enterprise target:

```text
Web/PWA
  ↓ TLS
WAF / Load Balancer / API Gateway
  ↓
EUAS API replicas ── Redis / Job Scheduler
  ↓
PostgreSQL HA ── Object Storage ── GIS / SCADA / ERP integrations
  ↓
Central Audit / Logs / Metrics / Traces
```

The current service boundary keeps UI and domain rules independent from the future database and infrastructure upgrades.


## 8. Reference Deployment Security Boundary

EUAS keeps the reference deployment self-contained while adding a PostgreSQL runtime path and unified approval/workflow engine while enforcing server-side authentication/RBAC, transactional writes, upload controls, login throttling, audit history and security headers. The CSP currently permits inline script execution because the dependency-light frontend still generates some inline event handlers; the enterprise hardening path is to migrate those remaining handlers to delegated listeners and then remove `unsafe-inline`. See `SECURITY.md`.

The Docker reference runs as a non-root application user and Compose provides a health check and persistent database/upload volumes. Horizontal production scaling requires moving session/rate-limit state and persistence to shared services (for example Redis + PostgreSQL/object storage).


## Approval / Workflow Engine

EUAS introduces `approval_requests` and `workflow_events` as shared workflow primitives. Work-order submission creates an approval assigned to a supervisor or maintenance-management role. Purchase-requisition submission creates a procurement approval. Decisions update the originating business record, write workflow history, create audit entries and notify the requester. Rejected work can be corrected and resubmitted, creating a fresh approval request.

## Database Modes

See [DATABASE.md](DATABASE.md) for SQLite/PostgreSQL configuration, schema versioning and the live PostgreSQL preflight procedure. The build sandbox validates SQLite end-to-end and unit-tests PostgreSQL SQL translation; it does not contain a live PostgreSQL server.


## Automation and Operations Control

EUAS adds an explicit operations-control layer rather than hiding recurring logic inside UI actions. `job_runs` is the execution ledger. One automation transaction evaluates PM due conditions, inventory reorder requirements, overdue work, warranty/contract expiry and stale approvals. PM and reorder outputs feed the same approval/workflow primitives used by human-created records. Alert creation uses unread-record de-duplication.

The scheduler can be invoked through a protected API, an optional single-process interval, or `scripts/run_automation.py` under an external scheduler. External scheduling is the recommended clustered-production model because it gives one owner to recurring execution while web/API replicas remain stateless.

The observability surface consists of health/readiness endpoints, job-run history and a protected Prometheus-style metrics endpoint. SQLite reference deployments also support consistent backup/restore bundles; PostgreSQL deployments are expected to use PostgreSQL-native backup/PITR tooling.


## Service-Level Management

EUAS separates service-level policy from work-order business data. `sla_policies` maps work priority to response/resolution targets. `work_order_sla` stores the derived due clocks and measured result for each work order, while `sla_events` is an append-only breach ledger. This avoids mixing transient escalation state into the core work-order row and makes SLA reporting independently queryable.

The SLA clock is created when work is created (and backfilled for existing records). `start` records the first response and `complete` records resolution. The automation engine evaluates unresolved deadlines and creates de-duplicated escalation notifications.

## Durable Integration Events

`workflow_events` remains the human/business workflow history. In the current release each workflow transition also writes a separate `event_outbox` record. SLA policy changes and breaches publish events as well. The outbox record is committed in the same database transaction as the originating business action; delivery occurs later through the automation runner. This is the transactional-outbox pattern and prevents an external integration outage from rolling back maintenance/procurement work.

When `EUAS_EVENT_WEBHOOK_URL` is set, EUAS POSTs JSON events with event ID/type headers and an optional HMAC-SHA256 signature. Without a webhook, events are retained locally and marked `Skipped`, keeping the reference deployment network-independent.


## Governance & lifecycle evidence

EUAS adds a governance layer that remains transactional and queryable rather than UI-only. `audit_logs` records are linked by `prev_hash` and a deterministic SHA-256 `audit_hash`. The application can recompute the full chain and report the first invalid record if persisted audit content is modified. This is a **tamper-evident chain**, not a substitute for external notarization, database-level write-once controls or regulated WORM storage.

`maintenance_cost_ledger` posts cost events separately from the work-order aggregate. New labor and material consumption create ledger entries linked to both work order and asset, while seeded historical work cost is represented as historical ledger entries. This supports independent cost rollups and asset-lifetime maintenance analysis.

`report_snapshots` stores serialized asset-dossier point-in-time data plus a SHA-256 content hash. No update/delete report-snapshot API is exposed. A verification endpoint recomputes the hash to detect mutation. `backup_records` stores evidence for administrator-generated SQLite backup packages, including application version, byte size and returned SHA-256. `retention_policies` stores retention intent and powers a non-destructive eligibility preview; automatic purge is intentionally not performed by the reference build.


## Planning and asset-health layer

EUAS includes two service-level calculations above the core transactional model. The asset-health service computes a transparent score from asset condition/criticality plus operational evidence such as priority work, overdue work, failed inspections and SLA breaches. Snapshots are persisted separately in `asset_health_snapshots`, keeping the asset master free of derived scoring state.

The maintenance-planning service converts PM due dates and unresolved work into weekly demand buckets and compares estimated labor demand to active-technician capacity. The reference deployment uses a simple 40-hour-per-technician capacity assumption; production deployments should substitute craft calendars, shifts, leave, site rosters and skill constraints.

Approval delegation is implemented as a separate time-bounded authority table rather than modifying an approval's original assignee. Authorization checks evaluate the original assignment and any effective delegation server-side, preserving accountability and auditability.


## Workforce and reliability services (v3.7)

The planning service now derives weekly capacity from normalized workforce profiles, shifts, absences and efficiency factors. Work-order material and craft requirements provide readiness inputs without conflating planned demand with actual inventory issues or labor entries. Reliability is calculated as a reporting service over completed corrective/breakdown work, returning per-asset and per-site MTBF, MTTR and availability for a selectable analysis period. See `WORKFORCE_RELIABILITY.md` for formulas and limitations.


## Execution Coordination — v3.8.0

EUAS includes an execution-coordination layer between work planning and technician completion. `inventory_reservations` secures material for a specific work order before issue; generic inventory ISSUE/TRANSFER transactions are prevented from consuming reserved units. Reservation issue records flow through the normal inventory transaction, work-order material and maintenance-cost ledgers.

`dispatch_assignments` models Dispatched → Accepted → En Route → On Site → Completed/Cancelled. Only one active dispatch is allowed for a technician at a time. Arrival can start an assigned work order and records the SLA response milestone, while dispatch completion deliberately does not close the maintenance work itself.

`asset_outages` records forced/planned operational unavailability with explicit start/end timestamps, cause, impact and lost-capacity context. Reliability calculations use overlapping forced-outage duration when outage evidence exists and retain the older work-order-hours calculation only as a legacy fallback for assets without outage history.

## v4.0 Utility Command Center architecture

The v4.0 operations path introduces an explicit control-room coordination layer:

```text
SCADA / historian / edge gateway
        │
        │ HTTPS + X-EUAS-Integration-Key
        ▼
Telemetry Ingest API
  ├─ batch idempotency
  ├─ reading external-id de-duplication
  ├─ quality classification
  └─ time-series persistence
        │
        ├─ Good quality ──► threshold engine ──► suppression check
        │                                      │
        │                                      ├─ suppressed evidence only
        │                                      └─ operational alarm
        │                                               │
        │                                               ▼
        │                                      incident correlation
        │                                               │
        ▼                                               ▼
Telemetry trends / quality KPIs              Utility Command Center
                                                ├─ acknowledge
                                                ├─ incident → corrective WO
                                                ├─ outage context
                                                └─ technician dispatch context
```

This separates protocol adaptation from EUAS business logic. Native industrial protocols belong in an edge/integration adapter; EUAS receives normalized authenticated readings. The correlation engine is deliberately deterministic (same asset or configured topology neighbor + active correlation window) so an operator can explain why alarms were grouped.

Integration keys are stored only as SHA-256 digests. Human sessions and machine principals therefore use different authentication paths while converging on the same transaction, audit and event-outbox layers.


## v4.3 offline field synchronization

Field clients obtain an authenticated snapshot from `/api/field/sync/bootstrap`. Mutable field entities carry deterministic hashes over their workflow-relevant state. Offline operations are queued with unique operation IDs and later submitted to `/api/field/sync/push`. The server stores every operation before deciding Applied, Conflict or Rejected, and replay of the same operation ID is idempotent.

For mutable records, a stale base hash creates an explicit conflict instead of last-write-wins. Ordered edits to the same entity inside one push can rebase only when the first operation proves the original base hash was current at batch arrival. Conflict retry requires a fresh expected server hash, preventing a second race from being silently overwritten. See `FIELD_SYNC.md`.

## v4.2 topology-aware incident correlation

EUAS now maintains a directed operational graph separate from the asset parent/child hierarchy. A topology link means that an upstream asset operationally feeds, supplies, drives, contains or otherwise supports a downstream asset. The graph is intentionally configurable because physical asset hierarchy and operational dependency are not always the same.

When a new threshold alarm opens, the correlation service first preserves same-asset grouping and then looks for an open incident containing a directly connected topology neighbor at the same site inside the 30-minute correlation window. Incidents can therefore grow across multiple hops incrementally as adjacent alarmed assets join. This avoids automatically collapsing unrelated sibling assets simply because they share a distant common ancestor.

For a multi-asset incident, EUAS ranks alarmed assets deterministically using directed upstream reachability, first alarm onset and severity. It stores the selected candidate, score, reason and hop evidence on the incident. Corrective work generated from the incident targets that candidate. This is explainable root-cause **decision support**, not ML inference and not proof of physical causation.
