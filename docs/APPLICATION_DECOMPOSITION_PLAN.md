# APPLICATION_DECOMPOSITION_PLAN.md

Decomposition plan for `app/application.py` (2611 lines).

---

## 1. Current State

`app/application.py` is a monolithic FastAPI module containing:

| Section | Lines | Approx. Size |
|---|---|---|
| Imports | 1-18 | 18 |
| Lifespan / App init | 20-36 | 17 |
| Middleware (security headers) | 48-78 | 31 |
| Login brute-force helpers | 80-94 | 15 |
| Role constants | 96-102 | 7 |
| DB utility helpers | 105-128 | 24 |
| Domain service functions | 131-936 | 806 |
| Pydantic request models | 938-1057 | 120 |
| API route handlers | 1059-2608 | 1550 |
| Static mounts / root | 2608-2611 | 4 |

**Total: ~2611 lines in a single file.**

The file already depends on 25+ extracted `*_store.py` modules (asset_store, work_order_store, telemetry_store, etc.) which hold query-level data access. The routes and domain service logic in application.py are what remain after those extractions.

### Imports consumed

```python
from .config import APP_NAME, APP_VERSION, STATIC_DIR, UPLOAD_DIR, SESSION_HOURS,
    MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, ALLOWED_DOC_SUFFIXES, DB_BACKEND, DB_PATH,
    SCHEMA_VERSION, AUTOMATION_INTERVAL_MINUTES, EVENT_WEBHOOK_URL,
    EVENT_WEBHOOK_SECRET, OUTBOX_MAX_ATTEMPTS
from .database import db, init_db, now, audit_digest
from .audit_verification import AuditIntegrityError, replay_audit_history, verify_audit_chain_report
from .auth import hash_password, verify_password, current_user, require_roles
from .report_html import render_snapshot_report_html, render_work_order_report_html
from .kpi_service import risk_weighted_backlog
```

---

## 2. Shared Constants and Helpers Inventory

### 2a. Role constants (line 96-102)

```python
WRITE_ROLES    = ('admin','asset_manager','maintenance_manager','planner','supervisor')
WORK_ROLES     = ('admin','maintenance_manager','planner','supervisor','technician')
INV_ROLES      = ('admin','maintenance_manager','planner','storekeeper','technician')
DOC_WRITE_ROLES= ('admin','asset_manager','maintenance_manager','planner','supervisor',
                   'technician','storekeeper','procurement','hse','project_manager')
PROC_ROLES     = ('admin','maintenance_manager','procurement')
HSE_ROLES      = ('admin','hse','maintenance_manager')
PROJECT_ROLES  = ('admin','project_manager','maintenance_manager')
```

Used by: work order routes, inventory routes, procurement routes, HSE routes, project routes, inspection routes, dispatch routes, document routes, export routes, admin routes, telemetry routes, outage routes, SLA routes.

### 2b. SQL constants (lines 1532, 1570)

```python
ASSET_SELECT = '''SELECT a.*, at.name asset_type, at.utility_domain, ...'''
WO_SELECT    = '''SELECT w.*, a.asset_no, a.name asset_name, ...'''
```

Used by: asset routes, work order routes, dispatch board, search, dashboard, analytics, CSV exports.

### 2c. Work order state machine (lines 1622-1630)

```python
TRANSITIONS = { ... }   # state -> action -> target
ACTION_ROLES = { ... }  # action -> allowed roles
```

Used by: `transition_work` route.

### 2d. Backlog weights (lines 314-315)

```python
_BACKLOG_PRIORITY_WEIGHT  = {'Emergency':30, 'Critical':26, 'High':20, 'Medium':12, 'Low':5}
_BACKLOG_CRITICALITY_WEIGHT = {'Critical':14, 'High':10, 'Medium':6, 'Low':2}
```

Used by: `_ranked_work_backlog`, dashboard, backlog route.

### 2e. Document media types (lines 2213-2226)

```python
_DOCUMENT_MEDIA_TYPES = { '.pdf': 'application/pdf', ... }
```

Used by: `download_document`.

---

## 3. Domain Service Functions (lines 131-936)

These are private helpers that perform business logic, not route handlers.

| Function | Lines | Owner Module | Dependencies |
|---|---|---|---|
| `_asset_health` | 131-153 | reliability / asset_health | rows, get_or_404 |
| `_save_asset_health` | 155-159 | reliability / asset_health | _asset_health, now |
| `_delegation_active` | 161-167 | approval | now |
| `_forecast_bucket_start` | 169-170 | planning | timedelta |
| `_parse_days_of_week` | 172-180 | workforce | (none) |
| `_workforce_week_capacity` | 182-228 | workforce | rows, _parse_days_of_week |
| `_reservation_rows` | 230-233 | work_order / inventory | rows |
| `_sync_reserved_stock` | 235-238 | work_order / inventory | (none) |
| `_work_order_parts_readiness` | 240-256 | work_order | rows |
| `_maintenance_forecast` | 258-312 | planning | _forecast_bucket_start, _workforce_week_capacity, _work_order_parts_readiness |
| `_material_blocker_map` | 317-343 | work_order | rows |
| `_ranked_work_backlog` | 345-486 | work_order | rows, _material_blocker_map, _BACKLOG_PRIORITY_WEIGHT, _BACKLOG_CRITICALITY_WEIGHT |
| `_outage_overlap_hours` | 489-495 | operations / outage | _dt |
| `_asset_reliability_rows` | 497-524 | reliability | rows, _outage_overlap_hours, _dt |
| `_bad_actor_rows` | 526-582 | reliability | _asset_reliability_rows |
| `_site_reliability_rows` | 584-597 | reliability | _asset_reliability_rows |
| `notify` | 599-600 | notification | now |
| `notify_once` | 813-818 | notification | notify |
| `workflow_event` | 602-605 | workflow | emit_event, now |
| `create_approval` | 607-613 | approval | next_no, notify, now |
| `resolve_approval` | 615-620 | approval | now |
| `next_no` | 622-629 | shared | (none) |
| `get_or_404` | 631-634 | shared | HTTPException |
| `user_id_by_username` | 636-637 | shared | (none) |
| `_dt` | 639-641 | shared | datetime |
| `emit_event` | 643-646 | notification | uuid, json, now |
| `_channel_site` | 648-650 | telemetry | (none) |
| `_telemetry_alarm_level` | 652-660 | telemetry | (none) |
| `_event_instant_or_none` | 662-678 | telemetry | datetime, timezone |
| `_count_stale_channels` | 680-685 | telemetry | _event_instant_or_none |
| `_evaluate_telemetry_alarm` | 687-720 | telemetry | _telemetry_alarm_level, _channel_site, next_no, notify_once, emit_event, audit |
| `_operations_intelligence` | 722-733 | operations | _count_stale_channels |
| `_ensure_work_sla` | 735-750 | sla | _dt |
| `_backfill_work_order_slas` | 752-754 | sla | _ensure_work_sla |
| `_mark_sla_response` | 756-760 | sla | _ensure_work_sla, _dt |
| `_mark_sla_resolution` | 762-766 | sla | _ensure_work_sla, _dt |
| `_run_sla_scan` | 768-792 | sla | _backfill_work_order_slas, _dt, notify_once, emit_event |
| `_process_outbox` | 794-811 | integration | rows, json, hmac, urllib |
| `_generate_due_pm` | 843-867 | automation | rows, next_no, _ensure_work_sla, create_approval, workflow_event, audit, notify_once |
| `_run_reorder_scan` | 869-882 | automation | rows, next_no, create_approval, workflow_event, audit, notify_once |
| `_run_kpi_refresh` | 884-886 | automation | (no-op) |
| `_execute_automation` | 888-925 | automation | _generate_due_pm, _run_reorder_scan, _run_sla_scan, _run_kpi_refresh, _save_asset_health, _process_outbox, notify_once, emit_event, audit |
| `_automation_loop` | 927-936 | automation | _execute_automation, db |
| `_csv_safe_cell` | 823-834 | export | (none) |
| `csv_response` | 837-841 | export | _csv_safe_cell, StreamingResponse |
| `_recalculate_project_progress` | 2126-2130 | project | (none) |
| `_active_dispatch_holder` | 1632-1637 | dispatch | (none) |
| `_settle_cancelled_work` | 1639-1656 | work_order | resolve_approval, _sync_reserved_stock, audit |

