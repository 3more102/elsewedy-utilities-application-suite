# DATABASE INDEX AUDIT

> EUAS Application -- Schema Version 12

## Overview

- **Total tables:** 66
- **Total indexes:** 56 (including 1 partial index)
- **Database backends:** SQLite (default), PostgreSQL (via psycopg compatibility layer)

All indexes are defined in `app/database.py` within the `init_db()` function. Index names follow the convention `idx_<table_suffix>_<columns>`.

---

## Index Catalog

### Assets

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_assets_no` | `assets` | `asset_no` | Unique asset lookup by asset number | `WHERE asset_no = ?` |
| `idx_assets_location` | `assets` | `location_id` | Filter assets by physical location | `WHERE location_id = ?`, join to `locations` |
| `idx_assets_status` | `assets` | `status` | Filter assets by operational status | `WHERE status = 'Operating'`, dashboard asset counts |
| `idx_asset_parent` | `assets` | `parent_asset_id` | Navigate parent-child asset hierarchy | `WHERE parent_asset_id = ?`, tree queries |

**Redundancy check:** `idx_assets_no` is redundant with the `UNIQUE` constraint on `asset_no`, which SQLite already indexes. However, keeping it is harmless and makes the intent explicit. No other redundancy detected.

### Work Orders

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_wo_status` | `work_orders` | `status` | Filter work orders by status | `WHERE status = 'Open'`, board views |
| `idx_wo_assigned` | `work_orders` | `assigned_to, status` | Technician work list with status filter | `WHERE assigned_to = ? AND status IN (...)` |
| `idx_wo_due` | `work_orders` | `target_finish, status` | Due-date driven queries and SLA compliance | `WHERE target_finish <= ? AND status != 'Completed'` |
| `idx_wo_asset` | `work_orders` | `asset_id` | Work order history per asset | `WHERE asset_id = ? ORDER BY created_at` |
| `idx_wo_priority_status` | `work_orders` | `priority, status` | Priority-sorted work board views | `WHERE priority = 'High' AND status = 'Open'` |
| `idx_wo_work_type_status` | `work_orders` | `work_type, status, asset_id` | Filter by work type, status, and asset | `WHERE work_type = 'Corrective' AND status = 'Open'` |

**Redundancy check:** `idx_wo_status` is partially covered by the leading column of `idx_wo_assigned`, `idx_wo_priority_status`, and `idx_wo_work_type_status`, but it remains useful for queries that filter on status alone without an additional leading predicate. No redundancy.

### Preventive Maintenance

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_pm_due` | `maintenance_plans` | `next_due, active` | Scheduler: find overdue/soon-due PM plans | `WHERE active = 1 AND next_due <= ?` |
| `idx_pm_asset_active` | `maintenance_plans` | `asset_id, active` | PM plans for a specific active asset | `WHERE asset_id = ? AND active = 1` |

**Redundancy check:** None. Each serves a distinct query axis.

### Inventory and Reservations

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_inventory_warehouse` | `inventory_items` | `warehouse_id, category` | Browse items by warehouse and category | `WHERE warehouse_id = ? AND category = ?` |
| `idx_inventory_reservations_work` | `inventory_reservations` | `work_order_id, status` | Reservations for a work order | `WHERE work_order_id = ? AND status = 'Reserved'` |
| `idx_inventory_reservations_item` | `inventory_reservations` | `inventory_item_id, status` | Reservation availability per item | `WHERE inventory_item_id = ? AND status = 'Reserved'` |

**Redundancy check:** None.

### Inspections

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_inspection_asset` | `inspections` | `asset_id, status` | Inspections for an asset with status filter | `WHERE asset_id = ? AND status = 'Draft'` |

### Notifications

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_notifications_user` | `notifications` | `user_id, is_read, created_at` | User notification inbox | `WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC` |

