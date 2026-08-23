# EUAS Production Hardening Mega-Wave

## Continuation checkpoint

The mega-wave continues from the existing production-hardened implementation without replacing working architecture or rewriting repository history.

Main entering the release-evidence automation wave:

`22d2b7f32a1b4451271af32540f90b08c7e94fde`

Current focus order:

1. Immutable audit verification expansion
2. Durable event platform completion
3. Distributed worker reliability
4. Workflow state machine enforcement
5. Enterprise observability
6. PostgreSQL validation expansion
7. Security final gate
8. Release candidate evidence

## Verified implementation evidence

| Focus area | Evidence |
|---|---|
| Audit verification | `app/audit_verification.py` is the shared chain validator behind `/api/audit/integrity`, `/api/audit/replay` and `scripts/verify_audit.py`; replay refuses tampered chains with HTTP 409 |
| Disaster recovery | Backups record audit-chain evidence and restore refuses a snapshot whose recorded tamper-evident chain no longer validates |
| Security final gate | `scripts/production_readiness.py --check-db` fails deployment preflight when the persisted audit chain is broken (`audit_chain_integrity`) |
| Durable events | Operator retry of an attempt-exhausted outbox event atomically resets its delivery budget; automated runs never reset attempts |
| Durable-event idempotency | A retry of an already `Delivered` event is a terminal idempotent no-op, preventing the operator surface from creating a second external side effect |
| Outbox observability | `euas_outbox_attempt_exhausted` and the `outbox_exhausted` queue gauge expose undeliverable backlog |
| Distributed scheduler | The in-process automation scheduler uses a database-backed cross-replica singleton gate; the PostgreSQL gate is covered by a 12-worker concurrency smoke |
| Scheduler failover | Duplicate suppression follows the latest scheduler generation. A newer failed run is not hidden by an older recent success, so peer failover remains immediate |
| Workflow consistency | Dispatch transitions transactionally validate and lock the linked work-order lifecycle; stale dispatch arrival after work completion/closure rolls back cleanly |
| Outage lifecycle | Outage close claims exactly one terminal generation with a conditional update; concurrent/repeated closes can no longer duplicate close audits or `asset.outage.closed` events, and the asset-status restore runs after the claim in the same transaction |
| Approval domain ownership | The unified approval queue and delegation APIs are owned by `app/approval_store.py` alongside the atomic decision route; paths, models and role ceilings are unchanged |
| PM domain ownership | The maintenance-plan API surface (list/create/generate) is owned by `app/pm_store.py`; behavior and the WRITE_ROLES ceiling are unchanged |
| PostgreSQL validation | Mandatory CI includes inventory, audit, procurement, reservation, workflow, dispatch/work-state, dispatch assignment/redispatch, transfers, PM, alarms, inspections, business numbers, reorder, outbox, scheduler and telemetry concurrency smokes plus live HTTP smoke |
| Security scanning | Required security workflow includes Python dependency audit, CodeQL, production container smoke and high/critical vulnerability scanning |
| Release evidence | `scripts/engineering_evidence.py` measures the composed runtime against an isolated fresh SQLite database and `ENGINEERING_EVIDENCE.json` is checked in CI for drift |

## Current measured engineering snapshot

The release-evidence collector runs in GitHub Actions on both supported SQLite Python jobs. The latest measured snapshot (Ox Alpha next-wave, PR #48 head) is:

- application version: `3.9.0`
- schema contract: `10`
- unique `/api/` paths: `149`
- `/api/` route-method pairs: `167`
- relational tables: `61`
- explicit indexes: `38`
- source test definitions: `160`

The full SQLite regression suite on the same branch reports `160 passed`; all seven required CI checks (SQLite 3.11/3.12, PostgreSQL 16 integration, dependency audit, CodeQL, container smoke + vulnerability scan) pass on the PR #48 head.

## Integrated milestones

- PR #41 — audit recovery, outbox retry, backup integrity and observability hardening
- PR #42 — terminal-idempotent retry semantics for delivered outbox events
- PR #43 — cross-replica singleton scheduled automation
- PR #44 — linked work-order state enforcement for dispatch transitions
- PR #45 — latest-generation scheduler failover semantics
- PR #46 — deterministic release engineering evidence and drift gate
- PR #47 — telemetry temporal integrity port (parallel landing; reconciled with the Ox Alpha superset implementation on this branch)
- PR #48 (open) — telemetry temporal-integrity reconciliation, outage terminality fix, approval/PM domain ownership decomposition, dependency floor maintenance

The historical authorization ceiling remains unchanged: capability overlays are narrowing controls and are not permitted to grant a role access that the historical role did not already have.
