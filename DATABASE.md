# EUAS Database Architecture

EUAS v4.9.0 supports two runtime database modes behind the same application service boundary.

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

Current schema version: **19**.

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

The v4.9.0 schema contains **83 relational tables** and **76 explicit indexes**. Fresh installs create schema v19 directly. v4.9 adds `rcm_strategies` and `rcm_strategy_reviews`, supporting status/FMEA/type/review indexes and the `reliability.rcm.manage` / `reliability.rcm.approve` permissions. RCM links to CBM rules and PM plans are application-validated integer references because the reference initializer creates those legacy tables later in its cross-database DDL order; submission/activation validates existence, same-asset/FMEA scope and active state. v4.8 adds `failure_modes`, `asset_fmea` and `fmea_reviews`, FMEA linkage columns on work orders/CBM rules/CBM events, supporting lookup indexes and the `reliability.fmea.manage` permission. v4.7 adds `cbm_rules`, `cbm_events` and `cbm_rule_state`, CBM lookup indexes, CBM outcome counters on telemetry ingest batches and the `cbm.rules.manage` permission. v4.6 adds `user_permission_overrides`, expands the permission catalog metadata with category/risk/description fields and adds the override-expiry index. v4.5 adds `retention_holds`, `retention_runs` and `retention_run_items` plus active-hold, run-time/hash and run-item indexes. v4.4 adds `approval_signature_evidence`; v4.3 adds offline field-sync ledgers; v4.2 adds asset topology/root-cause evidence; v4.1 adds alarm shelving; and v4.0 adds the Utility Command Center integration/telemetry reliability tables. Production PostgreSQL upgrades should still be managed through an approved migration framework rather than relying on application startup as the long-term migration strategy.


## Schema v16 — fine-grained access administration

- `permissions` carries permission category, risk level and human-readable description metadata.
- `role_permissions` remains the baseline role-grant relation.
- `user_permission_overrides` stores explicit Allow/Deny decisions, reason, optional expiry and administrator evidence.
- `idx_user_permission_overrides_expiry` supports active-override evaluation and governance views.

Effective authorization is evaluated server-side. Seeded baseline grants are only added when a permission code is first introduced, so application restart does not restore a grant that an administrator intentionally removed.

## Schema v15 — retention governance

- `retention_holds` — class-wide or record-scoped legal holds with placement/release evidence.
- `retention_runs` — Preview/Execute run header, actor, summary and SHA-256 linked canonical manifest.
- `retention_run_items` — per-policy cutoff, eligible, held, protected, blocked and purged counts.

No database trigger claims immutable storage. External WORM/object-lock retention remains deployment infrastructure.


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


## Schema v18 — reliability and FMEA linkage

- `failure_modes` — reusable hierarchical failure-mode taxonomy with parent/child relations.
- `asset_fmea` — asset-specific effects, causes, controls, recommendations, S/O/D ratings, RPN, risk band, owner and review state.
- `fmea_reviews` — immutable rating/RPN review history.
- `work_orders.asset_fmea_id` — traceability from reliability analysis to governed work.
- `cbm_rules.asset_fmea_id` / `cbm_events.asset_fmea_id` — same-asset FMEA traceability through condition-based maintenance.
- Dedicated FMEA/failure-mode and linkage indexes support portfolio, asset, risk and work lookups.

## Schema v19 — Reliability-Centered Maintenance strategies

- `rcm_strategies` — one governed maintenance strategy per FMEA record, including functional failure, consequence class, strategy type, task, justification, interval, CBM/PM linkage, owner, status, approval/activation evidence and review due date.
- `rcm_strategy_reviews` — append-only formal Continue / Revise / Retire review history with previous/next due dates and reviewer evidence.
- RCM indexes support status, FMEA, strategy-type and review-history lookups.
- `reliability.rcm.manage` governs authoring/submission/activation/review; `reliability.rcm.approve` is an additional critical permission for Approval Center decisions on RCM strategy records.

`linked_cbm_rule_id` and `linked_pm_plan_id` are deliberately validated by application logic rather than declared as forward foreign keys in the shared SQLite/PostgreSQL reference initializer. On submission/activation EUAS proves the linked record exists, is active where required, and belongs to the same governed asset/FMEA context.

