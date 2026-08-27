# API Frontend Coverage

> Auto-generated classification of every EUAS API route by its consumption
> pattern. The backend defines **225+ routes** across 15+ Python modules.
> The frontend (`static/app.js` and `static/csp-action-bridge.js`) calls a
> large subset. Routes that are never called from the UI are documented here
> with their intended purpose.

---

## Legend

| Tag | Meaning |
|-----|---------|
| **FC** | Frontend consumed -- called from `static/app.js` or `static/csp-action-bridge.js` |
| **AO** | API-only intentionally -- designed for external integration (webhooks, SCADA, CLI) |
| **AD** | Admin-only -- only used in admin/management views |
| **IW** | Integration/webhook -- event outbox, retry mechanisms |
| **ED** | Export/download -- CSV exports, report downloads |
| **AI** | Automation/internal -- automation loop, outbox processing, metrics |
| **CP** | Compatibility -- legacy endpoints that duplicate newer functionality |
| **OR** | Orphaned -- not called from frontend or other known consumers |

---

## 1. Health

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/health` | GET | **AO** | Liveness probe; called by orchestrators, not the SPA |
| `/api/health/ready` | GET | **AO** | Readiness probe; called by load balancers |

---

## 2. Authentication

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/auth/login` | POST | **FC** | Login form in `app.js` |
| `/api/auth/logout` | POST | **FC** | Logout button |
| `/api/auth/me` | GET | **FC** | Session restore on page load |
| `/api/auth/profile` | PATCH | **FC** | Profile edit modal |
| `/api/auth/change-password` | POST | **FC** | Profile modal password change |
| `/api/auth/sessions` | GET | **FC** | Profile modal session list |
| `/api/auth/sessions/revoke-others` | POST | **FC** | Profile modal revoke button |

---

## 3. Reference Data

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/reference` | GET | **FC** | Boot sequence; loads sites, locations, asset types, vendors, users, warehouses |

---

## 4. Assets

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/assets` | GET | **FC** | Asset list, work-order picker, dropdowns |
| `/api/assets` | POST | **FC** | Create asset modal |
| `/api/assets/{id}` | GET | **FC** | Asset detail modal |
| `/api/assets/{id}` | PATCH | **FC** | Edit asset modal |
| `/api/assets/{id}` | DELETE | **CP** | Destructive action; not called from UI |
| `/api/assets/{id}/timeline` | GET | **FC** | Asset detail modal timeline |
| `/api/assets/{id}/health` | GET | **FC** | Asset detail modal health |
| `/api/assets/{id}/dossier` | POST | **OR** | Generate asset dossier snapshot |
| `/api/assets/health` | GET | **FC** | Dashboard asset health bands |
| `/api/assets/health/recalculate` | POST | **OR** | Not called from UI |
| `/api/assets-export.csv` | GET | **FC** | Asset list CSV export button |
| `/api/meters/{id}/readings` | POST | **OR** | Add meter reading; no UI path |

---

## 5. Work Orders

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/work-orders` | GET | **FC** | Work list view |
| `/api/work-orders` | POST | **FC** | Create work-order modal |
| `/api/work-orders/backlog` | GET | **OR** | Risk-weighted backlog; not called from UI (used by Intelligence view indirectly via KPI) |
| `/api/work-orders/{id}` | GET | **FC** | Work-order detail modal |
| `/api/work-orders/{id}` | PATCH | **FC** | Edit work-order modal |
| `/api/work-orders/{id}/transition` | POST | **FC** | Status transition buttons |
| `/api/work-orders/{id}/parts-readiness` | GET | **OR** | Dedicated endpoint; UI uses readiness embedded in detail response |
| `/api/work-orders/{id}/requirements` | POST | **FC** | Plan Part modal |
| `/api/work-orders/{id}/requirements/{rid}` | DELETE | **OR** | Remove planned part; no UI button |
| `/api/work-orders/{id}/reservations` | GET | **OR** | Listing; UI uses reservations embedded in detail response |
| `/api/work-orders/{id}/reservations` | POST | **OR** | Reserve material; UI issues directly via material modal |
| `/api/work-orders/{id}/reserve-all` | POST | **OR** | Reserve all planned materials; no UI button |
| `/api/work-orders/{id}/craft-requirements` | POST | **FC** | Plan Craft modal |
| `/api/work-orders/{id}/labor` | POST | **FC** | Labor modal |
| `/api/work-orders/{id}/materials` | POST | **FC** | Issue Part modal |
| `/api/work-orders/{id}/notes` | POST | **FC** | Add Note modal |
| `/api/work-orders/{id}/tasks/{tid}/toggle` | POST | **FC** | Checklist toggle button |
| `/api/work-orders/{id}/report` | GET | **FC** | Work-order report via `openProtected` |
| `/api/work-orders/{id}/dispatch` | POST | **FC** | Dispatch modal |

---

## 6. Reservations

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/reservations/{id}/release` | POST | **FC** | Release button in work-order detail |
| `/api/reservations/{id}/issue` | POST | **FC** | Issue button in work-order detail |

