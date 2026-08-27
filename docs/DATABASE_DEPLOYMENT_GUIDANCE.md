# DATABASE DEPLOYMENT GUIDANCE

> EUAS Application -- Schema Version 12

## Database Configuration

| Setting | Value |
|---------|-------|
| Default backend | SQLite |
| PostgreSQL backend | Activated via `EUAS_DATABASE_URL` env var |
| Schema version | 12 |
| Migration framework | `app/migrations.py` (baseline v9, registered v10-v12) |
| Connection manager | `app/database.py` context manager `db()` |

---

## SQLite Safe Operating Envelope

### PRAGMAs Applied on Every Connection

```sql
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
```

#### `foreign_keys=ON`

Enforces all `REFERENCES` constraints at the SQLite level. Without this, SQLite silently ignores foreign key declarations. Every connection must enable this before any data mutation.

#### `journal_mode=WAL` (Write-Ahead Logging)

- Enables concurrent reads while a write transaction is in progress.
- Write transactions still serialize -- only one writer at a time.
- WAL files (`-wal`, `-shm`) are created alongside the database file and must be included in backups.
- WAL is persistent once set; it survives process restarts.

#### `busy_timeout=5000`

When another connection holds a write lock, SQLite will wait up to 5000 ms before returning `SQLITE_BUSY`. This prevents immediate failure under moderate concurrency (e.g., multiple API requests hitting the database simultaneously). The value of 5000 ms was chosen to:

- Accommodate typical write transaction durations (usually < 50 ms).
- Provide headroom for migration operations that hold `BEGIN IMMEDIATE` for longer.
- Avoid indefinite blocking that would stall application threads.

**If you observe frequent `SQLITE_BUSY` errors:** Increase `busy_timeout` to 10000-15000 ms. If they persist, the application has outgrown SQLite's concurrency model.

### Concurrency Model

SQLite allows **one writer at a time**. The WAL mode improves read concurrency but does not allow parallel writes. The application handles this by:

1. Keeping write transactions short (typically < 100 ms).
2. Using `busy_timeout=5000` to queue writers instead of failing immediately.
3. Using `BEGIN IMMEDIATE` for migrations to acquire the write lock eagerly.

**Maximum practical throughput:** ~500-1000 writes/second on modern SSD hardware. For higher write volumes, PostgreSQL is required.

---

## WAL Mode Benefits

| Benefit | Description |
|---------|-------------|
| **Concurrent reads** | Readers do not block writers and vice versa. |
| **Crash recovery** | WAL is an append-only log; recovery after crash is faster than rollback journal. |
| **No exclusive locks for reads** | Read transactions use snapshot isolation without locking. |
| **Checkpoint efficiency** | WAL checkpoints can be tuned to control file size. |

**Operational note:** The WAL file grows until a checkpoint runs. SQLite checkpoints automatically when the WAL exceeds ~1000 pages (default). Manual `PRAGMA wal_checkpoint(TRUNCATE)` can be run during maintenance windows.

---

## Migration Safety

### Migration Framework (`app/migrations.py`)

The migration system operates on a numbered version scheme:

| Version | Name | Description |
|---------|------|-------------|
| 9 | baseline | Base schema (all tables, seed data) |
| 10 | `auth_session_hardening` | Auth sessions and login throttle tables |
| 11 | `site_customer_count` | Adds `customer_count` to `sites` |
| 12 | `apm_condition_reliability` | Adds `cbm_recommendations` and `fmea_records` |

### Migration Lock Mechanism

| Backend | Lock Mechanism |
|---------|---------------|
| SQLite | `BEGIN IMMEDIATE` -- acquires RESERVED lock eagerly |
| PostgreSQL | `pg_advisory_xact_lock(0x455541530010)` -- session-level advisory lock |

**Why `BEGIN IMMEDIATE` for SQLite:**
- `BEGIN` (deferred) acquires a SHARED lock, which can fail when upgrading to RESERVED if a writer is active.
- `BEGIN IMMEDIATE` acquires a RESERVED lock immediately, ensuring the migration does not stall behind a long read transaction.
- Only one migration can run at a time, which is enforced by the advisory lock pattern.