---

## 4. Pydantic Request Models (lines 938-1057)

| Model | Line | Domain |
|---|---|---|
| `LoginIn` | 939-944 | auth |
| `AssetIn` | 945-950 | asset |
| `AssetPatch` | 951-955 | asset |
| `WorkOrderIn` | 956-958 | work_order |
| `WorkOrderPatch` | 959-960 | work_order |
| `TransitionIn` | 961 | work_order |
| `CBMDecisionIn` | 962 | work_order |
| `SiteCustomerCountPatch` | 963 | reliability |
| `FMEAIn` | 964 | reliability |
| `FMEAPatch` | 965 | reliability |
| `SLAPolicyPatch` | 966 | sla |
| `NoteIn` | 967 | work_order |
| `FieldAssetUpdate` | 968 | asset |
| `LaborIn` | 969 | work_order |
| `MaterialIn` | 970 | work_order |
| `PMIn` | 971 | automation / planning |
| `InventoryIn` | 972 | inventory |
| `InventoryTxIn` | 973 | inventory |
| `PRIn` | 974 | procurement |
| `POIn` | 975 | procurement |
| `QuoteIn` | 976 | procurement |
| `VendorIn` | 977 | vendor |
| `ContractIn` | 978 | contract |
| `InspectionIn` | 979 | inspection |
| `InspectionSubmit` | 980 | inspection |
| `HSEIn` | 981 | hse |
| `ProjectIn` | 982 | project |
| `MeterReadingIn` | 983 | asset |
| `UserIn` | 984 | admin |
| `ProfilePatch` | 985-989 | auth |
| `PasswordChange` | 990-992 | auth |
| `ProjectTaskIn` | 993-994 | project |
| `ProjectTaskPatch` | 995-996 | project |
| `HSEPatch` | 997-998 | hse |
| `UserStatusIn` | 999 | admin |
| `ApprovalDecisionIn` | 1000 | approval |
| `ApprovalDelegationIn` | 1001-1002 | approval |
| `WorkRequirementIn` | 1003-1004 | work_order |
| `CraftRequirementIn` | 1005-1006 | work_order |
| `TechnicianProfileIn` | 1007-1008 | workforce |
| `ShiftAssignmentIn` | 1009-1010 | workforce |
| `AbsenceIn` | 1011-1012 | workforce |
| `ReservationIn` | 1013-1014 | work_order / inventory |
| `ReservationIssueIn` | 1015-1016 | work_order / inventory |
| `OutageIn` | 1017-1018 | operations / outage |
| `OutageCloseIn` | 1019-1020 | operations / outage |
| `ReliabilityCustomersIn` | 1021-1022 | reliability |
| `TelemetryChannelIn` | 1023-1025 | telemetry |
| `TelemetryChannelPatch` | 1026-1028 | telemetry |
| `TelemetryReadingItem` | 1029-1031 | telemetry |
| `TelemetryIngestIn` | 1032-1033 | telemetry |
| `AlarmWorkOrderIn` | 1034-1035 | telemetry |
| `DispatchIn` | 1036-1037 | dispatch |
| `DispatchTransitionIn` | 1038-1039 | dispatch |
| `KPIIn` | 1040-1047 | analytics / kpi |
| `KPIPatch` | 1048-1056 | analytics / kpi |
| `KPIRecalcIn` | 1057 | analytics / kpi |
| `RetentionPatch` | 2567 | governance (inline) |

---

## 5. Route Catalog

### 5a. Auth routes (lines 1081-1147)

| Method | Path | Function | Roles |
|---|---|---|---|
| POST | `/api/auth/login` | `login` | public |
| POST | `/api/auth/logout` | `logout` | authenticated |
| GET | `/api/auth/me` | `me` | authenticated |
| PATCH | `/api/auth/profile` | `update_profile` | authenticated |
| POST | `/api/auth/change-password` | `change_password` | authenticated |
| GET | `/api/auth/sessions` | `list_sessions` | authenticated |
| POST | `/api/auth/sessions/revoke-others` | `revoke_other_sessions` | authenticated |

### 5b. Asset routes (lines 1155-1186, 1531-1567)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/assets/health` | `asset_health_portfolio` | authenticated |
| GET | `/api/assets/{asset_id}/health` | `asset_health_detail` | authenticated |
| POST | `/api/assets/health/recalculate` | `recalculate_asset_health` | WRITE_ROLES |
| POST | `/api/meters/{meter_id}/readings` | `add_meter_reading` | WORK_ROLES |
| GET | `/api/planning/maintenance-forecast` | `maintenance_forecast` | authenticated |

### 5c. Workforce routes (lines 1188-1257)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/workforce/crafts` | `workforce_crafts` | authenticated |
| GET | `/api/workforce/shifts` | `workforce_shifts` | authenticated |
| GET | `/api/workforce/technicians` | `workforce_technicians` | authenticated |
| PUT | `/api/workforce/technicians/{user_id}` | `upsert_technician_profile` | admin, maintenance_manager, planner |
| POST | `/api/workforce/technicians/{user_id}/shift-assignments` | `add_shift_assignment` | admin, maintenance_manager, planner |
| GET | `/api/workforce/absences` | `workforce_absences` | authenticated |
| POST | `/api/workforce/absences` | `create_absence` | admin, maintenance_manager, planner, supervisor |
| GET | `/api/workforce/capacity` | `workforce_capacity` | authenticated |

