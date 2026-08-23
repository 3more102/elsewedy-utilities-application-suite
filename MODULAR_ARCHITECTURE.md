# EUAS Modular Architecture Migration

EUAS remains one deployable platform and one database-backed application. The modularization separates ownership and interfaces without introducing duplicate business logic or fake microservices.

## Current application boundaries

| Boundary | Canonical implementation | Current ownership |
|---|---|---|
| Audit | `apps/audit/` | audit writes, SHA-256 chain verification, replay/history reconstruction |
| Events | `apps/events/` | transactional outbox, workflow events, delivery/retry/DeadLetter lifecycle |
| Identity | `apps/identity/` | passwords, sessions, login throttling |
| Authorization | `apps/authorization/` | role checks, permission/capability policy evaluation |
| Assets | `apps/assets/` | asset mutation service and asset-created event publication |
| Maintenance | `apps/maintenance/` | work-order transition policy shared by API and field sync |
| Procurement | `apps/procurement/` | requisition/PO lifecycle policy |
| Inventory | `apps/inventory/` | stock transaction engine and reserved-stock protection |
| HSE | `apps/hse/` | HSE risk/status policy |
| Inspections | `apps/inspections/` | inspection result/corrective-action policy and failure event trigger |
| Projects | `apps/projects/` | project-task normalization and progress recalculation |
| Observability | `apps/observability/` | HTTP request metrics and health/readiness reporting |
| Integrations | `apps/integrations/` | external integration API keys and machine-to-machine telemetry authentication |

## Shared infrastructure

- `core/configuration/` owns runtime settings.
- `core/database/` owns SQLite/PostgreSQL database adapters, schema initialization and migrations.
- `core/shared/` owns cross-domain record-number generation.
- `core/correlation/` owns request/correlation ID generation.
- `api/middleware/` owns HTTP security headers and request-metric recording.

Dependency direction for migrated code is:

```text
API composition
    ↓
Domain / infrastructure apps
    ↓
Core infrastructure
```

Cross-domain code should consume exported app interfaces rather than private implementation helpers. The Events app is the shared event-publication boundary.

## Compatibility shims

The following historical imports remain intentionally available while callers migrate:

- `app/audit_store.py` → `apps.audit`
- `app/event_store.py` → `apps.events`
- `app/auth.py` → `apps.identity` + `apps.authorization`
- `app/config.py` → `core.configuration`
- `app/database.py` → `core.database`

New code should use the canonical modular paths. These shims contain no duplicate implementation.

## Preserved invariants

The migration does not alter the database schema version, foreign keys, transaction semantics, historical authorization policy, audit hash-chain format, reservation protection, procurement/work transition policy, or transactional-outbox DeadLetter behavior.

Outbox terminal behavior remains:

```text
Pending → Processing → Delivered
                  ↘ Failed → DeadLetter
DeadLetter --manual retry/reset--> Pending
```

## Jobs boundary status

A persistent worker queue with leasing, heartbeat and worker registry is **not present in the current v4.9 source tree**. No empty `apps/jobs/` placeholder is created. Existing synchronous automation history remains in `job_runs` until a real persistent worker subsystem is introduced and can be migrated behind a meaningful app interface.

## Remaining monolithic work

`app/main.py` still contains substantial API orchestration and several large read/reporting/service helpers, including telemetry/alarm intelligence, reliability/FMEA/RCM, approval/evidence, retention, workforce planning, field sync, reports/exports and some maintenance/asset read models. These should be extracted incrementally with focused tests rather than by a big-bang route move.

The next migration target should be the telemetry/alarm operations service because it is one of the largest remaining coherent domains and already has extensive regression coverage.
