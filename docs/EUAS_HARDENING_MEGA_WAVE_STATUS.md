# EUAS Production Hardening Mega-Wave

## Continuation checkpoint

The mega-wave continues from the existing production-hardened implementation without replacing working architecture or rewriting repository history.

Current verified `main` checkpoint:

`6b36171fd6ff08b3c11291daed5410f0be42c78e`

Current focus order:

1. Immutable audit verification expansion
2. Durable event platform completion
3. Distributed worker reliability
4. Workflow state machine enforcement
5. Enterprise observability
6. Controlled database migration and PostgreSQL validation
7. Security final gate
8. Release candidate evidence
9. UI/runtime delivery integrity

## Verified implementation evidence

| Focus area | Evidence |
|---|---|
| Audit verification | `app/audit_verification.py` is the shared chain validator behind `/api/audit/integrity`, `/api/audit/replay` and `scripts/verify_audit.py`; replay refuses tampered chains with HTTP 409 |
| Disaster recovery | Backups record audit-chain evidence and restore refuses a snapshot whose recorded tamper-evident chain no longer validates |
| Security final gate | `scripts/production_readiness.py --check-db` fails deployment preflight when the persisted audit chain is broken (`audit_chain_integrity`) |
| Controlled schema migration | `app/migrations.py` owns the post-v9 migration registry, structural validation and migration status; PostgreSQL uses a transaction-scoped advisory lock and SQLite takes an immediate write transaction before migration execution |
| Migration safety | The runner refuses databases newer than the application and unregistered version gaps, validates recorded versions structurally, repairs the historical pre-claimed-v10 auth case, and requires the target schema contract to be ready before success |
| Migration operations | `scripts/migrate.py` provides `status`, `upgrade`, `check` and JSON output. SQLite CI executes `upgrade` then `check`; PostgreSQL CI verifies the migration contract after strict production readiness |
| Unified startup migration | ASGI startup, production readiness, external automation and engineering evidence all route through the shared migration registry. The historical auth startup function remains only as a compatibility wrapper preserving its prior return shape |
| Durable events | Operator retry of an attempt-exhausted outbox event atomically resets its delivery budget; automated runs never reset attempts |
| Durable-event idempotency | A retry of an already `Delivered` event is a terminal idempotent no-op, preventing the operator surface from creating a second external side effect |
| Durable outbox leases | Outbox claims are committed before outbound I/O, active claims carry a bounded lease, stale claims are reclaimable, and exact generation fencing prevents an older sender from overwriting a newer delivery generation |
| Outbox observability | `/api/events/outbox/status` and Prometheus gauges expose retryable, queued, active-lease, stale-lease, exhausted and unresolved backlog plus oldest backlog age without exposing payload/error text |
| Distributed scheduler | The in-process automation scheduler uses a database-backed cross-replica singleton gate; the PostgreSQL gate is covered by a 12-worker concurrency smoke |
| Scheduler failover | Duplicate suppression follows the latest scheduler generation. A newer failed run is not hidden by an older recent success, so peer failover remains immediate |
| Workflow consistency | Dispatch transitions transactionally validate and lock the linked work-order lifecycle; work-task toggles and work-order note appends use conditional claims to reject concurrent stale writers without duplicate audit evidence |
| Outage lifecycle | Outage close claims exactly one terminal generation with a conditional update; concurrent/repeated closes cannot duplicate close audits or `asset.outage.closed` events, and the asset-status restore runs after the claim in the same transaction |
| Bounded operational reads | `/api/outages` uses bounded `limit`/`offset` pagination while preserving filters, deterministic ordering, response shape, authorization ceiling and complete CSV export behavior |
| Approval domain ownership | The unified approval queue and delegation APIs are owned by `app/approval_store.py` alongside the atomic decision route; paths, models and role ceilings are unchanged |
| PM domain ownership | The maintenance-plan API surface (list/create/generate) is owned by `app/pm_store.py`; behavior and the WRITE_ROLES ceiling are unchanged |
| Automation read ownership | Automation status and run-history reads are owned by `app/automation_read_store.py` with paths, handler/OpenAPI identity, query bounds, response shape, role ceiling and capability overlay preserved |
| PostgreSQL validation | Mandatory CI includes connectivity, strict production readiness, schema migration contract validation, inventory, audit, procurement, reservation, workflow, dispatch/work-state, dispatch assignment/redispatch, transfers, PM, alarms, inspections, business numbers, reorder, outbox, scheduler and telemetry concurrency smokes plus live HTTP smoke |
| Security scanning | Required security workflow includes Python dependency audit, CodeQL, production container smoke and high/critical vulnerability scanning |
| Frontend static validation | CI syntax-checks `static/app.js`, `static/ux-enhancements.js`, `static/dashboard-enhancements.js`, `static/productivity-enhancements.js`, `static/operational-enhancements.js` and `static/sw.js`, and parses `static/manifest.webmanifest` as JSON before merge |
| UI accessibility/interaction | The application shell includes skip navigation, dialog/live-region semantics, responsive/focus treatment, keyboard search navigation, modal/drawer focus management, Escape handling, mobile navigation scrim and dashboard semantic grouping without changing API/business behavior |
| Dense-data productivity | Sticky table headers/filter toolbars, horizontal-overflow affordances, keyboard table focus, Ctrl/Cmd+K global-search focus, required/invalid field semantics, grouped sidebar modules, module finder and Alt+M navigation improve dense operational workflows without changing routing or data behavior |
| Operational feedback safeguards | The UI adds network activity/offline recovery feedback, duplicate-submit protection, actionable root empty/error states, toast severity semantics and accessible retry/status behavior in a standalone enhancement layer |
| Release evidence | `scripts/engineering_evidence.py` measures the composed, fully migrated runtime against an isolated fresh SQLite database and `ENGINEERING_EVIDENCE.json` is checked in CI for drift |