### 5d. Work order routes (lines 1569-1841)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/work-orders` | `list_work` | authenticated |
| GET | `/api/work-orders/backlog` | `work_backlog` | authenticated |
| GET | `/api/work-orders/{wo_id}` | `get_work` | authenticated |
| POST | `/api/work-orders` | `create_work` | WRITE_ROLES |
| PATCH | `/api/work-orders/{wo_id}` | `update_work` | WRITE_ROLES |
| POST | `/api/work-orders/{wo_id}/transition` | `transition_work` | WORK_ROLES |
| GET | `/api/work-orders/{wo_id}/parts-readiness` | `work_parts_readiness` | authenticated |
| POST | `/api/work-orders/{wo_id}/requirements` | `add_work_requirement` | admin, maintenance_manager, planner, supervisor, storekeeper |
| DELETE | `/api/work-orders/{wo_id}/requirements/{rid}` | `delete_work_requirement` | admin, maintenance_manager, planner, supervisor, storekeeper |
| GET | `/api/work-orders/{wo_id}/reservations` | `list_work_reservations` | authenticated |
| POST | `/api/work-orders/{wo_id}/reservations` | `reserve_work_material` | admin, maintenance_manager, planner, supervisor, storekeeper |
| POST | `/api/work-orders/{wo_id}/reserve-all` | `reserve_all_work_materials` | admin, maintenance_manager, planner, supervisor, storekeeper |
| POST | `/api/reservations/{rid}/release` | `release_reservation` | admin, maintenance_manager, planner, supervisor, storekeeper |
| POST | `/api/reservations/{rid}/issue` | `issue_reservation` | INV_ROLES |
| POST | `/api/work-orders/{wo_id}/craft-requirements` | `add_work_craft_requirement` | admin, maintenance_manager, planner, supervisor |
| POST | `/api/work-orders/{wo_id}/labor` | `add_labor` | WORK_ROLES |
| POST | `/api/work-orders/{wo_id}/materials` | `add_material` | INV_ROLES |
| POST | `/api/work-orders/{wo_id}/notes` | `add_work_note` | WORK_ROLES |
| POST | `/api/work-orders/{wo_id}/tasks/{tid}/toggle` | `toggle_work_task` | WORK_ROLES |
| GET | `/api/work-orders/{wo_id}/report` | `work_report` | authenticated |

### 5e. Telemetry routes (lines 1429-1529)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/telemetry/channels` | `telemetry_channels` | authenticated |
| POST | `/api/telemetry/channels` | `create_telemetry_channel` | admin, asset_manager, maintenance_manager, planner |
| PATCH | `/api/telemetry/channels/{cid}` | `update_telemetry_channel` | admin, asset_manager, maintenance_manager, planner |
| POST | `/api/telemetry/ingest` | `ingest_telemetry` | admin, asset_manager, maintenance_manager, planner, supervisor, technician |
| GET | `/api/telemetry/readings` | `telemetry_readings` | authenticated |
| GET | `/api/alarms` | `alarms` | authenticated |
| POST | `/api/alarms/{aid}/acknowledge` | `acknowledge_alarm` | admin, asset_manager, maintenance_manager, planner, supervisor, technician |
| POST | `/api/alarms/{aid}/close` | `close_alarm` | admin, asset_manager, maintenance_manager, planner, supervisor |
| POST | `/api/alarms/{aid}/work-order` | `alarm_create_work_order` | admin, asset_manager, maintenance_manager, planner, supervisor |
| GET | `/api/operations/intelligence` | `operations_intelligence` | authenticated |

### 5f. Reliability routes (lines 1259-1334)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/reliability/assets` | `reliability_assets` | authenticated |
| GET | `/api/reliability/sites` | `reliability_sites` | authenticated |
| PUT | `/api/reliability/customers` | `reliability_set_customers` | admin, maintenance_manager, planner |
| GET | `/api/reliability/customers` | `reliability_get_customers` | authenticated |
| GET | `/api/reliability/indices` | `reliability_indices` | authenticated |
| GET | `/api/backlog/risk-weighted` | `backlog_risk_weighted` | authenticated |

### 5g. Dashboard routes (lines 1338-1427)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/workflow-events` | `list_workflow_events` | authenticated |
| GET | `/api/launchpad` | `launchpad` | authenticated |
| GET | `/api/dashboard` | `dashboard` | authenticated |
| GET | `/api/reference` | `reference` | authenticated |
| GET | `/api/operations` | `operations` | authenticated |
| GET | `/api/map` | `map_data` | authenticated |

### 5h. Operations routes (lines 1958-2001)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/outages` | `list_outages` | authenticated |
| POST | `/api/outages` | `create_outage` | admin, asset_manager, maintenance_manager, planner, supervisor, technician |
| POST | `/api/outages/{oid}/close` | `close_outage` | admin, asset_manager, maintenance_manager, planner, supervisor, technician |

### 5i. Dispatch routes (lines 2002-2061)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/dispatch` | `list_dispatch` | authenticated |
| GET | `/api/dispatch/board` | `dispatch_board` | authenticated |
| POST | `/api/work-orders/{wo_id}/dispatch` | `dispatch_work` | admin, maintenance_manager, planner, supervisor |
| POST | `/api/dispatch/{did}/transition` | `transition_dispatch` | WORK_ROLES |

### 5j. Inventory routes (lines 1847-1892)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/inventory` | `list_inventory` | authenticated |
| POST | `/api/inventory` | `create_inventory` | admin, storekeeper, maintenance_manager |
| POST | `/api/inventory/{iid}/transaction` | `inventory_tx` | INV_ROLES |
| GET | `/api/inventory/{iid}/transactions` | `inventory_history` | authenticated |
| POST | `/api/inventory/reorder-scan` | `reorder_scan` | admin, storekeeper, maintenance_manager, procurement |

### 5k. Procurement routes (lines 1894-1956)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/procurement` | `procurement` | authenticated |
| POST | `/api/procurement/requisitions` | `create_pr` | admin, storekeeper, maintenance_manager, procurement, planner |
| POST | `/api/procurement/requisitions/{pid}/submit` | `submit_pr` | admin, storekeeper, maintenance_manager, procurement, planner |
| POST | `/api/procurement/requisitions/{pid}/approve` | `approve_pr` | PROC_ROLES |
| POST | `/api/procurement/quotations` | `create_quote` | PROC_ROLES |
| POST | `/api/procurement/purchase-orders` | `create_po` | PROC_ROLES |
| POST | `/api/procurement/purchase-orders/{pid}/receive` | `receive_po` | admin, procurement, storekeeper |

### 5l. Inspection routes (lines 2071-2097)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/inspections` | `list_inspections` | authenticated |
| GET | `/api/inspections/{iid}` | `get_inspection` | authenticated |
| POST | `/api/inspections` | `create_inspection` | WORK_ROLES |
| POST | `/api/inspections/{iid}/submit` | `submit_inspection` | WORK_ROLES |

### 5m. HSE routes (lines 2099-2124)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/hse` | `list_hse` | authenticated |
| POST | `/api/hse` | `create_hse` | HSE_ROLES |
| PATCH | `/api/hse/{hid}` | `update_hse` | HSE_ROLES |

