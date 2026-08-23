# EUAS v4.5 — Governed Retention Execution

EUAS v4.5 turns retention from eligibility metadata into an auditable execution workflow while keeping destructive scope intentionally narrow.

## Policy evaluation

`GET /api/governance/retention/preview` reports for each active policy:

- cutoff timestamp;
- eligible records;
- records covered by active legal holds;
- protected records;
- blocked records where storage deletion cannot be coordinated safely;
- executable records.

The policy registry covers Audit Trail, Approval Signatures, Work Management, Documents, Notifications and Integration Events.

## Legal holds

Administrators can place a hold on an entire data class with `record_key=*` or on a specific business record key. Holds are durable and auditable. Release requires the current administrator password plus a release reason. An active hold always overrides purge eligibility.

## Preview and Execute runs

`POST /api/governance/retention/runs` accepts `mode=Preview` or `mode=Execute`. Preview is non-destructive. Execute is restricted to administrators and requires:

1. the current account password; and
2. exact confirmation text `EXECUTE RETENTION`.

The application scheduler does not automatically invoke destructive retention.

## Current execution matrix

| Data class | Application execution | Reason |
| --- | --- | --- |
| Audit Trail | Protected | tamper-evident governance evidence |
| Approval Signatures | Protected | electronic approval evidence |
| Work Management | Protected | maintenance history |
| Documents | Blocked | binary/object lifecycle must be coordinated with storage |
| Notifications | Executable | transaction-safe relational rows |
| Integration Events | Executable | transaction-safe outbox rows |

This matrix prevents the reference build from claiming deletion it cannot safely perform.

## Retention-run evidence chain

Every run stores a canonical JSON manifest containing actor, mode, scope, timing, summary and per-policy counts. `manifest_hash` is SHA-256 over the previous run hash plus canonical manifest payload. `GET /api/governance/retention/verify` walks the complete chain and reports the first invalid run if stored content changes.

`GET /api/governance/retention/runs/{id}/evidence` returns a ZIP containing:

- `manifest.json`;
- `items.csv`;
- `verification.json`.

The package is suitable for transfer into an external object-lock/WORM or records-management system. EUAS does not claim that local SQLite/PostgreSQL data is itself immutable.

## Observability

Metrics include:

- `euas_retention_runs_total`;
- `euas_retention_purged_records_total`;
- `euas_active_retention_holds`;
- `euas_retention_run_chain_valid`.

CSV run history is available at `/api/exports/retention-runs.csv`.

## Production boundary

A production deployment should pair this workflow with approved legal/records schedules, database backups, external object storage lifecycle, object lock/WORM where required, privileged-access monitoring, change control and a production migration framework.
