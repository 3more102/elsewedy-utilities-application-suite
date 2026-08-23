# EUAS Disaster Recovery

EUAS includes a deterministic backup, integrity-verification, and restore utility at `scripts/disaster_recovery.py`.

## Objectives

- Produce self-describing backup packages.
- Protect backup integrity with SHA-256 hashes and exact byte counts.
- Validate SQLite backups with `PRAGMA quick_check`.
- Validate PostgreSQL custom-format dumps with `pg_restore --list`.
- Include uploaded documents when requested.
- Refuse destructive restores unless `--force` is explicit.
- Reject manifest path traversal and unsafe upload archive members.

## Backup package

A backup is written as a timestamped directory:

```text
euas-backup-YYYYMMDDTHHMMSSZ/
├── manifest.json
├── database.sqlite3     # SQLite deployments
# or database.pgdump     # PostgreSQL deployments
└── uploads.zip          # unless --no-uploads is used
```

`manifest.json` records:

- backup format version
- UTC creation time
- EUAS application version
- schema version
- database backend
- artifact paths
- artifact sizes
- SHA-256 digests

The package is assembled in a staging directory and renamed into place only after all artifacts and the manifest are complete.

## Create a backup

```bash
python scripts/disaster_recovery.py backup --output-dir backups
```

The command creates the package and immediately performs a deep verification. It exits non-zero if creation or verification fails.

To exclude uploaded files:

```bash
python scripts/disaster_recovery.py backup --output-dir backups --no-uploads
```

### SQLite

When `EUAS_DATABASE_URL` is not PostgreSQL, EUAS backs up `EUAS_DB_PATH` using Python's SQLite online backup API. This avoids copying a potentially inconsistent live database file.

### PostgreSQL

When `EUAS_DATABASE_URL` starts with `postgresql://` or `postgres://`, the utility requires `pg_dump` on `PATH` and creates a custom-format dump using `--format=custom --no-owner`.

## Verify a backup

```bash
python scripts/disaster_recovery.py verify backups/euas-backup-YYYYMMDDTHHMMSSZ
```

Verification checks every artifact against its recorded size and SHA-256 digest. Deep verification additionally runs SQLite `PRAGMA quick_check` or PostgreSQL `pg_restore --list`.

For hash-only verification:

```bash
python scripts/disaster_recovery.py verify backups/euas-backup-YYYYMMDDTHHMMSSZ --shallow
```

A production restore should always use deep verification first.

## Restore SQLite

By default, restore refuses to overwrite an existing target database.

```bash
python scripts/disaster_recovery.py restore backups/euas-backup-YYYYMMDDTHHMMSSZ \
  --sqlite-target /srv/euas/euas.db
```

To intentionally replace an existing database:

```bash
python scripts/disaster_recovery.py restore backups/euas-backup-YYYYMMDDTHHMMSSZ \
  --sqlite-target /srv/euas/euas.db \
  --force
```

The SQLite restore is copied to a temporary target, integrity-checked, then atomically moved into place. Stale `-wal`, `-shm`, and `-journal` sidecars of the target are removed as part of the restore so leftover pre-crash WAL frames cannot be replayed over the restored database (EUAS runs SQLite in WAL mode).

## Restore PostgreSQL

PostgreSQL restore requires a target URL and `pg_restore` on `PATH`:

```bash
python scripts/disaster_recovery.py restore backups/euas-backup-YYYYMMDDTHHMMSSZ \
  --target-database-url 'postgresql://user:password@db-host/euas' \
  --force
```

The restore uses `--clean --if-exists --no-owner`, which drops and recreates every object in the target database; `--force` is therefore required, matching the SQLite restore gate. Run it only against the intended recovery database.

## Restore uploads

Uploads are restored only when explicitly requested:

```bash
python scripts/disaster_recovery.py restore backups/euas-backup-YYYYMMDDTHHMMSSZ \
  --sqlite-target /srv/euas/euas.db \
  --restore-uploads \
  --uploads-target /srv/euas/uploads
```

If the uploads target is non-empty, `--force` is required.

## Recommended production policy

1. Run database backups on a schedule appropriate to the recovery-point objective (RPO).
2. Copy completed packages to storage separate from the EUAS application host.
3. Apply storage encryption and access controls outside EUAS.
4. Run `verify` after transfer to the secondary location.
5. Periodically perform a full restore rehearsal in an isolated environment.
6. Record restore duration to measure the recovery-time objective (RTO).
7. Retain multiple generations so a logically corrupted backup does not become the only recovery point.

The utility guarantees artifact integrity and database-format validation. It does not itself provide off-site replication, encryption-at-rest, retention scheduling, or infrastructure orchestration; those remain deployment responsibilities.
