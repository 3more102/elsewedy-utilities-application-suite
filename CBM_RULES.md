# Condition-Based Maintenance Rules — EUAS v4.7

## Purpose

EUAS v4.7 adds a deterministic condition-based maintenance (CBM) rule editor on top of the existing telemetry pipeline. It converts verified telemetry conditions into traceable maintenance recommendations or governed work orders without claiming predictive analytics or machine learning.

## Evaluation contract

- Only telemetry readings with quality `Good` are evaluated. `Uncertain` and `Bad` readings remain stored for quality reporting but do not change CBM consecutive-hit state.
- Rules are bound to one telemetry channel and therefore one asset.
- Supported operators are `>=`, `>`, `<=`, `<`, `between`, and `outside`.
- `consecutive_readings` requires N consecutive matching Good readings before a trigger can open.
- `cooldown_minutes` prevents a manually/automatically resolved condition from immediately opening a duplicate event.
- An already-open event is updated instead of duplicated.
- A later Good reading that no longer matches the condition automatically resolves the open event and clears the active rule state.

## Actions

`Recommendation` opens a CBM event and notifies maintenance operations. `WorkOrder` additionally creates a **Condition-Based Maintenance** work order in `Submitted` state, links it to the CBM event and sends it through the existing Maintenance Manager approval workflow. The rule engine does not auto-approve generated work.

## Governance and access

Rule creation/update requires the `cbm.rules.manage` permission. Baseline grants are assigned to administrator, asset manager, maintenance manager and planner roles. CBM event acknowledgement uses the existing alarm-operation permission. Every rule create/update and trigger/resolve action emits audit/event evidence.

## Authoring validation

`POST /api/cbm/rules/{rule_id}/test?value=<number>` evaluates a value with **no side effects**. It does not update consecutive hits, open an event or create work.

## Evidence and observability

- `/api/cbm/rules` — rule catalog and live state
- `/api/cbm/events` — trigger lifecycle and work linkage
- `/api/exports/cbm-rules.csv` and `/api/exports/cbm-events.csv`
- telemetry ingest batches persist CBM opened/resolved/work-order counts
- metrics: `euas_active_cbm_rules`, `euas_open_cbm_events`, `euas_cbm_work_orders_total`

## Boundary

This feature is rules-based condition monitoring. It is not a predictive-maintenance model, failure-probability estimator, anomaly-learning system or vendor historian replacement. Predictive model governance remains a separate roadmap item.
