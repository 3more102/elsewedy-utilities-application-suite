# EUAS Workforce Planning & Reliability

EUAS v3.9.0 adds operational workforce-capacity planning, planned-parts readiness and asset/site reliability calculations to the existing maintenance planning layer.

## Workforce data model

The reference implementation uses these first-class entities:

- `crafts` — maintenance disciplines and reference labor rates.
- `technician_profiles` — technician craft, home site, weekly hours, efficiency and active state.
- `shift_templates` — shift start/end and paid hours.
- `technician_shift_assignments` — technician-to-shift assignments with effective dates and weekdays.
- `technician_absences` — approved/pending leave, training or other unavailability.
- `work_order_craft_requirements` — planned craft hours for work execution.
- `work_order_requirements` — planned spare-part quantities and required-by dates.

The demo includes two High Voltage Electrical technicians at New Cairo Substation, day-shift assignments and a future approved absence so capacity variation is visible immediately.

## Capacity calculation

`GET /api/workforce/capacity` calculates weekly available productive hours. For each technician the reference calculation is:

```text
scheduled shift hours
- approved absence hours
× productive efficiency %
= available maintenance capacity
```

If an upgraded database contains active technician users but no workforce profiles yet, EUAS returns an explicitly labelled `role_fallback` capacity instead of silently pretending profile-based planning exists.

Site filtering uses each technician's `home_site_id`. Craft capacity is returned separately so the maintenance forecast can identify craft-specific gaps.

## Maintenance forecast integration

`GET /api/planning/maintenance-forecast` now combines:

- calendar PM demand
- unresolved backlog and estimated hours
- site-filtered workforce capacity
- planned craft-hour requirements
- planned spare-part readiness

Each weekly bucket reports:

- PM jobs
- backlog jobs
- demand hours
- available capacity hours
- utilization
- craft demand/capacity/shortage
- parts-ready jobs
- parts-shortage jobs
- jobs with unknown parts requirements

### Parts readiness

`GET /api/work-orders/{id}/parts-readiness` compares each planned requirement against live available stock:

```text
available stock = current stock - reserved stock
```

A work order is `Ready` only when every planned item is available in the required quantity. It becomes `Shortage` when at least one requirement cannot be met. A work order with no planned requirements is reported as `Unknown`; EUAS does not treat missing material planning as proven readiness.

The reference forecast evaluates readiness per work order. It does not yet perform portfolio-wide material allocation optimization where multiple future jobs compete for the same inventory simultaneously.

## Reliability calculations

`GET /api/reliability/assets?period_days=365` computes reliability per asset from completed/closed corrective or breakdown work in the requested analysis window.

For each asset:

```text
Failures  = completed corrective/breakdown events
Downtime  = sum(actual_hours) for those failures
MTTR      = downtime / failures
Uptime    = analysis-period operating hours - downtime
MTBF      = uptime / failures
Availability = uptime / analysis-period hours × 100
```

If no failure occurred in the period, MTBF is returned as `null` rather than inventing a finite value. MTTR is `0` in that case and availability remains based on recorded corrective downtime.

`GET /api/reliability/sites` aggregates the same operating-time method across assets at each site.

### Interpretation limits

The reference calculation uses work-order `actual_hours` as downtime because the demo data model does not yet contain separate outage-start/outage-end telemetry. In a production utility deployment, reliability should preferentially use SCADA/OMS outage timestamps, equipment operating counters, service interruption data and formally classified failure events.

## APIs

```text
GET  /api/workforce/crafts
GET  /api/workforce/shifts
GET  /api/workforce/technicians
PUT  /api/workforce/technicians/{user_id}
POST /api/workforce/technicians/{user_id}/shift-assignments
GET  /api/workforce/absences
POST /api/workforce/absences
GET  /api/workforce/capacity

GET  /api/work-orders/{id}/parts-readiness
POST /api/work-orders/{id}/requirements
DELETE /api/work-orders/{id}/requirements/{requirement_id}
POST /api/work-orders/{id}/craft-requirements

GET  /api/reliability/assets
GET  /api/reliability/sites
```

Exports:

```text
/api/exports/workforce-capacity.csv
/api/exports/reliability.csv
/api/exports/maintenance-forecast.csv
```

Observability adds:

```text
euas_workforce_technicians
euas_parts_shortage_jobs_90d
```


## Outage-driven reliability in v3.8

When forced `asset_outages` exist inside the selected reliability window, EUAS derives failure count and downtime directly from outage start/end timestamps, including window-overlap handling. MTTR, MTBF and availability therefore use explicit outage evidence. Assets without outage history retain the previous completed corrective-work `actual_hours` method as a backwards-compatible fallback, and the API exposes `downtime_source` so consumers can distinguish the evidence basis.

## Material readiness and reservations

Planned requirements describe demand; reservations secure stock; issues record actual consumption. The readiness engine treats the work order's own active reservation as secured material while excluding reservations belonging to other work from unreserved availability. This prevents portfolio stock from appearing available twice.