---

## 7. Inventory

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/inventory` | GET | **FC** | Inventory list, part pickers |
| `/api/inventory` | POST | **FC** | Create inventory item modal |
| `/api/inventory/{id}/transaction` | POST | **FC** | Transaction modal |
| `/api/inventory/{id}/transactions` | GET | **FC** | History modal |
| `/api/inventory/reorder-scan` | POST | **FC** | Reorder Scan button |

---

## 8. Procurement

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/procurement` | GET | **FC** | Procurement view |
| `/api/procurement/requisitions` | POST | **FC** | Create PR modal |
| `/api/procurement/requisitions/{id}/submit` | POST | **FC** | Submit button |
| `/api/procurement/requisitions/{id}/approve` | POST | **FC** | Approve button |
| `/api/procurement/quotations` | POST | **FC** | Add Quote modal |
| `/api/procurement/purchase-orders` | POST | **FC** | Create PO modal |
| `/api/procurement/purchase-orders/{id}/receive` | POST | **FC** | Receive button |

---

## 9. Telemetry

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/telemetry/channels` | GET | **FC** | Telemetry view, ingest picker |
| `/api/telemetry/channels` | POST | **FC** | Create channel modal |
| `/api/telemetry/channels/{id}` | PATCH | **OR** | Update channel; not called from UI |
| `/api/telemetry/ingest` | POST | **FC** | Ingest Reading modal |
| `/api/telemetry/readings` | GET | **OR** | Reading history; not surfaced in UI |

---

## 10. Alarms

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/alarms` | GET | **FC** | Telemetry alarm queue |
| `/api/alarms/{id}/acknowledge` | POST | **FC** | Acknowledge button |
| `/api/alarms/{id}/close` | POST | **FC** | Close button |
| `/api/alarms/{id}/work-order` | POST | **FC** | Create WO button |

---

## 11. Operations

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/operations` | GET | **FC** | Operations view |
| `/api/operations/intelligence` | GET | **FC** | Telemetry view KPI strip |
| `/api/operations/situations` | GET | **FC** | Ops Center situation list |
| `/api/operations/situations/{key}/timeline` | GET | **FC** | Situation timeline modal |
| `/api/operations/why-red` | GET | **FC** | "Why is this red?" button |
| `/api/operations/recommendations` | GET | **FC** | "What should I do?" panel |
| `/api/operations/blocker-chain/{wo_id}` | GET | **FC** | Material blocker chain button |
| `/api/operations/inbox` | GET | **FC** | Operations inbox panel |

---

## 12. Outages

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/outages` | GET | **FC** | Outages list in operations view |
| `/api/outages` | POST | **FC** | Record Outage modal |
| `/api/outages/{id}/close` | POST | **FC** | Close outage button |

---

## 13. Dispatch

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/dispatch` | GET | **FC** | Dispatch activity table |
| `/api/dispatch/board` | GET | **FC** | Dispatch board KPIs and technician table |
| `/api/dispatch/{id}/transition` | POST | **FC** | Accept/En Route/Arrive/Finish buttons |

---

## 14. Field Service

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/field/my-work` | GET | **FC** | Field service tab view |
| `/api/field/assets/{id}/condition-meter` | POST | **FC** | Reading/Condition modal in field view |

---

