"""Trend and explanation adapters over the canonical executive KPI service.

Every value produced here comes from calling the canonical ``kpi_service``
compute functions across consecutive deterministic windows. No metric formula
exists in this module: an adapter only extracts already-computed values,
normalizes units/labels and ranks measured contributors. Contributors are
reported as observed evidence (correlation/contributor), never asserted cause.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException

from .executive_kpi_store import _kpi_filters


MAX_TREND_SAMPLES = 24
DEFAULT_TREND_SAMPLES = 12
CONDITION_DRIVER_LIMIT = 10

# family -> compute function + metric registry.
# Metric registry: key -> {'label', 'unit', 'direction', 'path'} where ``path``
# is the dotted key path of the value inside the family payload.
_TREND_FAMILIES: dict[str, dict] = {
    'reliability': {
        'compute': 'compute_reliability',
        'metrics': {
            'saidi': {'label': 'SAIDI', 'unit': 'minutes/customer',
                      'direction': 'lower_is_better', 'path': 'saidi_minutes'},
            'saifi': {'label': 'SAIFI', 'unit': 'interruptions/customer',
                      'direction': 'lower_is_better', 'path': 'saifi'},
            'caidi': {'label': 'CAIDI', 'unit': 'minutes/interruption',
                      'direction': 'lower_is_better', 'path': 'caidi_minutes'},
            'availability_pct': {'label': 'Availability', 'unit': '%',
                                 'direction': 'higher_is_better',
                                 'path': 'availability_pct'},
            'total_downtime_hours': {'label': 'Total Downtime', 'unit': 'hours',
                                     'direction': 'lower_is_better',
                                     'path': 'total_downtime_hours'},
            'outage_count': {'label': 'Outage Count', 'unit': 'outages',
                             'direction': 'lower_is_better',
                             'path': 'outage_count'},
            'avg_outage_duration_hours': {
                'label': 'Average Outage Duration', 'unit': 'hours',
                'direction': 'lower_is_better',
                'path': 'avg_outage_duration_hours'},
            'planned_outages': {'label': 'Planned Outages', 'unit': 'outages',
                                'direction': 'lower_is_better',
                                'path': 'planned_outages'},
            'unplanned_outages': {'label': 'Unplanned Outages', 'unit': 'outages',
                                  'direction': 'lower_is_better',
                                  'path': 'unplanned_outages'},
        },
    },
    'maintenance': {
        'compute': 'compute_maintenance_kpis',
        'metrics': {
            'open_work_orders': {'label': 'Open Work Orders', 'unit': 'work orders',
                                 'direction': 'lower_is_better', 'path': 'open_wo'},
            'overdue_work_orders': {'label': 'Overdue Work Orders',
                                    'unit': 'work orders',
                                    'direction': 'lower_is_better',
                                    'path': 'overdue_wo'},
            'emergency_work_orders': {'label': 'Emergency Work Orders',
                                      'unit': 'work orders',
                                      'direction': 'lower_is_better',
                                      'path': 'emergency_wo'},
            'high_risk_overdue_work_orders': {
                'label': 'High-Risk Overdue Work Orders', 'unit': 'work orders',
                'direction': 'lower_is_better', 'path': 'high_risk_overdue_wo'},
            'unassigned_critical_work_orders': {
                'label': 'Unassigned Critical Work Orders', 'unit': 'work orders',
                'direction': 'lower_is_better', 'path': 'unassigned_critical_wo'},
            'backlog_hours': {'label': 'Backlog Hours', 'unit': 'hours',
                              'direction': 'lower_is_better',
                              'path': 'backlog_hours'},
            'backlog_weeks': {'label': 'Backlog Weeks', 'unit': 'weeks',
                              'direction': 'lower_is_better',
                              'path': 'backlog_weeks'},
            'pm_compliance_pct': {'label': 'PM Compliance', 'unit': '%',
                                  'direction': 'higher_is_better',
                                  'path': 'pm_compliance_pct'},
            'schedule_compliance_pct': {'label': 'Schedule Compliance', 'unit': '%',
                                        'direction': 'higher_is_better',
                                        'path': 'schedule_compliance_pct'},
            'mtbf_hours': {'label': 'MTBF', 'unit': 'hours',
                           'direction': 'higher_is_better', 'path': 'mtbf_hours'},
            'mttr_hours': {'label': 'MTTR', 'unit': 'hours',
                           'direction': 'lower_is_better', 'path': 'mttr_hours'},
            'repeat_failure_rate_pct': {'label': 'Repeat Failure Rate', 'unit': '%',
                                        'direction': 'lower_is_better',
                                        'path': 'repeat_failure_rate_pct'},
        },
    },
    'inventory': {
        'compute': 'compute_inventory_procurement_kpis',
        'metrics': {
            'stockout_lines': {'label': 'Stockout Lines', 'unit': 'lines',
                               'direction': 'lower_is_better',
                               'path': 'stockout_items'},
        },
    },
    'workforce': {
        'compute': 'compute_workforce_kpis',
        'metrics': {
            'technicians_available': {'label': 'Available Technicians',
                                      'unit': 'technicians',
                                      'direction': 'higher_is_better',
                                      'path': 'technicians_available'},
            'unassigned_critical_work': {'label': 'Unassigned Critical Work',
                                          'unit': 'work orders',
                                          'direction': 'lower_is_better',
                                          'as_count': 'unassigned_critical_work'},
        },
    },
    'condition': {
        'compute': 'compute_condition_kpis',
        'metrics': {
            'active_alarms': {'label': 'Active Alarms', 'unit': 'alarms',
                              'direction': 'lower_is_better',
                              'path': 'active_alarms'},
            'critical_active_alarms': {'label': 'Critical Active Alarms',
                                       'unit': 'alarms',
                                       'direction': 'lower_is_better',
                                       'path': 'critical_active_alarms'},
            'unacknowledged_alarms': {'label': 'Unacknowledged Alarms',
                                      'unit': 'alarms',
                                      'direction': 'lower_is_better',
                                      'path': 'unacknowledged_alarms'},
            'alarm_storms': {'label': 'Alarm Storms', 'unit': 'storms',
                             'direction': 'lower_is_better',
                             'as_count': 'alarm_storms'},
            'repeated_alarm_assets': {'label': 'Repeated-Alarm Assets',
                                      'unit': 'assets',
                                      'direction': 'lower_is_better',
                                      'as_count': 'repeated_alarm_assets'},
        },
    },
    'hse': {
        'compute': 'compute_hse_kpis',
        'metrics': {
            'open_incidents': {'label': 'Open Incidents', 'unit': 'incidents',
                               'direction': 'lower_is_better',
                               'path': 'open_incidents'},
            'high_risk_incidents_open': {'label': 'High-Risk Incidents Open',
                                         'unit': 'incidents',
                                         'direction': 'lower_is_better',
                                         'path': 'high_risk_open'},
            # Genuinely as-of: compute_hse_kpis anchors the gap at the
            # requested period end and bounds incidents to that day.
            'days_since_last_high_risk': {
                'label': 'Days Since Last High-Risk Incident', 'unit': 'days',
                'direction': 'higher_is_better',
                'path': 'days_since_last_high_risk'},
        },
    },
    # Cost is genuinely window-based: compute_cost_kpis sums immutable
    # maintenance_cost_ledger entries inside each bucket's posted_at range,
    # so historical samples are real as-of evaluations, not snapshot echoes.
    'cost': {
        'compute': 'compute_cost_kpis',
        'metrics': {
            'maintenance_cost_window': {
                'label': 'Maintenance Cost', 'unit': 'currency',
                'direction': 'lower_is_better',
                'path': 'maintenance_cost_window'},
        },
    },
}


def _resolve(family: str, metric: str):
    from . import kpi_service

    entry = _TREND_FAMILIES.get(family)
    if not entry or metric not in entry['metrics']:
        return None
    compute = getattr(kpi_service, entry['compute'], None)
    if compute is None:
        return None
    return compute, entry['metrics'][metric]


def _extract(payload: dict, meta: dict):
    if 'as_count' in meta:
        value = payload.get(meta['as_count'])
        return len(value) if isinstance(value, (list, tuple)) else None
    path = meta['path']
    value = payload
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _bucket_filters(base_filters, bucket_index: int, period_days: int):
    """Deterministic non-overlapping windows anchored at the period end."""
    end_day = date.fromisoformat(
        str(base_filters.period_end or date.today().isoformat())[:10])
    bucket_end = end_day - timedelta(days=period_days * bucket_index)
    return _kpi_filters(
        period_end=bucket_end.isoformat(),
        period_days=period_days,
        site_id=base_filters.site_id,
        region=base_filters.region,
        asset_type_id=base_filters.asset_type_id,
        criticality=base_filters.criticality,
    )


def compute_metric_trend(conn, f, *, family: str, metric: str,
                         samples: int = DEFAULT_TREND_SAMPLES) -> dict:
    resolved = _resolve(family, metric)
    if resolved is None:
        raise HTTPException(
            404, f'unsupported KPI family/metric: {family}/{metric}')
    compute, meta = resolved
    period_days = max(1, int(f.period_days))
    samples = max(2, min(int(samples), MAX_TREND_SAMPLES))

    series = []
    for bucket in range(samples):
        bucket_filters = _bucket_filters(f, bucket, period_days)
        payload = compute(conn, bucket_filters)
        window = payload.get('window') or bucket_filters.window()
        series.append({
            'period_start': window.get('period_start'),
            'period_end': window.get('period_end'),
            'value': _extract(payload, meta),
        })
    series.reverse()  # chronological, oldest first

    values = [s['value'] for s in series if s['value'] is not None]
    return {
        'family': family,
        'metric': metric,
        'label': meta['label'],
        'unit': meta['unit'],
        'direction': meta['direction'],
        'period_days': period_days,
        'samples': series,
        'min': min(values) if values else None,
        'max': max(values) if values else None,
        'missing_note': (
            None if values else
            'No computable value in any window; see the family payload for '
            'missing inputs rather than treating this as zero.'
        ),
    }


def _maintenance_overdue_drivers(conn, f, limit: int = 5) -> list[dict]:
    """Single set-based query: top overdue open work orders by delay."""
    from .kpi_service import _asset_scope

    scope_sql, scope_args = _asset_scope(f)
    rows_ = [
        dict(row) for row in conn.execute(
            f'''SELECT w.id, w.wo_no, w.title, w.priority,
                       CAST(julianday(date('now','localtime'))
                            - julianday(w.target_finish) AS INT) AS days_overdue
                FROM work_orders w
                LEFT JOIN assets a ON a.id = w.asset_id
                LEFT JOIN locations l ON l.id = a.location_id
                LEFT JOIN sites s ON s.id = l.site_id
                WHERE w.target_finish IS NOT NULL
                  AND w.target_finish < date('now','localtime')
                  AND w.status NOT IN ('Completed','Closed','Cancelled')
                  {scope_sql}
                ORDER BY CASE w.priority WHEN 'Emergency' THEN 5
                         WHEN 'Critical' THEN 4 WHEN 'High' THEN 3
                         WHEN 'Medium' THEN 2 ELSE 1 END DESC,
                         days_overdue DESC
                LIMIT ?''',
            list(scope_args) + [limit],
        ).fetchall()
    ]
    return [
        {
            'kind': 'overdue_backlog',
            'label': (
                f"{r['wo_no']} overdue {max(int(r['days_overdue'] or 0), 0)} d"
            ),
            'magnitude': max(int(r['days_overdue'] or 0), 0),
            'unit': 'days',
            'attribution': 'contributor',
            'source_type': 'work_order',
            'source_id': int(r['id']),
            'drill': {'module': 'work', 'record': r['wo_no'], 'id': int(r['id'])},
        }
        for r in rows_
    ]


def _reliability_outage_drivers(conn, f, metric: str, limit: int = 5) -> list[dict]:
    """Reuse the canonical outage-driver extraction from the snapshot's own
    explanation section instead of recomputing attribution here.

    The driver record class must correspond to the metric being explained:
    ``planned_outages`` is explained by planned (non-forced) outage records,
    every other reliability metric is explained by forced/unplanned records.
    """
    from .kpi_service import explain_kpi_changes

    explanations = explain_kpi_changes(conn, f)
    section = ('planned_outages' if metric == 'planned_outages'
               else 'availability')
    drivers = []
    for driver in ((explanations.get(section) or {}).get('drivers') or [])[:limit]:
        link = driver.get('link') or {}
        drivers.append({
            'kind': driver.get('kind', 'unplanned_outage'),
            'label': driver.get('label'),
            'magnitude': driver.get('hours'),
            'unit': 'hours',
            'attribution': 'correlation',
            'source_type': 'asset_outage',
            'source_id': link.get('id'),
            'drill': link,
        })
    return drivers


def _repeat_failure_drivers(conn, f, limit: int = 10) -> list[dict]:
    """Chronic bad-actor contributors for the repeat-failure rate.

    Consumes the canonical ``repeat_failures`` explanation section; each
    contributor is an asset whose corrective completions literally compose
    the metric numerator, hence ``contributor`` attribution.
    """
    from .kpi_service import explain_kpi_changes

    section = (explain_kpi_changes(conn, f).get('repeat_failures') or {})
    drivers = []
    for driver in (section.get('drivers') or [])[:limit]:
        link = driver.get('link') or {}
        drivers.append({
            'kind': 'repeat_failure',
            'label': driver.get('label'),
            'magnitude': driver.get('failures_90d'),
            'unit': 'failures',
            'attribution': 'contributor',
            'source_type': 'asset',
            'source_id': link.get('id'),
            'drill': link,
        })
    return drivers


def _pm_compliance_drivers(conn, f, limit: int = 10) -> list[dict]:
    """Overdue-plan contributors for the PM compliance rate.

    Consumes the canonical ``pm_compliance`` explanation section; each
    contributor is a calendar plan the metric itself counts as overdue, so
    ``contributor`` attribution applies.
    """
    from .kpi_service import explain_kpi_changes

    section = (explain_kpi_changes(conn, f).get('pm_compliance') or {})
    drivers = []
    for driver in (section.get('drivers') or [])[:limit]:
        link = driver.get('link') or {}
        drivers.append({
            'kind': 'overdue_pm',
            'label': driver.get('label'),
            'magnitude': driver.get('days_overdue'),
            'unit': 'days overdue',
            'attribution': 'contributor',
            'source_type': 'pm_plan',
            'source_id': link.get('id'),
            'drill': link,
        })
    return drivers


def _schedule_compliance_drivers(conn, f, limit: int = 10) -> list[dict]:
    """Late-completion contributors for schedule compliance.

    Consumes the canonical ``schedule_compliance`` explanation section; each
    cited work order was measured by the rate itself.
    """
    from .kpi_service import explain_kpi_changes

    section = (explain_kpi_changes(conn, f)
               .get('schedule_compliance') or {})
    drivers = []
    for driver in (section.get('drivers') or [])[:limit]:
        link = driver.get('link') or {}
        drivers.append({
            'kind': 'late_completion',
            'label': driver.get('label'),
            'magnitude': driver.get('delay_days'),
            'unit': 'days late',
            'attribution': 'contributor',
            'source_type': 'work_order',
            'source_id': link.get('id'),
            'drill': link,
        })
    return drivers


def _maintenance_cost_drivers(conn, f, limit: int = 10) -> list[dict]:
    """Top-cost asset contributors for the window's maintenance cost.

    Consumes the canonical ``top_cost_assets`` section computed over the
    current window; each cited asset's ledger entries literally compose the
    metric sum, hence ``contributor`` attribution. The extraction reuses the
    canonical compute so contributor scope equals metric scope by
    construction (unattributed ledger entries are excluded from scoped
    views by compute_cost_kpis itself).
    """
    from .kpi_service import compute_cost_kpis

    drivers = []
    for row in (compute_cost_kpis(conn, f).get('top_cost_assets') or [])[:limit]:
        amount = round(float(row['amount']), 2)
        drivers.append({
            'kind': 'maintenance_cost',
            'label': (
                f"{row['asset_no']} {row.get('asset_name') or ''} ".strip()
                + f'{amount:,.2f}'
            ),
            'magnitude': amount,
            'unit': 'currency',
            'attribution': 'contributor',
            'source_type': 'asset',
            'source_id': int(row['asset_id']),
            'drill': {'module': 'assets', 'record': row['asset_no'],
                      'id': int(row['asset_id'])},
        })
    return drivers


def _hse_days_since_drivers(conn, f, *, current: dict,
                            limit: int = 5) -> list[dict]:
    """Recent high-risk incidents standing behind the days-since gap.

    The first driver is the literal determinant of the metric — the most
    recent high-risk incident at or before the anchor date (``contributor``
    attribution). Older high-risk records follow as ``correlation`` context.
    """
    from .kpi_service import _asset_scope, _rows, HSE_HIGH_RISK_SCORE

    anchor_str = str(current.get('days_since_anchor') or '')
    try:
        anchor = date.fromisoformat(anchor_str[:10])
    except (TypeError, ValueError):
        return []
    scope_sql, scope_args = _asset_scope(f, alias_a='a')
    rows_ = _rows(conn.execute(
        'SELECT h.id, h.incident_no, h.incident_type, h.title,'
        ' COALESCE(h.occurred_at,h.created_at) occurred_at'
        ' FROM safety_incidents h'
        ' LEFT JOIN sites s ON s.id=h.site_id'
        ' LEFT JOIN locations l ON l.id=h.location_id'
        ' LEFT JOIN assets a ON a.id=h.asset_id'
        ' WHERE 1=1' + scope_sql +
        ' AND h.risk_score>=? AND COALESCE(h.occurred_at,h.created_at)<=?'
        ' ORDER BY COALESCE(h.occurred_at,h.created_at) DESC, h.id DESC'
        ' LIMIT ?',
        list(scope_args) + [HSE_HIGH_RISK_SCORE,
                            anchor_str + 'T23:59:59', limit]))
    drivers: list[dict] = []
    for index, row in enumerate(rows_):
        try:
            days_before_anchor = max(
                0, (anchor -
                    datetime.fromisoformat(str(row['occurred_at'])[:19]).date()).days)
        except (TypeError, ValueError):
            continue
        drivers.append({
            'kind': 'high_risk_incident',
            'label': (
                f"{row['incident_no']} {row['incident_type']} on "
                f"{str(row['occurred_at'])[:10]}"
            ),
            'magnitude': days_before_anchor,
            'unit': 'days before anchor',
            'attribution': ('contributor' if index == 0
                            else 'correlation'),
            'source_type': 'safety_incident',
            'source_id': int(row['id']),
            'drill': {'module': 'hse', 'record': row['incident_no'],
                      'id': int(row['id'])},
        })
    return drivers


def _inventory_stockout_drivers(conn, f, *, current: dict,
                                limit: int = 10) -> list[dict]:
    """Stocked-out lines composing ``inventory/stockout_lines``.

    Consumes the canonical ``stockouts`` section of the same compute payload
    the metric counts; every cited line is one of the counted records, hence
    ``contributor`` attribution. Magnitude is how far below its own reorder
    point the line sits, derived deterministically from the record.
    """
    drivers = []
    for row in (current.get('stockouts') or [])[:limit]:
        try:
            available = float(row['current_stock']) - float(row['reserved_stock'])
            reorder = float(row['reorder_point'] or 0)
        except (TypeError, ValueError):
            available, reorder = 0.0, 0.0
        shortfall = round(max(0.0, reorder - available), 3)
        drivers.append({
            'kind': 'stockout_line',
            'label': (
                f"{row['item_no']} {row.get('name') or ''} ".strip()
                + f"in {row.get('warehouse_name') or 'warehouse'}"
            ),
            'magnitude': shortfall,
            'unit': ('units short of reorder point' if shortfall > 0
                     else 'at/below reorder point'),
            'attribution': 'contributor',
            'source_type': 'inventory_item',
            'source_id': int(row['id']),
            'drill': {'module': 'inventory', 'record': row['item_no'],
                      'id': int(row['id'])},
        })
    return drivers


def _workforce_unassigned_drivers(conn, f, *, current: dict,
                                  limit: int = 10) -> list[dict]:
    """Unassigned critical work orders composing the workforce metric.

    Consumes the canonical ``unassigned_critical_work`` section of the same
    compute payload the metric count comes from; every cited work order is
    one of the counted records, hence ``contributor`` attribution. Magnitude
    is how far past its own target the record already sits (0 when not yet
    due) — derived deterministically from the record itself.
    """
    today = date.fromisoformat(
        str(f.period_end or date.today().isoformat())[:10])
    drivers = []
    for row in (current.get('unassigned_critical_work') or [])[:limit]:
        days_past = 0
        target = row.get('target_finish')
        if target:
            try:
                days_past = max(0, (today -
                                    date.fromisoformat(str(target)[:10])).days)
            except (TypeError, ValueError):
                days_past = 0
        drivers.append({
            'kind': 'unassigned_critical_work',
            'label': (
                f"{row['wo_no']} {row.get('priority')} unassigned on "
                f"{row.get('asset_no') or 'unlocated asset'}"
            ),
            'magnitude': days_past,
            'unit': 'days past target' if days_past else 'not yet due',
            'attribution': 'contributor',
            'source_type': 'work_order',
            'source_id': int(row['id']),
            'drill': {'module': 'work', 'record': row['wo_no'],
                      'id': int(row['id'])},
        })
    return drivers


def _hse_incident_drivers(conn, f, *, metric: str, limit: int = 10) -> list[dict]:
    """Open-incident contributors for the HSE family.

    Consumes the canonical ``hse_*`` explanation sections; every cited
    incident is one of the open records composing the metric count.
    """
    from .kpi_service import explain_kpi_changes

    section_key = ('hse_high_risk_open'
                   if metric == 'high_risk_incidents_open'
                   else 'hse_open_incidents')
    section = (explain_kpi_changes(conn, f).get(section_key) or {})
    drivers = []
    for driver in (section.get('drivers') or [])[:limit]:
        link = driver.get('link') or {}
        drivers.append({
            'kind': driver.get('kind'),
            'label': driver.get('label'),
            'magnitude': driver.get('risk_score'),
            'unit': 'risk score',
            'attribution': 'contributor',
            'source_type': 'safety_incident',
            'source_id': link.get('id'),
            'drill': link,
        })
    return drivers


def explain_metric(conn, f, *, family: str, metric: str) -> dict:
    """Current vs previous window for one metric, with measured drivers.

    Contributors are diffed by identity between windows and labelled as
    contributors/correlations; no causal claim is made.
    """
    resolved = _resolve(family, metric)
    if resolved is None:
        raise HTTPException(
            404, f'unsupported KPI family/metric: {family}/{metric}')
    compute, meta = resolved
    period_days = max(1, int(f.period_days))

    prev_end_day = date.fromisoformat(
        str(f.period_end or date.today().isoformat())[:10]) - timedelta(days=period_days)
    previous_f = _kpi_filters(
        period_end=prev_end_day.isoformat(),
        period_days=period_days,
        site_id=f.site_id,
        region=f.region,
        asset_type_id=f.asset_type_id,
        criticality=f.criticality,
    )
    current = compute(conn, f)
    previous = compute(conn, previous_f)

    value = _extract(current, meta)
    previous_value = _extract(previous, meta)
    delta = pct_change = improved = None
    if value is not None and previous_value is not None:
        delta = round(float(value) - float(previous_value), 4)
        if float(previous_value) != 0:
            pct_change = round(100.0 * delta / abs(float(previous_value)), 2)
        if delta != 0:
            moved_up = delta > 0
            improved = (moved_up if meta['direction'] == 'higher_is_better'
                        else not moved_up)

    if family == 'reliability':
        drivers = _reliability_outage_drivers(conn, f, metric)
    elif family == 'maintenance' and metric == 'repeat_failure_rate_pct':
        drivers = _repeat_failure_drivers(conn, f)
    elif family == 'maintenance' and metric == 'pm_compliance_pct':
        drivers = _pm_compliance_drivers(conn, f)
    elif family == 'maintenance' and metric == 'schedule_compliance_pct':
        drivers = _schedule_compliance_drivers(conn, f)
    elif family == 'maintenance' and metric in {'mtbf_hours', 'mttr_hours'}:
        # MTBF/MTTR are derived from the scoped forced-outage set computed by
        # compute_reliability; those outage records are the composing
        # evidence, so the reliability driver path serves both metrics.
        drivers = _reliability_outage_drivers(conn, f, metric)
    elif family == 'hse' and metric in {'open_incidents',
                                        'high_risk_incidents_open'}:
        drivers = _hse_incident_drivers(conn, f, metric=metric)
    elif family == 'hse' and metric == 'days_since_last_high_risk':
        drivers = _hse_days_since_drivers(conn, f, current=current)
    elif family == 'cost':
        drivers = _maintenance_cost_drivers(conn, f)
    elif family == 'workforce' and metric == 'unassigned_critical_work':
        drivers = _workforce_unassigned_drivers(conn, f, current=current)
    elif family == 'inventory' and metric == 'stockout_lines':
        drivers = _inventory_stockout_drivers(conn, f, current=current)
    elif family == 'maintenance' and metric in {
            'open_work_orders', 'overdue_work_orders', 'emergency_work_orders',
            'high_risk_overdue_work_orders', 'unassigned_critical_work_orders',
            'backlog_hours', 'backlog_weeks'}:
        drivers = _maintenance_overdue_drivers(conn, f)
    elif family == 'condition':
        # Contributors come straight from the canonical condition computation:
        # most severe active alarms plus repeated-alarm assets as measured
        # recurrence evidence. No causal claim is attached.
        drivers = []
        for row in current.get('contributors', [])[:CONDITION_DRIVER_LIMIT]:
            severity_label = (row.get('severity') or 'alarm').lower()
            drivers.append({
                'kind': 'active_alarm',
                'label': (
                    f"{row['alarm_no']} {severity_label} on "
                    f"{row.get('asset_no') or 'unassigned asset'}"
                ),
                'magnitude': row.get('hours_open'),
                'unit': 'hours open',
                'attribution': 'contributor',
                'source_type': 'operational_alarm',
                'source_id': row['alarm_id'],
                'drill': {'module': 'telemetry', 'record': row['alarm_no'],
                          'id': row['alarm_id']},
            })
        storms = current.get('alarm_storms') or []
        if storms:
            drivers.append({
                'kind': 'risk_indicator',
                'label': f'{len(storms)} recurring alarm channel(s) at or above '
                         '3 occurrences',
                'magnitude': len(storms),
                'unit': 'channels',
                'attribution': 'correlation',
                'source_type': 'telemetry_channel',
                'source_id': storms[0].get('channel_id'),
                'drill': {'module': 'telemetry',
                          'record': storms[0].get('channel_code'),
                          'id': storms[0].get('channel_id')},
            })
    else:
        drivers = []

    parts: list[str] = []
    if delta is None:
        parts.append('Not enough recorded evidence in one or both windows '
                     'to compare.')
    else:
        verb = ('improved' if improved else 'worsened') if improved is not None \
            else 'changed'
        parts.append(
            f"{meta['label']} moved from {previous_value} to {value} "
            f"({delta:+g}) and {verb} against its direction."
        )
    if drivers:
        parts.append(
            f'{len(drivers)} contributing record(s) observed in the current '
            'window.'
        )

    return {
        'family': family,
        'metric': metric,
        'label': meta['label'],
        'unit': meta['unit'],
        'direction': meta['direction'],
        'value': value,
        'previous_value': previous_value,
        'delta': delta,
        'pct_change': pct_change,
        'improved': improved,
        'windows': {
            'current': current.get('window'),
            'previous': previous.get('window'),
        },
        'drivers': drivers[:10],
        'summary': ' '.join(parts),
        'disclaimer': (
            'Drivers are evidence observed in each window; correlation is '
            'not asserted as cause.'
        ),
    }
