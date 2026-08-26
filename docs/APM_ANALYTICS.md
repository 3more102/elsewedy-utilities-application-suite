# EUAS APM Analytics — Canonical Formulas

Every reliability/asset-performance figure below is computed by the deterministic
kernel in `app/reliability.py` and exposed through `app/apm_store.py`. No
machine-learning predictions are involved: each verdict states its evidence and,
where data is insufficient, returns `insufficient_data` instead of guessing.

---

## Asset health score

Anchor: `#asset-health-score` (referenced by `GET /api/reliability/health/{asset_id}`).

The score starts at 100 and subtracts capped evidence penalties:

| Factor | Penalty |
|---|---|
| condition | Good 0 / Fair 10 / Warning 25 / Poor 40 / Critical 55 (unknown 15) |
| criticality | Low 0 / Medium 3 / High 7 / Critical 12 (unknown 3) |
| status | Operating or Standby 0 / Under Maintenance or Restricted 10 / other 25 / empty 0 |
| priority_work | min(25, high-priority open work orders x 7) |
| overdue_work | min(20, overdue open work orders x 5) |
| failed_inspections | min(16, failed inspections x 8) |
| sla_breaches | min(10, breached SLA rows x 5) |
| operational_alarms | min(18, active alarms x 5 + active critical alarms x 5) |
| repeat_failures | min(15, corrective/emergency completions in window x 5) |
| deterioration | none 0 / adverse 6 / severe 12 |
| downtime_90d | min(10, forced outage hours in window // 4) |

```
score = max(0, min(100, 100 - sum(penalties)))
```

Bands: `>=85` Healthy, `>=70` Monitor, `>=50` Warning, else Critical.
Every nonzero factor is returned as a named contributor with its points.

## Risk score (separate from health)

Likelihood (1-5): base 1; +2 health Critical, +1 Warning; +1 any active
critical alarm; +1 if >= 2 repeat failures in window; +1 severe deterioration
trend. Consequence (1-5): criticality floor (Low 2 / Medium 3 / High 4 /
Critical 5), +1 when forced outage hours in window exceed 24.

```
risk_score = likelihood * consequence   # integer 1..25
```

Levels: `<=4` Low, `<=9` Medium, `<=16` High, else Extreme. Both factors list
their contributors verbatim.

## Deterioration watchlist

For every active telemetry channel the kernel classifies, over the selected
window:

* trend: least-squares slope judged relative to series scale (`rising`,
  `falling`, `stable`, or `insufficient_data` below `min_points` samples)
* excursions against channel warning/critical bounds (high bounds trigger at
  or above, low bounds at or below)
* acceleration (first-half vs second-half slope) and variance shift

Verdict levels: `none`, `adverse` (trend towards a bound, repeated abnormal
readings or current abnormal state), `severe` (persisted abnormal run,
critical excursion, or accelerating adverse trend). Channels with too little
history are hidden by default and surfaced with `level=insufficient_data`
when `include_insufficient=true`; flagged entries always rank after real
findings.

## Alarm correlation

Deterministic clusters over a rolling window; raw alarms are never merged.

* recurrence — >= 2 events on one asset/channel pair
* bursts — sliding-window clusters on one asset (`burst_threshold` within
  `burst_window_minutes`)
* site_bursts — sliding-window clusters at one site spanning >= 2 distinct
  assets (`site_burst_threshold`); rationale states a *probable common
  upstream condition*, causality is not asserted
* groups — >= 3 correlated alarms sharing one asset

Each cluster carries a content-derived stable `correlation_id`
(`COR-<sha256[:16]>` over kind, asset/site/channel and window bounds),
the `primary_alarm_id` (earliest event) and `related_alarm_ids`.

## Bad-actor ranking

Per-asset weighted points (capped):

| Driver | Points |
|---|---|
| failures | min(30, corrective completions x 6) |
| emergency_work | min(15, emergency/critical work orders x 5) |
| downtime | min(25, forced outage hours // 4) |
| alarms | min(15, alarms in window x 3) |
| cost | min(15, maintenance cost // 2000) |

MTBF/MTTR are reported as context, never scored directly. Ranking is
deterministic (points desc, then asset number). Every entry exposes its
factor breakdown and drivers.

## Post-maintenance effectiveness

Like-for-like windows before/after a completed work order compare alarms,
critical alarms, failures, downtime hours, threshold excursions and average
health snapshots. A dimension improves only on strict improvement; overall
verdicts are `improved`, `regressed`, `mixed`, `unchanged`, or
`insufficient_data`. Channels alarming both before and after are listed as
recurring issues.

## FMEA observed evidence

Completed corrective/emergency work orders map to an FMEA failure mode by
exact `failure_code` match only; unmapped codes are never inferred. The
observed occurrence scale is compared with the FMEA `occurrence` rating and
gaps between matching completions yield the mean interval.

## CBM recommendations

Watchlist findings with abnormal evidence become persisted recommendations
(`persistent_abnormal`, `critical_excursion`, `trend_deterioration`) with
deterministic confidence. An existing Open/Reviewed/Approved recommendation
for the same asset/channel/condition-type suppresses duplicates. Lifecycle:
Open -> Reviewed/Approved/Dismissed; only Approved records convert to work
orders (idempotent replay).