### 5n. Project routes (lines 2132-2170)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/projects` | `list_projects` | authenticated |
| POST | `/api/projects` | `create_project` | PROJECT_ROLES |
| POST | `/api/projects/{pid}/tasks` | `create_project_task` | PROJECT_ROLES |
| PATCH | `/api/projects/{pid}/tasks/{tid}` | `update_project_task` | PROJECT_ROLES |

### 5o. Vendor/contract routes (lines 2172-2186)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/vendors` | `vendors` | authenticated |
| POST | `/api/vendors` | `create_vendor` | admin, procurement, maintenance_manager |
| GET | `/api/contracts` | `contracts` | authenticated |
| POST | `/api/contracts` | `create_contract` | admin, procurement, maintenance_manager |

### 5p. Document routes (lines 2188-2247)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/documents` | `documents` | authenticated |
| POST | `/api/documents/upload` | `upload_document` | DOC_WRITE_ROLES |
| GET | `/api/documents/{did}/download` | `download_document` | authenticated |

### 5q. SLA routes (lines 2249-2295)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/sla/summary` | `sla_summary` | admin, maintenance_manager, planner, supervisor, executive |
| GET | `/api/sla/policies` | `sla_policies` | admin, maintenance_manager, planner, supervisor, executive |
| PATCH | `/api/sla/policies/{pid}` | `update_sla_policy` | admin, maintenance_manager |
| GET | `/api/sla/work-orders` | `sla_work_orders` | admin, maintenance_manager, planner, supervisor, executive |
| GET | `/api/sla/events` | `sla_event_list` | admin, maintenance_manager, planner, supervisor, executive |

### 5r. Automation routes (lines 2297-2362)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/events/outbox` | `outbox_list` | admin, maintenance_manager, executive |
| POST | `/api/events/outbox/{eid}/retry` | `retry_outbox_event` | admin, maintenance_manager |
| GET | `/api/automation/status` | `automation_status` | admin, maintenance_manager, executive |
| GET | `/api/automation/runs` | `automation_runs` | admin, maintenance_manager, executive |
| POST | `/api/automation/run` | `automation_run` | admin, maintenance_manager |
| GET | `/api/metrics` | `metrics` | admin, maintenance_manager, executive |

### 5s. Export routes (lines 2364-2448)

| Method | Path | Function |
|---|---|---|
| GET | `/api/exports/work-orders.csv` | `export_work_orders` |
| GET | `/api/exports/inventory.csv` | `export_inventory` |
| GET | `/api/exports/procurement.csv` | `export_procurement` |
| GET | `/api/exports/audit.csv` | `export_audit` |
| GET | `/api/exports/sla.csv` | `export_sla` |
| GET | `/api/exports/cost-ledger.csv` | `export_cost_ledger` |
| GET | `/api/exports/asset-health.csv` | `export_asset_health` |
| GET | `/api/exports/maintenance-forecast.csv` | `export_maintenance_forecast` |
| GET | `/api/exports/workforce-capacity.csv` | `export_workforce_capacity` |
| GET | `/api/exports/reliability.csv` | `export_reliability` |
| GET | `/api/exports/outages.csv` | `export_outages` |
| GET | `/api/exports/dispatch.csv` | `export_dispatch` |
| GET | `/api/exports/reservations.csv` | `export_reservations` |
| GET | `/api/exports/alarms.csv` | `export_alarms` |
| GET | `/api/exports/telemetry.csv` | `export_telemetry` |

### 5t. Admin routes (lines 2450-2606)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/admin/backup` | `admin_backup` | admin |
| GET | `/api/admin/backups` | `backup_history` | admin, maintenance_manager, executive |
| GET | `/api/notifications` | `notifications` | authenticated |
| POST | `/api/notifications/{nid}/read` | `notification_read` | authenticated |
| POST | `/api/notifications/read-all` | `notifications_read_all` | authenticated |
| GET | `/api/search` | `search` | authenticated |
| GET | `/api/analytics` | `analytics` | authenticated |
| GET | `/api/audit/integrity` | `audit_integrity` | admin, maintenance_manager, executive |
| GET | `/api/audit/replay` | `audit_replay` | admin, maintenance_manager, executive |
| GET | `/api/governance/retention` | `retention_policies` | admin, maintenance_manager, executive |
| GET | `/api/governance/retention/preview` | `retention_preview` | admin, maintenance_manager, executive |
| PATCH | `/api/governance/retention/{pid}` | `update_retention` | admin |
| GET | `/api/audit` | `audit_list` | admin, maintenance_manager, executive |
| GET | `/api/admin/users` | `list_users` | admin |
| POST | `/api/admin/users` | `create_user` | admin |
| PATCH | `/api/admin/users/{uid}/status` | `set_user_status` | admin |
| GET | `/api/admin/roles` | `list_roles` | authenticated |

### 5u. Report routes (lines 1533-1558)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/reports/snapshots` | `report_snapshots` | authenticated |
| GET | `/api/reports/snapshots/{rid}` | `report_snapshot` | authenticated |
| GET | `/api/reports/snapshots/{rid}/verify` | `verify_report_snapshot` | authenticated |
| GET | `/api/reports/snapshots/{rid}/html` | `report_snapshot_html` | authenticated |

### 5v. Field service (line 2063-2069)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/field/my-work` | `my_work` | authenticated |
| POST | `/api/field/assets/{aid}/condition-meter` | `field_asset_update` | WORK_ROLES |

### 5w. Health / root (lines 1059-1080, 2608-2611)

| Method | Path | Function | Roles |
|---|---|---|---|
| GET | `/api/health` | `health` | public |
| GET | `/api/health/ready` | `health_ready` | public |
| GET | `/` | `root` | public |

---

## 6. Recommended Extraction Order (lowest risk first)

### Phase 0: Create shared helpers module (prerequisite for all phases)

**Target:** `app/helpers.py` (~120 lines)

Extract first because every subsequent module depends on these functions.

| Symbol | Source Line | Rationale |
|---|---|---|
| `rows` | 105 | Used everywhere |
| `one` | 106-107 | Used in admin, automation |
| `audit` | 109-117 | Used in every write handler |
| `verify_audit_chain` | 119-122 | Used by audit integrity route |
| `post_cost` | 124-128 | Used by work order material/issue |
| `notify` | 599-600 | Used everywhere |
| `notify_once` | 813-818 | Used in automation, SLA, telemetry |
| `workflow_event` | 602-605 | Used in work order, procurement, dispatch |
| `create_approval` | 607-613 | Used in work order, procurement, PM, automation |
| `resolve_approval` | 615-620 | Used in work order transition, cancel |
| `emit_event` | 643-646 | Used in telemetry, outage, SLA |
| `next_no` | 622-629 | Used for every sequence generation |
| `get_or_404` | 631-634 | Used in almost every route |
| `user_id_by_username` | 636-637 | Used rarely but shared |
| `_dt` | 639-641 | Used in SLA, outage, reliability |
| `_csv_safe_cell` | 823-834 | Used in CSV export |
| `csv_response` | 837-841 | Used in every export route |

**Exported constants to include in helpers.py:**