## 15. Inspections

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/inspections` | GET | **FC** | Inspections list |
| `/api/inspections` | POST | **FC** | Create inspection modal |
| `/api/inspections/{id}` | GET | **FC** | Inspection detail/submit modal |
| `/api/inspections/{id}/submit` | POST | **FC** | Submit inspection form |

---

## 16. HSE

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/hse` | GET | **FC** | HSE list |
| `/api/hse` | POST | **FC** | Create HSE modal |
| `/api/hse/{id}` | PATCH | **FC** | Manage HSE modal |

---

## 17. Projects

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/projects` | GET | **FC** | Projects list |
| `/api/projects` | POST | **FC** | Create project modal |
| `/api/projects/{id}/tasks` | POST | **FC** | Add task button |
| `/api/projects/{id}/tasks/{tid}` | PATCH | **FC** | Edit task modal |

---

## 18. Vendors

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/vendors` | GET | **FC** | Vendor list, dropdowns |
| `/api/vendors` | POST | **FC** | Create vendor modal |

---

## 19. Contracts

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/contracts` | GET | **FC** | Contract list |
| `/api/contracts` | POST | **FC** | Create contract modal |

---

## 20. Documents

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/documents` | GET | **FC** | Document list |
| `/api/documents/upload` | POST | **FC** | Upload document modal |
| `/api/documents/{id}/download` | GET | **FC** | Download button |

---

## 21. SLA

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/sla/summary` | GET | **FC** | Automation view SLA panel |
| `/api/sla/policies` | GET | **FC** | Automation view SLA policies |
| `/api/sla/policies/{id}` | PATCH | **FC** | Edit SLA policy modal |
| `/api/sla/work-orders` | GET | **OR** | SLA work-order list; not surfaced in UI |
| `/api/sla/events` | GET | **OR** | SLA event log; not surfaced in UI |

---

## 22. Workflow Events

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/workflow-events` | GET | **FC** | Approval history modal |

---

## 23. Approvals

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/approvals` | GET | **FC** | Approvals list |
| `/api/approvals/{id}/decision` | POST | **FC** | Approve/Reject modal |
| `/api/approval-delegations` | GET | **FC** | Delegation list |
| `/api/approval-delegations` | POST | **FC** | Delegate modal |
| `/api/approval-delegations/{id}/deactivate` | PATCH | **FC** | Deactivate button |

---

## 24. Maintenance Plans

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/maintenance-plans` | GET | **FC** | Maintenance view plan list |
| `/api/maintenance-plans` | POST | **FC** | Create plan modal |
| `/api/maintenance-plans/generate` | POST | **FC** | Generate Due Work button |

---

## 25. Planning

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/planning/maintenance-forecast` | GET | **FC** | Maintenance forecast, dashboard command strip |

---

## 26. Workforce

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/workforce/crafts` | GET | **FC** | Craft picker in plan-craft modal |
| `/api/workforce/shifts` | GET | **FC** | Shift picker |
| `/api/workforce/technicians` | GET | **FC** | Technician roster |
| `/api/workforce/technicians/{id}` | PUT | **FC** | Edit profile modal |
| `/api/workforce/technicians/{id}/shift-assignments` | POST | **FC** | Assign shift modal |
| `/api/workforce/absences` | GET | **FC** | Absence calendar |
| `/api/workforce/absences` | POST | **FC** | Create absence modal |
| `/api/workforce/capacity` | GET | **FC** | 8-week capacity table |

---