### Approvals

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_approvals_queue` | `approval_requests` | `status, assigned_role, assigned_user_id, requested_at` | Pending approval queue for a role or user | `WHERE status = 'Pending' AND assigned_role = ? ORDER BY requested_at` |
| `idx_approval_record` | `approval_requests` | `module, record_type, record_id, status` | Look up approval for a specific record | `WHERE module = ? AND record_type = ? AND record_id = ? AND status = 'Pending'` |
| `idx_approval_delegations_active` | `approval_delegations` | `delegate_user_id, active, start_at, end_at` | Active delegations for a delegate user | `WHERE delegate_user_id = ? AND active = 1 AND start_at <= ? AND end_at >= ?` |

**Redundancy check:** `idx_approvals_queue` and `idx_approval_record` serve different query paths (queue vs. record lookup). No redundancy.

### Workflow Events

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_workflow_record` | `workflow_events` | `module, record_type, record_id, created_at` | Event history for a specific record | `WHERE module = ? AND record_type = ? AND record_id = ? ORDER BY created_at` |

### Job Runs

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_job_runs_status` | `job_runs` | `status, started_at` | Find running or failed jobs | `WHERE status = 'Running'`, `WHERE status = 'Failed' ORDER BY started_at` |

### SLA

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_work_order_sla_due` | `work_order_sla` | `response_status, response_due, resolution_status, resolution_due` | SLA breach detection | `WHERE response_status = 'Pending' AND response_due <= ?` |
| `idx_sla_events_work` | `sla_events` | `work_order_id, created_at` | SLA event timeline for a work order | `WHERE work_order_id = ? ORDER BY created_at` |

### Event Outbox

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_outbox_status` | `event_outbox` | `status, created_at` | Outbox polling for pending events | `WHERE status = 'Pending' ORDER BY created_at LIMIT ?` |

### Cost Ledger

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_cost_ledger_asset` | `maintenance_cost_ledger` | `asset_id, posted_at` | Cost history for an asset over time | `WHERE asset_id = ? ORDER BY posted_at` |
| `idx_cost_ledger_work` | `maintenance_cost_ledger` | `work_order_id, posted_at` | Cost history for a work order | `WHERE work_order_id = ? ORDER BY posted_at` |

### Report Snapshots

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_report_snapshots_scope` | `report_snapshots` | `scope_type, scope_id, generated_at` | Reports for a given scope | `WHERE scope_type = ? AND scope_id = ? ORDER BY generated_at DESC` |

### Asset Health

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_asset_health_asset` | `asset_health_snapshots` | `asset_id, calculated_at` | Health history for an asset | `WHERE asset_id = ? ORDER BY calculated_at DESC` |

### Workforce Management

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_technician_profiles_site` | `technician_profiles` | `home_site_id, active` | Active technicians at a site | `WHERE home_site_id = ? AND active = 1` |
| `idx_shift_assignments_user` | `technician_shift_assignments` | `user_id, active, effective_from, effective_to` | Current shift for a user | `WHERE user_id = ? AND active = 1 AND effective_from <= ?` |
| `idx_technician_absences_user` | `technician_absences` | `user_id, status, start_date, end_date` | Absences for a user in a date range | `WHERE user_id = ? AND status = 'Approved' AND start_date <= ? AND end_date >= ?` |

### Work Requirements and Crafts

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_work_requirements_work` | `work_order_requirements` | `work_order_id, status` | Material requirements for a work order | `WHERE work_order_id = ? AND status = 'Required'` |
| `idx_work_craft_work` | `work_order_craft_requirements` | `work_order_id, craft_id` | Craft requirements for a work order | `WHERE work_order_id = ?` |

### Asset Outages

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_asset_outages_asset` | `asset_outages` | `asset_id, start_at, end_at` | Outage history for an asset | `WHERE asset_id = ? ORDER BY start_at DESC` |
| `idx_asset_outages_site` | `asset_outages` | `site_id, status, start_at` | Open outages at a site | `WHERE site_id = ? AND status = 'Open' ORDER BY start_at` |

### Dispatch

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_dispatch_technician` | `dispatch_assignments` | `technician_user_id, status, dispatched_at` | Active dispatch for a technician | `WHERE technician_user_id = ? AND status = 'Dispatched'` |
| `idx_dispatch_work` | `dispatch_assignments` | `work_order_id, status` | Dispatch for a work order | `WHERE work_order_id = ? AND status != 'Cancelled'` |

