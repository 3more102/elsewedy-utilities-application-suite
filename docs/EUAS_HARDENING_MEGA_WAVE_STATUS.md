# EUAS Production Hardening Mega-Wave

## Continuation checkpoint

The mega-wave continues from the existing production-hardened implementation without replacing working architecture or rewriting repository history.

Current verified `main` checkpoint:

`84347c9fb989e7d948e757a868e1a5768cc9bbcc`

Current focus order:

1. Immutable audit verification expansion
2. Durable event platform completion
3. Distributed worker reliability
4. Workflow state machine enforcement
5. Enterprise observability
6. PostgreSQL validation expansion
7. Security final gate
8. Release candidate evidence
9. UI/runtime delivery integrity

## Verified implementation evidence

| Focus area | Evidence |
|---|---|
| Audit verification | `app/audit_verification.py` is the shared chain validator behind `/api/audit/integrity`, `/api/audit/replay` and `scripts/verify_audit.py`; replay refuses tampered chains with HTTP 409 |
| Disaster recovery | Backups record audit-chain evidence and restore refuses a snapshot whose recorded tamper-evident chain no longer validates |
| Security final gate | `scripts/production_readiness.py --check-db` fails deployment preflight when the persisted audit chain is broken (`audit_chain_integrity`) |
| Durable events | Operator retry of an attempt-exhausted outbox event atomically resets its delivery budget; automated runs never reset attempts |
| Durable-event idempotency | A retry of an already `Delivered` event is a terminal idempotent no-op, preventing the operator surface from creating a second external side effect |
| Durable outbox leases | Outbox claims are committed before outbound I/O, active claims carry a bounded lease, stale claims are reclaimable, and exact generation fencing prevents an older sender from overwriting a newer delivery generation |
| Outbox observability | `/api/events/outbox/status` and Prometheus gauges expose retryable, queued, active-lease, stale-lease, exhausted and unresolved backlog plus oldest backlog age without exposing payload/error text |
| Distributed scheduler | The in-process automation scheduler uses a database-backed cross-replica singleton gate; the PostgreSQL gate is covered by a 12-worker concurrency smoke |
| Scheduler failover | Duplicate suppression follows the latest scheduler generation. A newer failed run is not hidden by an older recent success, so peer failover remains immediate |
| Workflow consistency | Dispatch transitions transactionally validate and lock the linked work-order lifecycle; work-task toggles and work-order note appends use conditional claims to reject concurrent stale writers without duplicate audit evidence |
| Outage lifecycle | Outage close claims exactly one terminal generation with a conditional update; concurrent/repeated closes cannot duplicate close audits or `asset.outage.closed` events, and the asset-status restore runs after the claim in the same transaction |
| Bounded operational reads | `/api/outages` now uses bounded `limit`/`offset` pagination while preserving filters, deterministic ordering, response shape, authorization ceiling and complete CSV export behavior |
| Approval domain ownership | The unified approval queue and delegation APIs are owned by `app/approval_store.py` alongside the atomic decision route; paths, models and role ceilings are unchanged |
| PM domain ownership | The maintenance-plan API surface (list/create/generate) is owned by `app/pm_store.py`; behavior and the WRITE_ROLES ceiling are unchanged |
| Automation read ownership | Automation status and run-history reads are owned by `app/automation_read_store.py` with paths, handler/OpenAPI identity, query bounds, response shape, role ceiling and capability overlay preserved |
| PostgreSQL validation | Mandatory CI includes inventory, audit, procurement, reservation, workflow, dispatch/work-state, dispatch assignment/redispatch, transfers, PM, alarms, inspections, business numbers, reorder, outbox, scheduler and telemetry concurrency smokes plus live HTTP smoke |
| Security scanning | Required security workflow includes Python dependency audit, CodeQL, production container smoke and high/critical vulnerability scanning |
| Frontend static validation | CI now syntax-checks `static/app.js`, `static/ux-enhancements.js`, `static/dashboard-enhancements.js` and `static/sw.js`, and parses `static/manifest.webmanifest` as JSON before merge |
| UI accessibility/interaction | The application shell includes skip navigation, dialog/live-region semantics, responsive/focus treatment, keyboard search navigation, modal/drawer focus management, Escape handling, mobile navigation scrim and dashboard semantic grouping without changing API/business behavior |
| Release evidence | `scripts/engineering_evidence.py` measures the composed runtime against an isolated fresh SQLite database and `ENGINEERING_EVIDENCE.json` is checked in CI for drift |

## Current measured engineering snapshot

The release-evidence collector runs in GitHub Actions on both supported SQLite Python jobs. The latest CI-measured snapshot on the PR #61 merge candidate is:

- application version: `3.9.0`
- schema contract: `10`
- unique `/api/` paths: `150`
- `/api/` route-method pairs: `168`
- relational tables: `61`
- explicit indexes: `38`
- source test definitions: `190`

The SQLite Python 3.11 full regression suite on that exact candidate reports `190 passed, 1 warning`; Python 3.12 also passes. PostgreSQL 16 integration passes the strict production-readiness gate, all configured concurrency smokes, and the live HTTP smoke. The frontend static-validation job passes. EUAS Security passes CodeQL, Python dependency audit, production container smoke and the high/critical vulnerability scan.

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

Superseded, intentionally unmerged branches/PRs are not counted as integrated milestones (including PR #56 and PR #60).

The historical authorization ceiling remains unchanged: capability overlays are narrowing controls and are not permitted to grant a role access that the historical role did not already have.