## 27. Reports / Snapshots

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/reports/snapshots` | GET | **FC** | Asset detail snapshot list |
| `/api/reports/snapshots/{id}` | GET | **OR** | Raw snapshot JSON; not called from UI |
| `/api/reports/snapshots/{id}/verify` | GET | **OR** | Hash verification; not called from UI |
| `/api/reports/snapshots/{id}/html` | GET | **FC** | Open Report button |

---

## 28. Reliability

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/reliability/assets` | GET | **OR** | Per-asset reliability table; not called from UI |
| `/api/reliability/sites` | GET | **OR** | Per-site reliability table; not called from UI |
| `/api/reliability/customers` | GET | **FC** | Customer counts modal |
| `/api/reliability/customers` | PUT | **FC** | Save customer count button |
| `/api/reliability/indices` | GET | **OR** | Distribution indices report; not called from UI |
| `/api/reliability/health/{asset_id}` | GET | **OR** | Per-asset health from APM store |
| `/api/reliability/risk-matrix` | GET | **OR** | Risk matrix; not called from UI |
| `/api/reliability/deterioration-watchlist` | GET | **OR** | Deterioration watchlist; not called from UI |
| `/api/reliability/alarm-correlation` | GET | **OR** | Alarm correlation analysis; not called from UI |
| `/api/reliability/cbm-evaluation` | POST | **OR** | CBM evaluation trigger; not called from UI |
| `/api/reliability/cbm-recommendations` | GET | **OR** | CBM recommendations list; not called from UI |
| `/api/reliability/cbm-recommendations/{id}/convert-to-work-order` | POST | **OR** | Convert CBM recommendation to WO; not called from UI |
| `/api/reliability/cbm-recommendations/{id}/{action}` | POST | **OR** | Accept/dismiss CBM recommendation; not called from UI |
| `/api/reliability/bad-actors` | GET | **FC** | Intelligence view bad actors panel |
| `/api/reliability/maintenance-effectiveness` | GET | **OR** | Maintenance effectiveness analysis; not called from UI |
| `/api/reliability/fmea` | GET | **OR** | FMEA catalog list; not called from UI |
| `/api/reliability/fmea` | POST | **OR** | Create FMEA entry; not called from UI |
| `/api/reliability/fmea/{id}` | PATCH | **OR** | Update FMEA entry; not called from UI |
| `/api/reliability/fmea/{id}/approve` | POST | **OR** | Approve FMEA entry; not called from UI |
| `/api/reliability/fmea/{id}/observed-evidence` | GET | **OR** | FMEA observed evidence; not called from UI |

---

## 29. Backlog

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/backlog/risk-weighted` | GET | **FC** | Intelligence view risk-weighted backlog panel |

---

## 30. KPI System (kpi_store)

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/kpis` | GET | **FC** | Intelligence view KPI strip |
| `/api/kpis/{id}` | GET | **FC** | KPI detail modal |
| `/api/kpis/{id}/explanation` | GET | **FC** | KPI explanation modal |
| `/api/kpis/{id}/drilldown` | GET | **FC** | KPI drill-down modal |
| `/api/kpis/{id}/recalculate` | POST | **FC** | Recalculate single KPI button |
| `/api/kpis/recalculate-all` | POST | **FC** | Intelligence view Recalculate All button |
| `/api/kpis/reliability` | GET | **FC** | Dashboard reliability panel |
| `/api/kpis/reliability.csv` | GET | **ED** | Export button in reliability panel |
| `/api/kpis/workforce` | GET | **FC** | Dashboard workforce panel |
| `/api/kpis/workforce.csv` | GET | **ED** | Export button in workforce panel |
| `/api/kpis/maintenance` | GET | **FC** | Dashboard maintenance panel |
| `/api/kpis/maintenance.csv` | GET | **ED** | Export button in maintenance panel |
| `/api/kpis/inventory` | GET | **FC** | Dashboard inventory panel |
| `/api/kpis/inventory.csv` | GET | **ED** | Export button in inventory panel |
| `/api/sites/{id}/customer-count` | PATCH | **FC** | Customer count save button |

---

## 31. Executive KPIs (executive_kpi_store)

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/kpi/executive` | GET | **FC** | Analytics view condition intelligence |
| `/api/kpi/executive/refresh` | POST | **OR** | Refresh executive KPIs; not called from UI |
| `/api/kpi/backlog/risk` | GET | **OR** | Backlog risk KPI; not called from UI |
| `/api/kpi/deterioration` | GET | **OR** | Deterioration KPI; not called from UI |
| `/api/kpi/parts/shortages` | GET | **FC** | Material-blocked work shortage lines |
| `/api/kpi/pm-risk` | GET | **FC** | Dashboard PM risk panel |
| `/api/kpi/hse` | GET | **FC** | Dashboard command strip HSE panel |
| `/api/kpi/assets/{id}` | GET | **OR** | Per-asset KPI; not called from UI |
| `/api/kpi/trend` | GET | **FC** | Condition intelligence trend sparklines |
| `/api/kpi/explanation` | GET | **FC** | Condition intelligence "Why?" modal |
| `/api/exports/executive-kpis.csv` | GET | **ED** | Export button in analytics view |

---

## 32. APM Store (reliability intelligence)

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/exports/reliability/bad-actors.csv` | GET | **ED** | Export button in Intelligence view |
| `/api/exports/reliability/deterioration-watchlist.csv` | GET | **OR** | Deterioration export; not called from UI |
| `/api/exports/reliability/fmea.csv` | GET | **OR** | FMEA export; not called from UI |