```python
WRITE_ROLES, WORK_ROLES, INV_ROLES, DOC_WRITE_ROLES, PROC_ROLES, HSE_ROLES, PROJECT_ROLES
```

**Import contract:** `from .helpers import rows, one, audit, ...`

**Risk:** Very low. Pure utility functions with no state.

**Verification:** Run `pytest` and `python -m py_compile app/application.py` after extraction.

---

### Phase 1: Extract Pydantic models

**Target:** `app/schemas.py` (~130 lines)

Move all 50+ Pydantic model classes (lines 938-1057 plus `RetentionPatch` at 2567) to a single schema module.

**Import contract:** `from .schemas import LoginIn, WorkOrderIn, ...`

**Risk:** Very low. Models are data-only classes with no logic.

**Verification:** `python -c "from app.schemas import *"` and full test suite.

---

### Phase 2: Extract auth routes

**Target:** `app/routes/auth.py` (~140 lines)

| Handler | Lines |
|---|---|
| `login` | 1082-1095 |
| `logout` | 1097-1101 |
| `me` | 1102-1103 |
| `update_profile` | 1104-1118 |
| `change_password` | 1120-1132 |
| `list_sessions` | 1134-1139 |
| `revoke_other_sessions` | 1141-1147 |

**Also move:** Login brute-force helpers (`_login_key`, `_login_is_blocked`, `_login_failure`, `_login_success`, `_LOGIN_FAILURES`, `LOGIN_WINDOW_SECONDS`, `LOGIN_MAX_FAILURES`) at lines 41-94.

**Dependencies:** `helpers.py` (audit, get_or_404, rows, now), `schemas.py` (LoginIn, ProfilePatch, PasswordChange), auth module.

**Risk:** Low. Isolated authentication concern with no cross-domain coupling.

---

### Phase 3: Extract field service routes

**Target:** `app/routes/field.py` (~30 lines)

| Handler | Lines |
|---|---|
| `my_work` | 2064-2069 |
| `field_asset_update` | 1827-1834 |

**Dependencies:** `helpers.py` (rows, get_or_404, audit, WO_SELECT, WORK_ROLES), `schemas.py` (FieldAssetUpdate).

**Risk:** Very low. Two small handlers.

---

### Phase 4: Extract workforce routes

**Target:** `app/routes/workforce.py` (~170 lines)

| Handler | Lines |
|---|---|
| `workforce_crafts` | 1188-1190 |
| `workforce_shifts` | 1192-1194 |
| `workforce_technicians` | 1196-1207 |
| `upsert_technician_profile` | 1209-1219 |
| `add_shift_assignment` | 1221-1230 |
| `workforce_absences` | 1232-1237 |
| `create_absence` | 1239-1249 |
| `workforce_capacity` | 1251-1257 |

**Move with:** `_forecast_bucket_start`, `_parse_days_of_week`, `_workforce_week_capacity` (lines 169-228).

**Dependencies:** `helpers.py` (rows, get_or_404, audit, now), `schemas.py` (TechnicianProfileIn, ShiftAssignmentIn, AbsenceIn).

**Risk:** Low. Self-contained workforce domain.

---

### Phase 5: Extract inspection routes

**Target:** `app/routes/inspection.py` (~40 lines)

| Handler | Lines |
|---|---|
| `list_inspections` | 2072-2074 |
| `get_inspection` | 2075-2078 |
| `create_inspection` | 2079-2087 |
| `submit_inspection` | 2088-2097 |

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, notify, now), `schemas.py` (InspectionIn, InspectionSubmit), WORK_ROLES.

**Risk:** Low. Self-contained, small surface area.

---

### Phase 6: Extract HSE routes

**Target:** `app/routes/hse.py` (~40 lines)

| Handler | Lines |
|---|---|
| `list_hse` | 2100-2102 |
| `create_hse` | 2103-2108 |
| `update_hse` | 2109-2124 |

**Dependencies:** `helpers.py` (rows, get_or_404, one, next_no, audit, notify, now), `schemas.py` (HSEIn, HSEPatch), HSE_ROLES.

**Risk:** Low. Three small handlers.

---

### Phase 7: Extract project routes

**Target:** `app/routes/project.py` (~60 lines)

| Handler | Lines |
|---|---|
| `list_projects` | 2133-2144 |
| `create_project` | 2145-2148 |
| `create_project_task` | 2149-2156 |
| `update_project_task` | 2157-2170 |

**Move with:** `_recalculate_project_progress` (lines 2126-2130).

**Dependencies:** `helpers.py` (rows, one, get_or_404, next_no, audit, now), `schemas.py` (ProjectIn, ProjectTaskIn, ProjectTaskPatch), PROJECT_ROLES.

**Risk:** Low. Four handlers plus one helper.

---

### Phase 8: Extract vendor/contract routes

**Target:** `app/routes/vendor.py` (~30 lines)

| Handler | Lines |
|---|---|
| `vendors` | 2173-2175 |
| `create_vendor` | 2176-2179 |
| `contracts` | 2180-2182 |
| `create_contract` | 2183-2186 |

**Dependencies:** `helpers.py` (rows, next_no, audit, now), `schemas.py` (VendorIn, ContractIn).

**Risk:** Very low. Four small handlers.

---

### Phase 9: Extract operations/outage routes

**Target:** `app/routes/outage.py` (~80 lines)

| Handler | Lines |
|---|---|
| `list_outages` | 1961-1972 |
| `create_outage` | 1974-1985 |
| `close_outage` | 1987-2001 |

**Move with:** `_outage_overlap_hours` (lines 489-495).

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, notify, emit_event, _dt, now), `schemas.py` (OutageIn, OutageCloseIn).

**Risk:** Low. Three handlers, one helper.

---

### Phase 10: Extract dispatch routes

**Target:** `app/routes/dispatch.py` (~80 lines)

| Handler | Lines |
|---|---|
| `list_dispatch` | 2003-2012 |
| `dispatch_board` | 2014-2025 |
| `dispatch_work` | 2027-2040 |
| `transition_dispatch` | 2042-2061 |

**Move with:** `_active_dispatch_holder` (lines 1632-1637).

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, notify, emit_event, workflow_event, _mark_sla_response, now), `schemas.py` (DispatchIn, DispatchTransitionIn), WO_SELECT, WORK_ROLES.

**Risk:** Low-medium. `dispatch_board` uses WO_SELECT and interacts with work order state.

---

### Phase 11: Extract inventory routes

**Target:** `app/routes/inventory.py` (~80 lines)

| Handler | Lines |
|---|---|
| `list_inventory` | 1848-1853 |
| `create_inventory` | 1854-1857 |
| `inventory_tx` | 1858-1884 |
| `inventory_history` | 1885-1887 |
| `reorder_scan` | 1888-1892 |

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, notify, now), `schemas.py` (InventoryIn, InventoryTxIn), INV_ROLES, `_run_reorder_scan` (from automation).

**Risk:** Low-medium. `reorder_scan` depends on automation helper; import the function or move the scan logic.

---

### Phase 12: Extract procurement routes

**Target:** `app/routes/procurement.py` (~80 lines)

