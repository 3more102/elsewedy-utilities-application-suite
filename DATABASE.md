# EUAS Database Architecture

EUAS v4.4.0 supports two runtime database modes behind the same application service boundary.

## 1. SQLite reference mode

This is the default zero-configuration mode used for local demos, QA and presentations.

```text
EUAS_DB_PATH=./euas.db
```

Characteristics:

- relational schema with foreign keys
- WAL mode
- explicit indexes
- automatic schema creation and demo seeding
- no external database server required

## 2. PostgreSQL mode

Set:

```text
EUAS_DATABASE_URL=postgresql://euas:<password>@<host>:5432/euas
```

When a PostgreSQL URL is present, EUAS selects the PostgreSQL adapter instead of SQLite. The adapter provides parameter translation, PostgreSQL DDL conversion, transaction handling and generated-ID retrieval while keeping the application APIs unchanged.

Install the project requirements in the target environment. PostgreSQL mode requires `psycopg`.

### Docker Compose

```bash
docker compose -f docker-compose.postgres.yml up --build
```

Set a non-default password before production use:

```bash
export EUAS_POSTGRES_PASSWORD='replace-with-a-secret'
```

### Connectivity preflight

```bash
EUAS_DATABASE_URL=postgresql://... python scripts/postgres_preflight.py
```

## Schema versioning

EUAS records applied schema versions in:

```text
schema_migrations
```

Current schema version: **2**.

The startup initializer is idempotent for the reference application: it creates missing tables/indexes and records the current schema version. For a large production rollout, replace startup DDL with a dedicated migration tool and controlled deployment pipeline.

## Important validation note

SQLite initialization and the full EUAS regression suite are executed in the supplied build environment. The PostgreSQL adapter SQL translation contract is unit-tested. A live PostgreSQL server is not available in the current build sandbox, and the sandbox has no internet access to install the missing PostgreSQL driver, so a live PostgreSQL integration test is **not claimed** in this release.

The target deployment should run:

1. `python scripts/postgres_preflight.py`
2. application startup against an empty PostgreSQL database
3. `python scripts/smoke_test.py` adapted to that environment or equivalent CI API smoke
4. backup/restore validation before go-live

## Production recommendations

- managed PostgreSQL or HA cluster
- encrypted connections and secret manager
- automated backup / point-in-time recovery
- migration approval in CI/CD
- connection pooling
- database monitoring and slow-query analysis
- least-privilege application role
- separate reporting/read replicas where needed


## v3.3 operations ledger

`job_runs` records every EUAS automation execution with run number, trigger source, actor, business date, status, timestamps, JSON result summary and error text. Schema version 3 creates this ledger and its status/start-time index.


## Schema v4 additions

Schema version 4 adds four operational-control tables:

- `sla_policies` — priority-to-response/resolution policy master.
- `work_order_sla` — one SLA clock/result row per work order.
- `sla_events` — unique breach/escalation history.
- `event_outbox` — durable integration events and delivery attempts.

The release continues to initialize schema additively with `CREATE TABLE/INDEX IF NOT EXISTS` and records the active release schema in `schema_migrations`.


## Schema v5 governance tables

Schema version **5** adds:

- `maintenance_cost_ledger` — posted maintenance labor/material/historical cost events linked to work orders and assets.
- `report_snapshots` — point-in-time serialized reports with SHA-256 content hashes.
- `backup_records` — evidence of application-generated SQLite backup packages and checksums.
- `retention_policies` — configurable retention intent and non-destructive eligibility calculations.
- `audit_logs.prev_hash` / `audit_logs.audit_hash` — linked tamper-evident audit-chain fields.

The v4.4.0 schema contains **71 relational tables** and **55 explicit indexes**. Fresh installs create schema v14 directly. v4.4 adds `approval_signature_evidence` plus signer/time indexes for credential-verified, hash-chained approval decision evidence. v4.3 adds `field_sync_clients` and `field_sync_operations` for idempotent offline field replay and conflict evidence. v4.2 adds `asset_topology_links` plus root-cause/correlation fields on `alarm_incidents`; v4.1 adds the `alarm_shelves` governance table and its active/alarm indexes. v4.0 adds `integration_api_keys`, `telemetry_ingest_batches`, `alarm_suppressions`, `alarm_incidents` and `alarm_incident_members`, while `telemetry_readings` gains external-id/batch linkage for duplicate protection and traceability. The reference initialization also adds the two audit-chain columns to an existing audit table when absent and backfills blank legacy hashes in sequence. Production PostgreSQL upgrades should still be managed through an approved migration framework rather than relying on application startup as the long-term migration strategy.


## Schema v6 planning tables

- `approval_delegations` — temporary user-to-user approval authority with module/effective-date scope.
- `asset_health_snapshots` — persisted point-in-time health score, band and factor breakdown for each asset.

The live asset-health endpoint recalculates from current transactional state; snapshots are persisted on explicit recalculation and automation runs.


## Schema v7 — workforce and planning readiness

Schema v7 adds `crafts`, `technician_profiles`, `shift_templates`, `technician_shift_assignments`, `technician_absences`, `work_order_requirements` and `work_order_craft_requirements`. These records keep resource availability and planned material/craft demand normalized instead of embedding schedules or spare lists in free-text work-order fields.


## v3.8 execution tables

- `inventory_reservations` — work-order-specific material reservation, issued quantity and release state.
- `dispatch_assignments` — technician dispatch lifecycle, ETA and field arrival timestamps.
- `asset_outages` — explicit asset downtime/outage evidence linked to assets, sites and optionally work orders.

Indexes support active reservation lookup by work/item, dispatch lookup by technician/work, and outage analysis by asset/site and timestamp. Reserved stock is protected from generic issue/transfer transactions so allocated work cannot be silently starved by unrelated stock movements.


## Schema v10 — Command Center additions

### `integration_api_keys`
Stores only the SHA-256 digest of machine-to-machine telemetry keys, plus scope, expiry, active state and last-use evidence. The plaintext key is never persisted.

### `telemetry_ingest_batches`
Provides per-batch evidence for received/accepted/duplicate/bad-quality/suppressed readings and alarm outcomes.

### `telemetry_readings.external_id` / `batch_id`
`external_id` is unique per telemetry channel when supplied. `batch_id` links the reading to an ingestion batch.

### `alarm_suppressions`
Time-bounded site/asset/channel suppression windows for maintenance, commissioning and test activities.

### `alarm_incidents` and `alarm_incident_members`
Correlated operational incidents and their member alarms. The relationship is normalized so alarm evidence remains independently queryable while one incident can coordinate response and corrective work.


## Schema v12 — asset topology and incident root-cause evidence

`asset_topology_links` stores directed operational relationships between upstream and downstream assets. Active links are indexed in both directions for correlation lookup. The API rejects self-links, duplicate active links and directed cycles.

`alarm_incidents` now persists `root_cause_asset_id`, `correlation_mode`, `root_cause_score`, `root_cause_reason` and `topology_hops`. These values are deterministic decision-support evidence, not a predictive diagnosis. Startup backfills the fields for existing incidents after a v4.1 → v4.2 upgrade.
