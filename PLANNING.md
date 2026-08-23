# EUAS Maintenance Planning & Asset Health

EUAS v4.6.0 extends the planning layer that connects asset risk, planned maintenance and available field capacity.

## Asset Health Engine

`GET /api/assets/health` calculates a deterministic score from 0–100 for every asset. The score is intended as an operational prioritization aid, not a machine-learning prediction or OEM diagnostic.

Inputs currently include:

- asset condition
- asset criticality
- operating status
- unresolved High/Critical/Emergency work
- overdue work orders
- failed inspections
- work-order SLA breaches

Risk bands:

| Score | Band |
|---:|---|
| 85–100 | Healthy |
| 70–84.9 | Monitor |
| 50–69.9 | Warning |
| < 50 | Critical |

`POST /api/assets/health/recalculate` persists a point-in-time snapshot in `asset_health_snapshots`. Automation runs also create snapshots so the platform can retain a health trend.

### Important limitation

The score is a transparent rules-based operational score. It does **not** claim predictive failure probability. Production deployments can replace or augment the scoring service with OEM condition-monitoring, SCADA/IoT or predictive-maintenance models while preserving the same API boundary.

## 90-Day Maintenance Forecast

`GET /api/planning/maintenance-forecast?horizon_days=90` builds weekly planning buckets from:

- active PM plans with calendar due dates
- unresolved work-order backlog
- work-order estimated labor hours
- active technician count

Each week contains:

- PM jobs
- backlog jobs
- demand hours
- shift/absence-adjusted technician capacity
- utilization percentage
- capacity state
- craft demand and craft shortage hours
- parts-ready, parts-shortage and parts-unknown job counts

The v3.7 reference capacity model is no longer `technicians × 40 hours`. It uses technician profiles, home sites, shift assignments, effective weekdays, approved absences and productive-efficiency percentages. Craft capacity is tracked separately. A labelled `role_fallback` remains only for upgraded databases that have technician users but have not yet created workforce profiles.

CSV export:

```text
/api/exports/maintenance-forecast.csv
```

## Approval Delegation

EUAS supports temporary user-to-user approval delegation through `approval_delegations`.

A delegation contains:

- delegator
- delegate
- module scope (`*`, Work Management or Procurement)
- start time
- end time
- active state
- creator and audit record

Delegated approvals remain visible to the original approver. The delegate receives authorization only while the delegation is active and within its effective date/time range. The server enforces the delegation; it is not a UI-only permission.

Automation marks expired active delegations inactive. Delegation creation and deactivation are written to the tamper-evident audit chain.

## Exports and Observability

Asset health export:

```text
/api/exports/asset-health.csv
```

Prometheus-style metrics now include:

```text
euas_asset_health_score_avg
euas_maintenance_forecast_peak_utilization_pct
```


## v3.7 Workforce and reliability

Detailed capacity, parts-readiness and MTBF/MTTR/availability methodology is documented in [WORKFORCE_RELIABILITY.md](WORKFORCE_RELIABILITY.md).