| Handler | Lines |
|---|---|
| `procurement` | 1895-1909 |
| `create_pr` | 1910-1915 |
| `submit_pr` | 1916-1924 |
| `approve_pr` | 1926-1931 |
| `create_quote` | 1932-1935 |
| `create_po` | 1937-1944 |
| `receive_po` | 1945-1956 |

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, create_approval, resolve_approval, workflow_event, now), `schemas.py` (PRIn, POIn, QuoteIn), PROC_ROLES.

**Risk:** Low-medium. Seven handlers with approval workflow dependencies.

---

### Phase 13: Extract document routes

**Target:** `app/routes/document.py` (~90 lines)

| Handler | Lines |
|---|---|
| `documents` | 2189-2191 |
| `upload_document` | 2192-2208 |
| `download_document` | 2241-2247 |

**Move with:** `_DOCUMENT_MEDIA_TYPES`, `_document_media_type`, `_document_download_name` (lines 2213-2238).

**Dependencies:** `helpers.py` (rows, next_no, audit, now), config constants (UPLOAD_DIR, MAX_UPLOAD_BYTES, etc.), DOC_WRITE_ROLES.

**Risk:** Low. Three handlers with file I/O; the upload handler has file-system side effects but no cross-domain coupling.

---

### Phase 14: Extract SLA routes

**Target:** `app/routes/sla.py` (~80 lines)

| Handler | Lines |
|---|---|
| `sla_summary` | 2250-2263 |
| `sla_policies` | 2265-2267 |
| `update_sla_policy` | 2269-2281 |
| `sla_work_orders` | 2283-2291 |
| `sla_event_list` | 2293-2295 |

**Move with (already domain):** `_ensure_work_sla`, `_backfill_work_order_slas`, `_mark_sla_response`, `_mark_sla_resolution`, `_run_sla_scan` (lines 735-792).

**Dependencies:** `helpers.py` (rows, get_or_404, audit, notify_once, emit_event, _dt, now), `schemas.py` (SLAPolicyPatch).

**Risk:** Low-medium. Five route handlers plus five domain helpers. The SLA scan is called by automation; keep it importable.

---

### Phase 15: Extract telemetry routes

**Target:** `app/routes/telemetry.py` (~250 lines)

| Handler | Lines |
|---|---|
| `telemetry_channels` | 1430-1438 |
| `create_telemetry_channel` | 1440-1447 |
| `update_telemetry_channel` | 1449-1457 |
| `ingest_telemetry` | 1459-1474 |
| `telemetry_readings` | 1476-1483 |
| `alarms` | 1485-1493 |
| `acknowledge_alarm` | 1495-1500 |
| `close_alarm` | 1502-1507 |
| `alarm_create_work_order` | 1509-1517 |
| `operations_intelligence` | 1519-1529 |

**Move with:** `_channel_site`, `_telemetry_alarm_level`, `_event_instant_or_none`, `_count_stale_channels`, `_evaluate_telemetry_alarm`, `_operations_intelligence` (lines 648-733).

**Dependencies:** `helpers.py` (rows, get_or_404, next_no, audit, notify_once, emit_event, now), `schemas.py` (TelemetryChannelIn, TelemetryChannelPatch, TelemetryIngestIn, AlarmWorkOrderIn), `_ensure_work_sla`, `create_approval`, `workflow_event`.

**Risk:** Medium. Ten handlers plus seven domain helpers. The alarm evaluation has concurrency retry logic. `alarm_create_work_order` creates work orders, creating a cross-domain dependency.

---

### Phase 16: Extract reliability routes

**Target:** `app/routes/reliability.py` (~250 lines)

| Handler | Lines |
|---|---|
| `reliability_assets` | 1259-1261 |
| `reliability_sites` | 1263-1265 |
| `reliability_set_customers` | 1268-1278 |
| `reliability_get_customers` | 1280-1284 |
| `reliability_indices` | 1314-1317 |
| `backlog_risk_weighted` | 1321-1334 |

**Move with:** `_asset_reliability_rows`, `_bad_actor_rows`, `_site_reliability_rows`, `_distribution_indices_report` (lines 497-597, 1286-1312).

**Dependencies:** `helpers.py` (rows, get_or_404, audit, _dt, now), `schemas.py` (ReliabilityCustomersIn, FMEAIn, FMEAPatch, SiteCustomerCountPatch), `_outage_overlap_hours`, `_save_asset_health`, kpi_store.

**Risk:** Medium. Six handlers plus four domain helpers. `_distribution_indices_report` imports from kpi_store.

---

### Phase 17: Extract dashboard routes

**Target:** `app/routes/dashboard.py` (~80 lines)

| Handler | Lines |
|---|---|
| `list_workflow_events` | 1338-1345 |
| `launchpad` | 1348-1352 |
| `dashboard` | 1354-1403 |
| `reference` | 1406-1409 |
| `operations` | 1410-1419 |
| `map_data` | 1420-1427 |

**Dependencies:** `helpers.py` (rows, _dt), `_asset_health`, `_asset_reliability_rows`, `_site_reliability_rows`, `_maintenance_forecast`, `_material_blocker_map`, `_operations_intelligence`, WO_SELECT, ASSET_SELECT.

**Risk:** Medium-high. `dashboard` is the largest single handler (~50 lines) and calls many domain helpers from reliability, planning, and work order domains.

---

### Phase 18: Extract work order routes

**Target:** `app/routes/work_order.py` (~500 lines)

| Handler | Lines |
|---|---|
| `list_work` | 1571-1580 |
| `work_backlog` | 1581-1583 |
| `get_work` | 1584-1598 |
| `create_work` | 1599-1611 |
| `update_work` | 1612-1621 |
| `transition_work` | 1657-1682 |
| `work_parts_readiness` | 1683-1686 |
| `add_work_requirement` | 1688-1698 |
| `delete_work_requirement` | 1700-1704 |
| `list_work_reservations` | 1706-1709 |
| `reserve_work_material` | 1711-1726 |
| `reserve_all_work_materials` | 1728-1744 |
| `release_reservation` | 1746-1754 |
| `issue_reservation` | 1756-1776 |
| `add_work_craft_requirement` | 1778-1786 |
| `add_labor` | 1788-1792 |
| `add_material` | 1793-1816 |
| `add_work_note` | 1817-1822 |
| `toggle_work_task` | 1823-1826 |
| `work_report` | 1836-1841 |

**Move with:** `_reservation_rows`, `_sync_reserved_stock`, `_work_order_parts_readiness`, `_material_blocker_map`, `_ranked_work_backlog`, `_settle_cancelled_work`, `_active_dispatch_holder`, `TRANSITIONS`, `ACTION_ROLES`, `_BACKLOG_PRIORITY_WEIGHT`, `_BACKLOG_CRITICALITY_WEIGHT` (lines 230-486, 1622-1656).

