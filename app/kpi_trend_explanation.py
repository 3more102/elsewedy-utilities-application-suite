"""Trend and explanation adapters over the canonical executive KPI service.

Every value produced here comes from calling the canonical ``kpi_service``
compute functions across consecutive deterministic windows. No metric formula
exists in this module: an adapter only extracts already-computed values,
normalizes units/labels and ranks measured contributors. Contributors are
reported as observed evidence (correlation/contributor), never asserted cause.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException

from .executive_kpi_store import _kpi_filters


MAX_TREND_SAMPLES = 24
DEFAULT_TREND_SAMPLES = 12

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
            'backlog_hours': {'label': 'Backlog Hours', 'unit': 'hours',
                              'direction': 'lower_is_better',
                              'path': 'backlog_hours'},
            'pm_compliance_pct': {'label': 'PM Compliance', 'unit': '%',
                                  'direction': 'higher_is_better',
                                  'path': 'pm_compliance_pct'},
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
                                         'path': 'unassigned_critical_work'},
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


def _extract(payload: dict, path: str):
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
            'value': _extract(payload, meta['path']),
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


def _reliability_outage_drivers(conn, f, limit: int = 5) -> list[dict]:
    """Reuse the canonical outage-driver extraction from the snapshot's own
    explanation section instead of recomputing attribution here."""
    from .kpi_service import explain_kpi_changes

    explanations = explain_kpi_changes(conn, f)
    availability = explanations.get('availability') or {}
    drivers = []
    for driver in (availability.get('drivers') or [])[:limit]:
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

    value = _extract(current, meta['path'])
    previous_value = _extract(previous, meta['path'])
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
        drivers = _reliability_outage_drivers(conn, f)
    elif family == 'maintenance':
        drivers = _maintenance_overdue_drivers(conn, f)
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