### Migration Validation

Each migration has a `validate` function that checks the schema actually has the expected columns/tables. This catches partial migrations that may have been interrupted.

### Schema Version Guard

The application refuses to start if the database schema version is newer than the application binary:

```python
if db_version > int(_euas_config.SCHEMA_VERSION):
    raise RuntimeError(
        f'Database schema version {db_version} is newer than application schema version '
        f'{_euas_config.SCHEMA_VERSION}; refusing to start.'
    )
```

This prevents data corruption from running an older binary against a newer schema.

---

## Backup and Restore

### SQLite Backup

**Recommended approach:** Use the SQLite online backup API or file-level copy.

#### Option 1: File Copy (simple)

```powershell
# Stop the application, then copy:
Copy-Item "euas.db" "euas_backup_<timestamp>.db"
Copy-Item "euas.db-wal" "euas_backup_<timestamp>.db-wal" -ErrorAction SilentlyContinue
Copy-Item "euas.db-shm" "euas_backup_<timestamp>.db-shm" -ErrorAction SilentlyContinue
```

Both the WAL and SHM files must be copied for a consistent backup.

#### Option 2: SQLite `.backup` command

```sql
-- From sqlite3 CLI or application code:
.backup "euas_backup.db"
```

This performs an online backup without stopping reads or writes.

### PostgreSQL Backup

```bash
pg_dump "$EUAS_DATABASE_URL" > euas_backup_<timestamp>.sql
```

### Restore

```powershell
# SQLite restore
Copy-Item "euas_backup_<timestamp>.db" "euas.db"
# Restart the application -- WAL will replay automatically
```

```bash
# PostgreSQL restore
psql "$EUAS_DATABASE_URL" < euas_backup_<timestamp>.sql
```

### Backup Schedule Recommendations

| Environment | Frequency | Retention |
|-------------|-----------|-----------|
| Development | On demand | 7 days |
| Staging | Daily | 30 days |
| Production | Every 6 hours | 90 days (minimum) |

Production backups must also be verified with `PRAGMA integrity_check` (SQLite) or `pg_dump --verify` (PostgreSQL).

---

## When to Consider PostgreSQL

### Indicators That SQLite Is Insufficient

| Symptom | Threshold |
|---------|-----------|
| Concurrent write failures | `SQLITE_BUSY` errors exceeding 1% of write operations |
| Write throughput | > 500 writes/second sustained |
| Database size | > 50 GB |
| Multi-process access | More than one application server writing to the same database |
| Replication requirement | Need for read replicas or streaming replication |
| Advanced data types | Need for JSONB, arrays, full-text search, or geospatial queries |

### PostgreSQL Migration Path

The application already supports PostgreSQL via the `EUAS_DATABASE_URL` environment variable. The compatibility layer in `app/database.py` translates:

| SQLite Construct | PostgreSQL Equivalent |
|------------------|----------------------|
| `INSERT OR IGNORE INTO` | `INSERT INTO ... ON CONFLICT DO NOTHING` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `?` placeholders | `%s` placeholders |
| `lastrowid` | `SELECT LASTVAL()` |
| `PRAGMA table_info(t)` | `information_schema.columns` |
| `BEGIN IMMEDIATE` | `pg_advisory_xact_lock(key)` |
| `sqlite_master` | `information_schema.tables` |
| `REAL` type | `DOUBLE PRECISION` |

### Migration Considerations

1. **Data export:** Export SQLite data to CSV/SQL and import into PostgreSQL.
2. **Schema translation:** The `_postgresize_schema()` function handles DDL translation.
3. **Testing:** Run the full test suite with `EUAS_DATABASE_URL` pointing to PostgreSQL before production cutover.
4. **Connection pooling:** PostgreSQL deployments should use connection pooling (e.g., PgBouncer) since `psycopg.connect()` opens a new connection per request.