**Dependencies:** `helpers.py` (rows, get_or_404, one, next_no, audit, notify, post_cost, workflow_event, create_approval, resolve_approval, emit_event, now), `schemas.py` (WorkOrderIn, WorkOrderPatch, TransitionIn, NoteIn, LaborIn, MaterialIn, WorkRequirementIn, CraftRequirementIn, ReservationIn, ReservationIssueIn), `_ensure_work_sla`, `_mark_sla_response`, `_mark_sla_resolution`, WO_SELECT, WRITE_ROLES, WORK_ROLES, INV_ROLES.

**Risk:** High. This is the largest extraction group (~500 lines, 20 handlers). Dependencies span helpers, SLA, approval, notification, inventory reservation, and dispatch domains.

---

### Phase 19: Extract export routes

**Target:** `app/routes/export.py` (~100 lines)

All 15 CSV export handlers (lines 2364-2448).

**Dependencies:** `helpers.py` (rows, csv_response, WO_SELECT, ASSET_SELECT), `_asset_health`, `_maintenance_forecast`, `_workforce_week_capacity`, `_forecast_bucket_start`, `_asset_reliability_rows`, `_backfill_work_order_slas`.

**Risk:** Medium. Many handlers depend on domain helpers from other extracted modules. Each export handler is simple, but the dependency surface is wide.

---

### Phase 20: Extract admin routes

**Target:** `app/routes/admin.py` (~200 lines)

| Handler | Lines |
|---|---|
| `admin_backup` | 2450-2471 |
| `backup_history` | 2473-2475 |
| `notifications` | 2478-2480 |
| `notification_read` | 2481-2483 |
| `notifications_read_all` | 2484-2488 |
| `search` | 2489-2502 |
| `analytics` | 2503-2528 |
| `audit_integrity` | 2529-2531 |
| `audit_replay` | 2533-2546 |
| `retention_policies` | 2548-2550 |
| `retention_preview` | 2552-2565 |
| `update_retention` | 2569-2574 |
| `audit_list` | 2576-2584 |
| `list_users` | 2585-2587 |
| `create_user` | 2588-2591 |
| `set_user_status` | 2592-2603 |
| `list_roles` | 2604-2606 |

**Dependencies:** `helpers.py` (rows, get_or_404, one, next_no, audit, verify_audit_chain, csv_response, now), `schemas.py` (UserIn, UserStatusIn, RetentionPatch), `_asset_health`, `_asset_reliability_rows`, `_site_reliability_rows`, `_maintenance_forecast`, `ASSET_SELECT`, `WO_SELECT`, config constants, audit_verification module, sqlite3.

**Risk:** Medium-high. `analytics` calls many domain helpers. `admin_backup` does filesystem I/O and sqlite3 operations. `search` uses ASSET_SELECT and WO_SELECT.

---

### Phase 21: Extract automation routes

**Target:** `app/routes/automation.py` (~150 lines)

| Handler | Lines |
|---|---|
| `outbox_list` | 2297-2305 |
| `retry_outbox_event` | 2307-2313 |
| `automation_status` | 2316-2327 |
| `automation_runs` | 2329-2331 |
| `automation_run` | 2333-2338 |
| `metrics` | 2340-2362 |

**Move with:** `_execute_automation`, `_automation_loop`, `_generate_due_pm`, `_run_reorder_scan`, `_run_kpi_refresh`, `_process_outbox`, `_count_stale_channels` (lines 794-936).

**Dependencies:** `helpers.py` (rows, one, next_no, audit, notify_once, emit_event, now), `_ensure_work_sla`, `_save_asset_health`, `_asset_health`, `_maintenance_forecast`, `_workforce_week_capacity`, `_run_sla_scan`, `_backfill_work_order_slas`, config constants, asyncio.

**Risk:** Medium-high. The automation engine orchestrates PM generation, reorder scan, SLA scan, outbox processing, health recalculation, and notifications. The `metrics` handler is read-only but queries many tables.

---

### Phase 22: Extract report routes

**Target:** `app/routes/report.py` (~30 lines)

| Handler | Lines |
|---|---|
| `report_snapshots` | 1533-1539 |
| `report_snapshot` | 1541-1545 |
| `verify_report_snapshot` | 1547-1551 |
| `report_snapshot_html` | 1553-1558 |

**Dependencies:** `helpers.py` (rows, get_or_404), report_html module, hashlib, hmac, json.

**Risk:** Very low. Four small handlers.

---

## 7. Dependency Graph Between Extracted Modules

```
helpers.py  <--- (all modules depend on this)
schemas.py  <--- (all route modules depend on this)
    |
    v
auth.py              [no outbound route deps]
field.py             [work_order (WO_SELECT)]
inspection.py        [work_order (WO_SELECT, notify)]
hse.py               [notification (notify)]
project.py           [no outbound route deps]
vendor.py            [no outbound route deps]
workforce.py         [no outbound route deps]
outage.py            [notification (notify), events (emit_event)]
dispatch.py          [work_order (WO_SELECT, _mark_sla_response, workflow_event)]
inventory.py         [automation (_run_reorder_scan), notification (notify)]
procurement.py       [approval (create_approval, resolve_approval), workflow]
document.py          [config only]
sla.py               [notification (notify_once), events (emit_event)]
telemetry.py         [work_order (create_approval, workflow_event, _ensure_work_sla), notification]
reliability.py       [outage (_outage_overlap_hours), asset_health, kpi_store]
dashboard.py         [reliability, planning, work_order, operations, asset_health]
work_order.py        [SLA, approval, notification, events, dispatch]
export.py            [reliability, planning, work_order, SLA]
admin.py             [audit_verification, reliability, planning, asset_health]
automation.py        [PM, reorder, SLA, health, outbox, notification]
report.py            [report_html]
```

**Critical path:** `helpers.py` and `schemas.py` must be extracted first. Everything else can proceed in parallel once the shared foundation exists.

**Circular dependency risk:** None identified. The dependency graph is a DAG.

---

## 8. Route Path Preservation

Every route path and HTTP method listed in Section 5 must remain unchanged after extraction. The route registration pattern in each extracted module must follow:

```python
# app/routes/work_order.py
from fastapi import APIRouter, Depends, Query
from ..auth import current_user, require_roles
from ..helpers import rows, get_or_404, ...
from ..schemas import WorkOrderIn, ...

router = APIRouter(prefix="/api", tags=["work-orders"])

@router.get("/work-orders")
def list_work(...): ...

@router.get("/work-orders/{wo_id}")
def get_work(...): ...
```

The main `application.py` must include each router:

```python
from .routes.auth import router as auth_router
from .routes.work_order import router as work_order_router
# ...
app.include_router(auth_router)
app.include_router(work_order_router)
# ...
```

Static mounts (`/uploads`, `/static`) and the root route (`/`) remain in application.py.

---

## 9. Pydantic Model Ownership