### Telemetry

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_telemetry_channels_asset` | `telemetry_channels` | `asset_id, active` | Active channels for an asset | `WHERE asset_id = ? AND active = 1` |
| `idx_telemetry_channels_metric` | `telemetry_channels` | `metric_type, active` | Channels by metric type | `WHERE metric_type = 'Temperature' AND active = 1` |
| `idx_telemetry_readings_channel_time` | `telemetry_readings` | `channel_id, captured_at` | Time-series readings for a channel | `WHERE channel_id = ? ORDER BY captured_at DESC` |
| `idx_telemetry_readings_client_ref` | `telemetry_readings` | `channel_id, client_ref` (partial) | Idempotent dedup by client reference | `WHERE channel_id = ? AND client_ref = ?` (partial: `client_ref IS NOT NULL`) |

### Operational Alarms

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_operational_alarms_status` | `operational_alarms` | `status, severity, opened_at` | Alarm dashboard sorted by severity | `WHERE status = 'Open' ORDER BY severity, opened_at` |
| `idx_operational_alarms_asset` | `operational_alarms` | `asset_id, status, opened_at` | Alarms for an asset | `WHERE asset_id = ? AND status = 'Open'` |
| `idx_alarms_channel_status` | `operational_alarms` | `channel_id, status, opened_at` | Alarms for a telemetry channel | `WHERE channel_id = ? AND status = 'Open'` |
| `idx_alarms_site_status` | `operational_alarms` | `site_id, status, severity` | Alarms for a site | `WHERE site_id = ? AND status = 'Open' ORDER BY severity` |

**Redundancy check:** `idx_operational_alarms_status` overlaps with `idx_operational_alarms_asset` and `idx_alarms_site_status` when `status = 'Open'` is the leading predicate. However, the dashboard query typically scans all open alarms without an asset or site filter, so `idx_operational_alarms_status` serves that full-table scan. No functional redundancy.

### Locations

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_locations_site` | `locations` | `site_id` | Locations for a site | `WHERE site_id = ?` |

### Audit Logs

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_audit_chain` | `audit_logs` | `id, audit_hash` | Tamper-evident chain traversal | `WHERE id = ?` for chain verification |
| `idx_audit_user_time` | `audit_logs` | `user_id, created_at` | Audit trail for a user over time | `WHERE user_id = ? ORDER BY created_at` |

### KPI Snapshots

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_kpi_snapshot_scope` | `kpi_snapshot` | `scope_key, calculated_at` | KPI history for a scope | `WHERE scope_key = ? ORDER BY calculated_at DESC` |

### CBM Recommendations

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_cbm_asset_status` | `cbm_recommendations` | `asset_id, status` | CBM recommendations for an asset | `WHERE asset_id = ? AND status = 'Open'` |
| `idx_cbm_channel_status` | `cbm_recommendations` | `channel_id, status` | CBM recommendations from a channel | `WHERE channel_id = ? AND status = 'Open'` |

### FMEA Records

| Index | Table | Columns | Purpose | Query Patterns |
|-------|-------|---------|---------|----------------|
| `idx_fmea_asset` | `fmea_records` | `asset_id, status` | FMEA records for an asset | `WHERE asset_id = ? AND status = 'Draft'` |

---

## Redundancy Summary

**No redundant indexes detected.** Each index serves a distinct query pattern or access path. The only observation:

- `idx_assets_no` duplicates the implicit UNIQUE index on `asset_no`. This is intentional for clarity and is negligible in overhead.

---

## Index Design Principles Observed

1. **Composite indexes** are ordered with the most selective/filterable column first (e.g., `status` before `created_at`).
2. **Partial index** (`idx_telemetry_readings_client_ref`) uses `WHERE client_ref IS NOT NULL` to keep the index small.
3. **No covering indexes** -- all indexes are lookup/filter indexes; no `INCLUDE` columns are used.
4. **Consistent naming** -- `idx_<table_suffix>_<columns>` pattern throughout.
5. **Foreign key indexes** are not always present; the schema relies on application-level query patterns rather than blanket FK indexing.
