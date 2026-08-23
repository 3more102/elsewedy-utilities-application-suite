# EUAS Operations Guide

## Automation engine

EUAS v4.9.0 includes an auditable automation engine for recurring operational controls. Each run is recorded in `job_runs` with a unique run number, trigger source, actor, business date, completion status and JSON summary.

A run evaluates:

- due calendar, meter/runtime/usage and condition-based preventive-maintenance plans;
- low-stock items that require automatic purchase requisitions;
- overdue work orders and responsible-user notifications;
- asset warranties expiring within 30 days;
- active contracts expiring within 30 days;
- approval requests that have remained pending for more than two days;
- work-order SLA response and resolution breaches;
- durable integration-event outbox delivery/retry.

Generated PM work orders enter the normal work-order approval workflow. Automatic replenishment requisitions enter the normal procurement approval workflow. Unread notifications are de-duplicated by target and linked record so repeated job cycles do not continuously create the same alert.

### Manual API execution

`POST /api/automation/run`

Allowed roles: System Administrator and Maintenance Manager.

### External scheduler

For cron, Windows Task Scheduler or Kubernetes CronJob:

```bash
python scripts/run_automation.py
```

Optional business date:

```bash
python scripts/run_automation.py --as-of 2026-08-19
```

For clustered/multi-replica production deployments, prefer this external-scheduler pattern so only one scheduler owns recurring execution.

### In-process scheduler

Single-node deployments can set:

```text
EUAS_AUTOMATION_INTERVAL_MINUTES=60
```

The default is `0`, which disables in-process scheduling.

## Observability

Authenticated management roles can read:

- `GET /api/health`
- `GET /api/health/ready`
- `GET /api/automation/status`
- `GET /api/automation/runs`
- `GET /api/metrics`

`/api/metrics` returns Prometheus-style counters/gauges for HTTP request volume, 5xx errors, average request latency, uptime, active sessions, successful automation runs, SLA breaches and pending/failed outbox events.

## Reporting / exports

Protected CSV exports are available for:

- Work Orders — `/api/exports/work-orders.csv`
- Inventory — `/api/exports/inventory.csv`
- Procurement — `/api/exports/procurement.csv`
- Audit Trail — `/api/exports/audit.csv`
- SLA performance — `/api/exports/sla.csv`

The Automation & Reports application exposes these downloads through the UI.

## SQLite backup and restore

### UI/API backup

Administrators can download a transactionally consistent backup from Automation & Reports. The bundle contains:

- `database/euas.db`
- uploaded files
- `backup_manifest.json`

### CLI backup

```bash
python scripts/backup_sqlite.py
```

Custom output:

```bash
python scripts/backup_sqlite.py --output /backup/EUAS_backup.zip
```

The script uses SQLite's online backup API and runs `PRAGMA integrity_check` before finalizing the bundle.

### CLI restore

Stop the EUAS process first, then run:

```bash
python scripts/restore_sqlite.py EUAS_backup.zip --force
```

The restore tool validates the bundle, checks database integrity and preserves a timestamped safety copy of the existing database before replacement.

For PostgreSQL production deployments, use platform-native PostgreSQL backup tools (`pg_dump`, managed snapshots, PITR/WAL policies) instead of the built-in SQLite backup endpoint.

## Notification operations

Users can mark individual notifications as read or use **Mark all read** in the Notification Centre. Automation-generated unread alerts are de-duplicated until the existing alert is read or resolved by operational action.


## SLA operations

Management roles can inspect `/api/sla/summary`, `/api/sla/work-orders`, `/api/sla/events` and `/api/sla/policies`. Each work order receives a policy from its priority at creation/backfill time. Starting work stamps the first-response result; completing work stamps the resolution result. Automation detects overdue response/resolution clocks, records a unique `sla_events` breach, sends de-duplicated escalation notifications and publishes an integration event.

Default seeded policies:

| Priority | Response | Resolution |
|---|---:|---:|
| Emergency | 15 min | 240 min |
| Critical | 30 min | 480 min |
| High | 120 min | 1440 min |
| Medium | 480 min | 4320 min |
| Low | 1440 min | 10080 min |

Administrators and Maintenance Managers can change these targets from Automation & Reports; existing work-order clocks for the matching priority are recalculated from original creation time.

## Integration event outbox

Workflow and SLA events are persisted in `event_outbox` before external delivery. With no webhook configured, automation marks events `Skipped` while retaining the complete event record. With a webhook configured, delivery is attempted and recorded as `Delivered` or `Failed`; failed events can be manually re-queued. See `INTEGRATIONS.md`.


## Audit-chain verification

Administrators can verify the persisted audit chain through the API:

```text
GET /api/audit/integrity
```

or directly from the configured database:

```bash
python scripts/verify_audit.py
```

A valid result includes the number of records checked and the current head hash. A changed audit row causes a non-zero CLI exit and reports the first invalid record.

## Backup evidence registry

`GET /api/admin/backup` returns the SQLite backup ZIP with an `X-EUAS-Backup-SHA256` response header and records the generated artifact in `backup_records`. `GET /api/admin/backups` exposes recent evidence records. The separate CLI backup/restore utilities remain available for operational recovery. PostgreSQL deployments should use PostgreSQL-native backup tooling.

## Governed retention execution

`GET /api/governance/retention/preview` calculates eligible, held, protected, blocked and executable counts for each policy. `POST /api/governance/retention/runs` persists Preview and Execute runs. Execute is never triggered automatically by the scheduler: an administrator must re-enter the current password and provide the exact `EXECUTE RETENTION` confirmation.

Legal holds are managed under `/api/governance/retention/holds`. Active holds override purge eligibility. Protected classes remain non-destructive even during an Execute run. Transaction-safe application purge currently covers Notifications and Integration Events; document binaries are blocked until coordinated object-storage deletion is available.

Every run is hash-chained and can be verified through `/api/governance/retention/verify`. The evidence ZIP contains the canonical manifest, policy/item counts and chain verification so operations teams can archive it in an external immutable evidence repository. See `RETENTION_GOVERNANCE.md`.


## v3.6 planning operations

Automation runs persist current asset-health snapshots, emit alerts for Critical health bands, and deactivate approval delegations whose effective end time has passed. The 90-day maintenance forecast is calculated on demand and can be exported as CSV.

## v4.0 control-room automation

Automation now also:

- expires ended alarm-suppression windows;
- detects telemetry channels with no recent data and emits de-duplicated stale-telemetry notifications;
- correlates legacy/unassigned active alarms into operational incidents;
- continues to process durable outbox delivery after the control-room checks.

The Utility Command Center should be treated as the first operational triage surface: correlated incident → alarm evidence → outage context → technician dispatch → corrective work.


## Field synchronization operations

Supervisors can inspect synchronization evidence through `/api/exports/field-sync.csv`; platform metrics expose pending, conflicting and recently applied field operations. A rising `euas_field_sync_conflicts` count should trigger review of field connectivity, dispatch overlap and server-side edits made while technicians are offline. Conflict resolution is intentionally explicit rather than automatic last-write-wins.
