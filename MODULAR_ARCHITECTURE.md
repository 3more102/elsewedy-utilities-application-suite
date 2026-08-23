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
| Maintenance | `apps/maintenance/` | work-order creation/update commands, atomic lifecycle transitions, dispatch ownership, SLA handling |
| Procurement | `apps/procurement/` | requisition/PO lifecycle policy |
| Inventory | `apps/inventory/` | stock transaction engine, reservations/readiness, reserved-stock reconciliation and protection |
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

`apps/jobs/` is a real persistent worker subsystem backed by the shared database. It owns durable jobs, workers, job attempts and leases, including atomic claim, heartbeat, retry/backoff, lease-expiry recovery, DeadLetter, manual replay, cancellation, deduplication and handler registration. Event dispatch consumes this platform through the canonical Events boundary rather than a parallel queue.

## Remaining monolithic work

`app/main.py` still contains substantial API orchestration and large read/reporting helpers, including procurement/inventory HTTP commands, inspection/HSE execution, retention, field synchronization, reports/exports and several cross-domain read models. Telemetry, alarms, correlation, condition monitoring, jobs, CBM, reliability/FMEA/RCM, workforce/scheduling/preventive maintenance and approval evidence already have canonical application boundaries.

The next migration target is procurement and inventory command/concurrency hardening, followed by inspection/HSE execution and route decomposition.
