# EUAS Database Architecture

EUAS v3.9.0 supports SQLite reference mode and PostgreSQL production mode behind the same application service boundary.

## Runtime database modes

### SQLite reference mode

```text
EUAS_DB_PATH=./euas.db
```

SQLite is the default zero-configuration mode for local development, demos and QA. EUAS enables foreign keys and WAL mode and uses the same application schema contract exercised by CI.

### PostgreSQL mode

```text
EUAS_DATABASE_URL=postgresql://euas:<password>@<host>:5432/euas
```

When a PostgreSQL URL is configured, EUAS uses the PostgreSQL adapter for qmark bind translation, DDL compatibility, transactional behavior and generated-ID handling. PostgreSQL mode requires `psycopg` from `requirements.txt`.

Local container deployment:

```bash
docker compose -f docker-compose.postgres.yml up --build
```

Connectivity preflight:

```bash
EUAS_DATABASE_URL=postgresql://... python scripts/postgres_preflight.py
```

## Schema contract and migration ledger

The application schema contract is currently **version 10**. Applied versions are recorded in:

```text
schema_migrations
```

The historical bootstrap owns the large additive base schema through version 9. Schema changes after that baseline are executed through the migration registry in `app/migrations.py`.

The migration runner:

- pins legacy bootstrap to schema v9 so bootstrap cannot pre-claim a later version;
- orders registered migrations by version;
- validates the structural contract for a recorded migration instead of trusting the ledger marker alone;
- can repair the historical v10 pre-claim case when the marker exists but the hardened auth tables are missing;
- refuses databases carrying schema versions newer than the running application;
- refuses gaps for which no migration is registered;
- serializes PostgreSQL migration execution with a transaction-scoped advisory lock;
- takes an immediate SQLite write transaction before migration inspection/execution.

### Migration operations

Inspect the configured database without modifying it:

```bash
python scripts/migrate.py status
```

Bootstrap the v9 base when necessary and advance through registered migrations:

```bash
python scripts/migrate.py upgrade
```

Fail unless the configured database exactly satisfies the current migration contract:

```bash
python scripts/migrate.py check
```

All commands support machine-readable output:

```bash
python scripts/migrate.py check --json
```

`production_readiness.py --check-db` uses the same controlled bootstrap/migration path and treats migration-contract failure as a deployment failure. The external automation worker also migrates before executing scheduled work.

## Schema v10 authentication hardening

Schema v10 adds the hardened authentication persistence layer:

- `auth_sessions` — digest-only bearer-session storage with revocation, expiry and client metadata;
- `auth_login_throttle` — persistent login throttling state;
- indexes for active-session lookup, token-digest lookup and throttle maintenance.

The legacy `sessions` table remains as a rolling-upgrade compatibility landing zone. Historical raw tokens found there are converted to one-way SHA-256 digests and removed transactionally during the v10 migration.

## Earlier schema milestones

- **v3:** `job_runs` automation execution ledger.
- **v4:** SLA policy/results/events and durable `event_outbox`.
- **v5:** maintenance cost ledger, report snapshots, backup records, retention policies and tamper-evident audit-chain fields.
- **v6:** approval delegations and asset-health snapshots.
- **v7:** crafts, technician profiles, shifts, absences and normalized work-order resource requirements.
- **v8/v9:** inventory reservations, dispatch assignments, asset outages and subsequent production-hardening additions incorporated into the v9 baseline.
- **v10:** hardened session storage and persistent authentication throttling.

## CI and production validation

GitHub Actions validates both supported database modes. The mandatory pipeline includes:

- SQLite regression on Python 3.11 and 3.12;
- deterministic engineering-evidence drift detection;
- migration `upgrade` + `check` validation on an isolated SQLite database;
- PostgreSQL 16 connectivity and strict production-readiness checks;
- PostgreSQL migration-contract validation;
- PostgreSQL concurrency smokes across inventory, audit, procurement, reservations, workflow, dispatch, transfers, preventive maintenance, alarms, inspections, business numbers, reorder generation, outbox delivery, scheduler singleton behavior and telemetry ordering/integrity;
- live PostgreSQL HTTP smoke testing.

Before production deployment, run at minimum:

```bash
python scripts/postgres_preflight.py
python scripts/migrate.py upgrade
python scripts/migrate.py check
python scripts/production_readiness.py --strict-production --require-postgres --check-db
```

Production deployments should additionally use encrypted connections, managed secrets, automated backup/PITR, database monitoring, least-privilege roles and an approved migration/deployment change process.