## Current measured engineering snapshot

The deterministic evidence snapshot currently checked into `main` is:

- application version: `3.9.0`
- schema contract: `10`
- unique `/api/` paths: `150`
- `/api/` route-method pairs: `168`
- relational tables: `63`
- explicit indexes: `41`
- source test definitions: `193`

PR #65 validated the migration framework on exact head `9389d360d96ac4d0d903d65716ad05d3ffb3e18a` with EUAS CI #202 and EUAS Security #174. Both SQLite Python jobs passed migration `upgrade`/`check`, deterministic evidence, readiness tests and full regression; PostgreSQL 16 passed strict readiness, migration contract validation, the configured concurrency smokes and live HTTP smoke; Security passed CodeQL, dependency audit, container smoke and high/critical vulnerability scanning.

PR #66 validated ASGI startup unification on exact head `67f477a3f5921be4d5b2b5ec03ae3adb1f5ff1d5` with EUAS CI #204 and EUAS Security #176, including both SQLite suites and the complete PostgreSQL integration sequence.

PR #67 validated the operational-feedback UI layer on exact head `d241cb1ca0edb09f9746d44b49588818f72fcb77` with EUAS CI #207 and EUAS Security #179 before its merge onto the already-hardened current `main`.

## Integrated milestones

- PR #41 — audit recovery, outbox retry, backup integrity and observability hardening
- PR #42 — terminal-idempotent retry semantics for delivered outbox events
- PR #43 — cross-replica singleton scheduled automation
- PR #44 — linked work-order state enforcement for dispatch transitions
- PR #45 — latest-generation scheduler failover semantics
- PR #46 — deterministic release engineering evidence and drift gate
- PR #47 — telemetry temporal-integrity port
- PR #48 — telemetry temporal-integrity reconciliation, outage terminality, approval/PM domain ownership, dependency/action maintenance
- PR #49 — atomic work-task toggle transition claims
- PR #50 — compare-and-set work-order note append
- PR #51 — bounded outage pagination
- PR #52 — durable outbox delivery leases and generation fencing
- PR #53 — payload-free outbox operational snapshot
- PR #54 — outbox lease/backlog Prometheus gauges
- PR #55 — automation read-model decomposition without API/OpenAPI drift
- PR #57 — refreshed EUAS application shell and accessibility layer
- PR #58 — keyboard/mobile interaction and focus-management enhancement layer
- PR #59 — executive dashboard signal-clarity enhancement layer
- PR #61 — permanent frontend static syntax/manifest CI gate
- PR #63 — dense-data productivity and form/table accessibility enhancements
- PR #64 — grouped sidebar navigation and accessible module discovery
- PR #65 — controlled schema migration registry, CLI, readiness integration and SQLite/PostgreSQL migration gates
- PR #66 — ASGI startup unification with the shared migration registry
- PR #67 — operational network/offline, submit-safety, state and toast feedback layer

Superseded or duplicate unmerged branches/PRs are not counted as integrated milestones, including PR #56, PR #60 and PR #68.

The historical authorization ceiling remains unchanged: capability overlays are narrowing controls and are not permitted to grant a role access that the historical role did not already have.
