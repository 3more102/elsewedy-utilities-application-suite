# EUAS Database Architecture

EUAS v4.0.0 supports two runtime database modes behind the same application service boundary.

## 1. SQLite reference mode

SQLite remains the default zero-configuration mode for local development, demonstrations and lightweight QA.

```text
EUAS_DB_PATH=./euas.db
```

Characteristics:

- relational schema with foreign keys
- WAL mode
- explicit indexes
- automatic schema creation and demo seeding
- no external database server required
- full regression coverage in CI on Python 3.11 and Python 3.12

SQLite reference mode is not the recommended multi-user production database.

## 2. PostgreSQL production-target mode

Configure a PostgreSQL URL:

```text
EUAS_DATABASE_URL=postgresql://euas:<password>@<host>:5432/euas
```

When a PostgreSQL URL is present, EUAS selects the PostgreSQL adapter instead of SQLite. PostgreSQL mode requires `psycopg`, which is included in `requirements.txt`.

### Docker Compose

```bash
docker compose -f docker-compose.postgres.yml up --build
```

Set a non-default password before any non-development deployment:

```bash
export EUAS_POSTGRES_PASSWORD='replace-with-a-secret'
```

## PostgreSQL compatibility contract

EUAS v4.0.0 retains the SQLite-shaped application query layer while hardening the PostgreSQL adapter in `app/postgres_compat.py`.

The compatibility layer currently provides:

- SQLite qmark (`?`) bind translation to psycopg `%s` binds
- literal `%` escaping for parameterized PostgreSQL queries such as SQL `LIKE` patterns
- typed standalone nullable bind checks for PostgreSQL type inference
- SQLite-style direct cursor iteration
- statement-local generated IDs using `RETURNING id`
- safe detection of serial/identity `id` columns through `information_schema`
- `INSERT OR IGNORE` translation to `ON CONFLICT DO NOTHING`

The v4 generated-ID implementation intentionally does **not** use a lazy session-global `LASTVAL()` result. Endpoints can insert audit/event records after their business row, so the originating insert must retain its own generated ID.

## Schema versioning

EUAS records applied schema versions in:

```text
schema_migrations
```

Current application schema contract: **version 9**.

Fresh installations create the complete v9 schema and seed data idempotently. The current startup initializer is suitable for fresh/reference environments and additive table/index creation, but it is **not claimed as a complete production migration framework for arbitrary historical schemas**.

For controlled production upgrades, use a reviewed migration process and validate the target database before traffic is enabled.

## Production readiness gate

EUAS v4 adds a deterministic deployment check:

```bash
python scripts/production_readiness.py
```

For a production PostgreSQL deployment:

```bash
EUAS_ENV=production \
EUAS_DATABASE_URL=postgresql://... \
python scripts/production_readiness.py --strict-production --require-postgres --check-db
```

The gate validates deployment configuration, PostgreSQL selection, database initialization/connectivity, critical tables, seed integrity and the active schema contract. It also reports webhook-signing, scheduler, session-lifetime and upload-limit readiness.

## PostgreSQL connectivity preflight

Run:

```bash
EUAS_DATABASE_URL=postgresql://... python scripts/postgres_preflight.py
```

This verifies that the configured server is reachable and reports the connected PostgreSQL database/user/server version.

## Live PostgreSQL CI evidence

Unlike v3.x, EUAS v4.0.0 has a live PostgreSQL integration lane in GitHub Actions using PostgreSQL 16.

The CI gate boots a fresh PostgreSQL service and validates:

1. database connectivity preflight
2. strict production-readiness checks
3. application startup against PostgreSQL
4. health and readiness endpoints
5. security response headers
6. administrator authentication
7. dashboard read paths
8. asset retrieval
9. generated-ID work-order creation and persistence
10. telemetry-channel creation
11. telemetry-reading ingestion
12. threshold-driven operational alarm creation
13. telemetry history retrieval
14. alarm-to-corrective-work-order linkage
15. persisted telemetry/alarm retrieval
16. automation execution
17. application metrics exposure

The same pull-request gate also runs the full SQLite regression suite on Python 3.11 and Python 3.12.

## Current schema capability history

### Schema v3 — automation operations ledger

`job_runs` records every EUAS automation execution with run number, trigger source, actor, business date, status, timestamps, JSON result summary and error text.

### Schema v4 — operational controls

- `sla_policies`
- `work_order_sla`
- `sla_events`
- `event_outbox`

### Schema v5 — governance and evidence

- `maintenance_cost_ledger`
- `report_snapshots`
- `backup_records`
- `retention_policies`
- `audit_logs.prev_hash`
- `audit_logs.audit_hash`

### Schema v6 — planning intelligence

- `approval_delegations`
- `asset_health_snapshots`

### Schema v7 — workforce and planning readiness

- `crafts`
- `technician_profiles`
- `shift_templates`
- `technician_shift_assignments`
- `technician_absences`
- `work_order_requirements`
- `work_order_craft_requirements`

### Schema v8 — execution coordination

- `inventory_reservations`
- `dispatch_assignments`
- `asset_outages`

### Schema v9 — utility operations intelligence

- `telemetry_channels`
- `telemetry_readings`
- `operational_alarms`

The v9 schema contains the complete integrated EUAS data model used by the v4.0.0 application release.

## Production database recommendations

Before enterprise go-live, retain these deployment controls around EUAS:

- managed PostgreSQL or an HA PostgreSQL topology
- TLS for database connections
- secrets manager rather than plaintext credentials
- automated backups and point-in-time recovery
- tested restore procedures
- approved forward/rollback migrations
- connection pooling
- database metrics and slow-query analysis
- least-privilege application role
- separate reporting/read replicas where workload requires them
- explicit upgrade rehearsals against a production-like copy

## Remaining database lifecycle work

Live PostgreSQL runtime compatibility is now CI-proven for representative read/write workflows. The next database-lifecycle milestone is a dedicated migration framework for upgrading arbitrary existing production schemas, including reversible versioned migrations and upgrade rehearsal tests. EUAS v4.0.0 does not claim that milestone is complete.
