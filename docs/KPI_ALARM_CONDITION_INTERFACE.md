# Alarm / Condition KPI Family — Integration Interface

Status: **externally owned** (`app/reliability.py`, `app/apm_store.py` and the
alarm/condition implementation files are actively developed by another
session; branch `oxalpha/utilities-kpi-intelligence` additionally carries a
parallel `app/kpi_engine.py`). This document defines the contract the
Executive Dashboard expects so the future family can be wired without
rework. Nothing here is implemented against owned files.

## Endpoint shape expected by `static/app.js`

The dashboard's KPI panel pattern (`kpiFamilyPanel` in `static/app.js`)
requires a single GET endpoint returning one JSON payload:

```
GET /api/kpis/<family>
```

Required top-level keys:

| Key | Meaning |
|---|---|
| `kpi_family` | Stable family id, e.g. `alarm_condition` |
| `as_of` / `window_start` / `window_end` | Measurement window actually used |
| `kpis` | Map of KPI id → definition + value (shape below) |
| `contributors` | Ranked causal contributors, newest/most-severe first |
| `data_freshness.generated_at` | Server-side generation stamp |

Optional but consumed by existing renderers when present:
`counts`, previous-window fields, per-KPI `previous_value`.

## Per-KPI entry

Each entry in `kpis` must contain:

- `id` — stable identifier (e.g. `critical_alarms_open`)
- `name`, `definition`, `formula` — explainability strings
- `unit` — display unit (`'count'`, `'hours'`, `'%'`, ...)
- `direction` — `'lower_is_better'` or `'higher_is_better'`
- `value` — number **or `null` when not computable**
- `missing_inputs` — list of missing inputs whenever `value` is `null`
  (the UI renders an honest *Unavailable* state and must never show an
  invented zero)
- `previous_value` (optional) — enables the delta/trend renderer
- `target`, `warning_threshold`, `critical_threshold` (optional)

## Expected alarm/condition KPI ids

Suggested identifiers for the future family (dashboard is id-agnostic, but
these match the product brief):

- `critical_alarms_open`
- `unacknowledged_alarms`
- `alarm_storms_active`
- `repeated_alarm_assets`
- `deteriorating_assets` (only if the owner's condition model supplies it)

## Contributors

`contributors` entries must carry source-record identifiers so drill-downs
reuse existing detail surfaces instead of new ones:

- alarm-based rows: `alarm_no`, `asset_id`, `asset_no`, plus severity /
  occurrence counts / first-and-last-seen timestamps as available
- asset-condition rows: `asset_id`, `asset_no`, evidence shares or factor text

Contributor ranking must be computed server-side.

## Filters

If the family accepts scoping, mirror the reliability contract:
`site_id` and `as_of` query parameters. Families that cannot honour a filter
must ignore it server-side rather than half-apply it; the dashboard labels
portfolio-wide panels explicitly.

## Non-goals for this integration

- No client-side recomputation of any index.
- No mock responses: until the owner ships the endpoint, the dashboard simply
  does not render the family (families are added only with real payloads).