---

## 33. Work-Order Effectiveness

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/work-orders/{id}/effectiveness` | GET | **OR** | Effectiveness score for a completed WO; not called from UI |

---

## 34. Automation / Metrics

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/automation/status` | GET | **FC** | Automation view status panel |
| `/api/automation/runs` | GET | **FC** | Automation view run history |
| `/api/automation/run` | POST | **FC** | Run Automation Now button |
| `/api/metrics` | GET | **AI** | Prometheus-format metrics endpoint |

---

## 35. Event Outbox

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/events/outbox` | GET | **FC** | Automation view outbox table |
| `/api/events/outbox/{id}/retry` | POST | **FC** | Retry button in outbox table |

---

## 36. Exports (CSV)

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/exports/work-orders.csv` | GET | **ED** | Automation view export button |
| `/api/exports/inventory.csv` | GET | **ED** | Automation view export button |
| `/api/exports/procurement.csv` | GET | **ED** | Automation view export button |
| `/api/exports/audit.csv` | GET | **ED** | Automation view export button |
| `/api/exports/sla.csv` | GET | **ED** | Automation view export button |
| `/api/exports/cost-ledger.csv` | GET | **ED** | Automation view export button |
| `/api/exports/asset-health.csv` | GET | **ED** | Automation view export button |
| `/api/exports/maintenance-forecast.csv` | GET | **ED** | Automation view export button |
| `/api/exports/workforce-capacity.csv` | GET | **ED** | Automation view export button |
| `/api/exports/reliability.csv` | GET | **ED** | Automation view export button |
| `/api/exports/outages.csv` | GET | **ED** | Automation view export button |
| `/api/exports/dispatch.csv` | GET | **ED** | Automation view export button |
| `/api/exports/reservations.csv` | GET | **ED** | Automation view export button |
| `/api/exports/alarms.csv` | GET | **ED** | Automation view export button |
| `/api/exports/telemetry.csv` | GET | **ED** | Automation view export button |

---

## 37. Audit / Governance

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/audit` | GET | **FC** | Admin view audit trail table |
| `/api/audit/integrity` | GET | **FC** | Admin view integrity badge |
| `/api/audit/replay` | GET | **OR** | Verified audit replay; not called from UI |
| `/api/governance/retention` | GET | **FC** | Automation view retention policies |
| `/api/governance/retention/preview` | GET | **FC** | Automation view retention preview |
| `/api/governance/retention/{id}` | PATCH | **FC** | Edit retention modal |

---

## 38. Administration

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/admin/users` | GET | **FC** | Admin user list |
| `/api/admin/users` | POST | **FC** | Create user modal |
| `/api/admin/users/{id}/status` | PATCH | **FC** | Activate/Deactivate button |
| `/api/admin/roles` | GET | **FC** | Role picker in create-user modal |
| `/api/admin/backup` | GET | **FC** | Download Backup button |
| `/api/admin/backups` | GET | **FC** | Backup history (loaded in automation view) |

---

## 39. Notifications

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/notifications` | GET | **FC** | Notification bell badge |
| `/api/notifications/{id}/read` | POST | **FC** | Notification click handler |
| `/api/notifications/read-all` | POST | **FC** | Mark all read button |

---

## 40. Search

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/search` | GET | **FC** | Global search bar debounced input |

---

## 41. Analytics

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/analytics` | GET | **FC** | Analytics view full page |

---

## 42. Map

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/map` | GET | **FC** | GIS map view |

---

