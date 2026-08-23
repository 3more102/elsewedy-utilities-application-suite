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
| PostgreSQL validation | Mandatory CI includes inventory, audit, procurement, reservation, workflow, dispatch/work-state, dispatch assignment/redispatch, transfers, PM, alarms, inspections, business numbers, reorder, outbox, scheduler and telemetry concurrency smokes plus live HTTP smoke |
| Security scanning | Required security workflow includes Python dependency audit, CodeQL, production container smoke and high/critical vulnerability scanning |
| Release evidence | `scripts/engineering_evidence.py` measures the composed runtime against an isolated fresh SQLite database and `ENGINEERING_EVIDENCE.json` is checked in CI for drift |

## Current measured engineering snapshot

The release-evidence collector was executed in GitHub Actions on both supported SQLite Python jobs. The measured snapshot for this wave is:

- application version: `3.9.0`
- schema contract: `10`
- unique `/api/` paths: `149`
- `/api/` route-method pairs: `167`
- relational tables: `61`
- explicit indexes: `38`
- source test definitions: `148`

The full SQLite regression suite on the same evidence branch reports `148 passed`.

## Integrated milestones

- PR #41 — audit recovery, outbox retry, backup integrity and observability hardening
- PR #42 — terminal-idempotent retry semantics for delivered outbox events
- PR #43 — cross-replica singleton scheduled automation
- PR #44 — linked work-order state enforcement for dispatch transitions
- PR #45 — latest-generation scheduler failover semantics
- PR #46 — deterministic release engineering evidence and drift gate (current wave)

The historical authorization ceiling remains unchanged: capability overlays are narrowing controls and are not permitted to grant a role access that the historical role did not already have.