| Module | Models |
|---|---|
| `schemas.py` | All 50+ models (LoginIn through KPIRecalcIn, plus RetentionPatch) |
| `routes/auth.py` | Imports: LoginIn, ProfilePatch, PasswordChange |
| `routes/work_order.py` | Imports: WorkOrderIn, WorkOrderPatch, TransitionIn, NoteIn, LaborIn, MaterialIn, WorkRequirementIn, CraftRequirementIn, ReservationIn, ReservationIssueIn |
| `routes/telemetry.py` | Imports: TelemetryChannelIn, TelemetryChannelPatch, TelemetryIngestIn, TelemetryReadingItem, AlarmWorkOrderIn |
| `routes/workforce.py` | Imports: TechnicianProfileIn, ShiftAssignmentIn, AbsenceIn |
| `routes/inspection.py` | Imports: InspectionIn, InspectionSubmit |
| `routes/hse.py` | Imports: HSEIn, HSEPatch |
| `routes/project.py` | Imports: ProjectIn, ProjectTaskIn, ProjectTaskPatch |
| `routes/vendor.py` | Imports: VendorIn, ContractIn |
| `routes/inventory.py` | Imports: InventoryIn, InventoryTxIn |
| `routes/procurement.py` | Imports: PRIn, POIn, QuoteIn |
| `routes/dispatch.py` | Imports: DispatchIn, DispatchTransitionIn |
| `routes/outage.py` | Imports: OutageIn, OutageCloseIn |
| `routes/reliability.py` | Imports: ReliabilityCustomersIn, FMEAIn, FMEAPatch, SiteCustomerCountPatch |
| `routes/sla.py` | Imports: SLAPolicyPatch |
| `routes/admin.py` | Imports: UserIn, UserStatusIn, RetentionPatch |
| `routes/automation.py` | Imports: PMIn, KPIIn, KPIPatch, KPIRecalcIn |

---

## 10. SQL Constant Ownership

| Constant | Lines | Home Module | Consumers |
|---|---|---|---|
| `ASSET_SELECT` | 1532 | `helpers.py` or `routes/dashboard.py` (shared) | dashboard, operations, search, analytics, export |
| `WO_SELECT` | 1570 | `helpers.py` or `routes/dashboard.py` (shared) | work_order, dispatch_board, search, analytics, export, dashboard |
| `TRANSITIONS` | 1622 | `routes/work_order.py` | work_order transition handler only |
| `ACTION_ROLES` | 1623-1630 | `routes/work_order.py` | work_order transition handler only |
| `_BACKLOG_PRIORITY_WEIGHT` | 314 | `routes/work_order.py` | _ranked_work_backlog only |
| `_BACKLOG_CRITICALITY_WEIGHT` | 315 | `routes/work_order.py` | _ranked_work_backlog only |
| `_DOCUMENT_MEDIA_TYPES` | 2213-2226 | `routes/document.py` | download_document only |
| `_CSV_FORMULA_PREFIXES` | 820 | `helpers.py` | _csv_safe_cell only |

**Recommendation:** `ASSET_SELECT` and `WO_SELECT` are used by 5+ modules each. Place them in `helpers.py` to avoid circular imports. Alternative: define them in the module that owns the primary route and re-export.

---

## 11. Shared Constants to Move

| Constant | Lines | Target | Used By |
|---|---|---|---|
| `WRITE_ROLES` | 96 | `helpers.py` | work_order, asset |
| `WORK_ROLES` | 97 | `helpers.py` | work_order, inspection, dispatch, field, workforce |
| `INV_ROLES` | 98 | `helpers.py` | work_order (material, issue), inventory |
| `DOC_WRITE_ROLES` | 99 | `helpers.py` | document |
| `PROC_ROLES` | 100 | `helpers.py` | procurement |
| `HSE_ROLES` | 101 | `helpers.py` | hse |
| `PROJECT_ROLES` | 102 | `helpers.py` | project |
| `LOGIN_WINDOW_SECONDS` | 42 | `routes/auth.py` | login brute-force only |
| `LOGIN_MAX_FAILURES` | 43 | `routes/auth.py` | login brute-force only |
| `_REQUEST_METRICS` | 45 | `middleware.py` or stay in application.py | security_headers, metrics |
| `_LOGIN_FAILURES` | 41 | `routes/auth.py` | login brute-force only |
| `logger` | 46 | `routes/automation.py` (or application.py) | automation loop |

---

## 12. Risk Assessment Summary

| Phase | Extraction | Lines | Risk | Rationale |
|---|---|---|---|---|
| 0 | helpers.py | ~120 | Very low | Pure utilities, no state |
| 1 | schemas.py | ~130 | Very low | Data-only classes |
| 2 | auth routes | ~140 | Low | Isolated authentication |
| 3 | field service | ~30 | Very low | Two small handlers |
| 4 | workforce | ~170 | Low | Self-contained domain |
| 5 | inspection | ~40 | Low | Small surface area |
| 6 | HSE | ~40 | Low | Three handlers |
| 7 | project | ~60 | Low | Four handlers + 1 helper |
| 8 | vendor/contract | ~30 | Very low | Four small handlers |
| 9 | operations/outage | ~80 | Low | Three handlers + 1 helper |
| 10 | dispatch | ~80 | Low-medium | WO_SELECT dependency |
| 11 | inventory | ~80 | Low-medium | automation dependency |
| 12 | procurement | ~80 | Low-medium | approval workflow |
| 13 | document | ~90 | Low | File I/O only |
| 14 | SLA | ~80 | Low-medium | automation dependency |
| 15 | telemetry | ~250 | Medium | Concurrency logic, cross-domain |
| 16 | reliability | ~250 | Medium | kpi_store import |
| 17 | dashboard | ~80 | Medium-high | Many domain helper calls |
| 18 | work order | ~500 | High | Largest, many dependencies |
| 19 | export | ~100 | Medium | Wide dependency surface |
| 20 | admin | ~200 | Medium-high | analytics, backup I/O |
| 21 | automation | ~150 | Medium-high | Orchestrator |
| 22 | report | ~30 | Very low | Four small handlers |

---

## 13. Post-Extraction application.py

After all phases, `application.py` should contain only:

1. Imports (~20 lines)
2. Lifespan context manager (~15 lines)
3. FastAPI app instantiation (~5 lines)
4. Security headers middleware (~30 lines)
5. Router includes (~30 lines)
6. Static mounts + root route (~5 lines)

**Target size: ~105 lines** (down from 2611).

The middleware (`security_headers`) and request metrics (`_REQUEST_METRICS`) should stay in application.py since they apply to all routes globally. Alternatively, the middleware can be extracted to `app/middleware.py` and attached via `app.add_middleware()`.

---

## 14. Migration Checklist

- [ ] Create `app/helpers.py` with all shared utilities and role constants
- [ ] Create `app/schemas.py` with all Pydantic models
- [ ] Create `app/routes/` directory with `__init__.py`
- [ ] Extract phases 2-22 one at a time, running tests after each
- [ ] Verify every route path/method/role is preserved (diff against OpenAPI spec)
- [ ] Run `python -m py_compile app/application.py` after each extraction
- [ ] Run full test suite after each extraction
- [ ] Update any external imports that reference symbols from application.py
- [ ] After all phases, verify application.py is under 120 lines
- [ ] Generate and diff OpenAPI schema before and after decomposition