## 43. Launchpad

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/launchpad` | GET | **FC** | Home view launchpad cards |

---

## 44. Dashboard

| Route | Method | Tag | Notes |
|-------|--------|-----|-------|
| `/api/dashboard` | GET | **FC** | Home, Dashboard, and Ops Center views |

---

## Summary Statistics

| Category | Count |
|----------|-------|
| Frontend consumed (FC) | ~120 |
| Export/download (ED) | ~19 |
| API-only intentionally (AO) | 2 |
| Admin-only (AD) | 0 (admin routes use FC tag) |
| Integration/webhook (IW) | 0 (outbox uses FC tag) |
| Automation/internal (AI) | 1 |
| Compatibility (CP) | 2 |
| Orphaned (OR) | ~30 |

---

## Orphaned Capabilities Worth Surfacing in the UI

The following backend features have full API implementations but are not yet
accessible from any frontend view. They represent high-value intelligence and
operational tools that could be surfaced in future UI work.

### Reliability Analytics

- **Per-asset reliability** (`/api/reliability/assets`) -- MTBF, MTTR, availability, cost per asset over configurable periods
- **Per-site reliability** (`/api/reliability/sites`) -- Aggregated site-level reliability comparison
- **Distribution indices** (`/api/reliability/indices`) -- SAIFI, SAIDI, CAIDI, ASAI computation with outage attribution
- **Risk matrix** (`/api/reliability/risk-matrix`) -- Cross-tabulation of asset condition and criticality
- **Maintenance effectiveness** (`/api/reliability/maintenance-effectiveness`) -- Ratio of planned vs corrective work and outcomes
- **Work-order effectiveness** (`/api/work-orders/{id}/effectiveness`) -- Post-completion effectiveness score for individual work orders

### Condition-Based Maintenance (CBM)

- **CBM recommendations** (`/api/reliability/cbm-recommendations`) -- ML-adjacent recommendations derived from condition data
- **CBM evaluation trigger** (`/api/reliability/cbm-evaluation`) -- On-demand evaluation of channel data against CBM rules
- **CBM-to-work-order conversion** (`/api/reliability/cbm-recommendations/{id}/convert-to-work-order`) -- One-click promotion of a recommendation to a corrective work order
- **CBM accept/dismiss** (`/api/reliability/cbm-recommendations/{id}/{action}`) -- Lifecycle management of CBM recommendations

### FMEA Catalog

- **FMEA list** (`/api/reliability/fmea`) -- Failure Mode and Effects Analysis catalog
- **FMEA create/update** (`/api/reliability/fmea`, PATCH) -- CRUD for FMEA entries
- **FMEA approval** (`/api/reliability/fmea/{id}/approve`) -- Review and approve FMEA entries
- **FMEA observed evidence** (`/api/reliability/fmea/{id}/observed-evidence`) -- Link actual failure data to FMEA predictions

### Bad Actors & Deterioration

- **Deterioration watchlist** (`/api/reliability/deterioration-watchlist`) -- Assets trending toward failure
- **Alarm correlation** (`/api/reliability/alarm-correlation`) -- Statistical correlation between alarm patterns and failures
- **Bad actors export** (`/api/exports/reliability/bad-actors.csv`) -- CSV export of bad actor analysis

### KPI Intelligence

- **KPI trend sparklines** (`/api/kpi/trend`) -- Already consumed for condition intelligence; could be generalized to all KPI families
- **KPI explanations** (`/api/kpi/explanation`) -- Already consumed for condition intelligence; could be extended to all KPIs
- **Per-asset KPIs** (`/api/kpi/assets/{id}`) -- Drill from any asset into its KPI contribution

### Telemetry

- **Channel patch** (`/api/telemetry/channels/{id}`) -- Update channel thresholds without recreating
- **Reading history** (`/api/telemetry/readings`) -- Time-series chart data for individual channels

### Operations

- **Asset dossier generation** (`/api/assets/{id}/dossier`) -- On-demand asset report snapshot
- **Audit replay** (`/api/audit/replay`) -- Verified, tamper-evident audit timeline for governance evidence

### Work-Order Operations

- **Reserve all materials** (`/api/work-orders/{id}/reserve-all`) -- One-click reservation of all planned parts
- **Parts readiness check** (`/api/work-orders/{id}/parts-readiness`) -- Dedicated readiness endpoint (currently embedded in detail response)
- **Work-order reservations list** (`/api/work-orders/{id}/reservations`) -- Dedicated listing (currently embedded in detail response)

### SLA

- **SLA work-order list** (`/api/sla/work-orders`) -- Filterable SLA compliance view
- **SLA events** (`/api/sla/events`) -- SLA breach event history

### Legacy / Compatibility

- **Asset delete** (`DELETE /api/assets/{id}`) -- Destructive action; no UI trigger (intentional)
- **Asset recalculate health** (`POST /api/assets/health/recalculate`) -- Portfolio-wide health recalculation; could be an admin tool
