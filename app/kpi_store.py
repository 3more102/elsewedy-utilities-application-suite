"""EUAS KPI framework: explainable utility reliability indicators.

Every KPI carries its business definition, formula, unit, directionality,
thresholds, previous-period comparison and causal contributors so a manager
can move from a bad number to the specific outages that caused it. Values are
computed only from real recorded outages and declared customer counts; when an
input is absent the KPI reports ``None`` plus the missing input instead of a
fabricated number.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Query

from . import application as _application
from .auth import current_user, require_roles
from .database import db


# Industry-typical defaults, intentionally conservative and documented as
# configuration points rather than derived facts. Units are per measurement
# window (``period_days``), not normalized to a year, unless noted.
RELIABILITY_KPI_DEFINITIONS: dict[str, dict] = {
    'saifi': {
        'name': 'System Average Interruption Frequency Index',
        'definition': (
            'Average number of sustained interruptions experienced by a '
            'customer over the measurement window.'
        ),
        'formula': 'sum(customers affected per sustained interruption) / total customers served',
        'unit': 'interruptions/customer',
        'direction': 'lower_is_better',
        'target': 1.0,
        'warning_threshold': 2.0,
        'critical_threshold': 5.0,
        'sources': ['asset_outages', 'sites.customer_count'],
    },
    'saidi': {
        'name': 'System Average Interruption Duration Index',
        'definition': (
            'Average cumulative interruption duration experienced by a '
            'customer over the measurement window.'
        ),
        'formula': 'sum(interruption duration x customers affected) / total customers served',
        'unit': 'hours/customer',
        'direction': 'lower_is_better',
        'target': 1.0,
        'warning_threshold': 4.0,
        'critical_threshold': 8.0,
        'sources': ['asset_outages', 'sites.customer_count'],
    },
    'caidi': {
        'name': 'Customer Average Interruption Duration Index',
        'definition': (
            'Average restoration time for a customer who experiences at least '
            'one sustained interruption.'
        ),
        'formula': 'SAIDI / SAIFI',
        'unit': 'hours/interruption',
        'direction': 'lower_is_better',
        'target': 1.0,
        'warning_threshold': 2.0,
        'critical_threshold': 4.0,
        'sources': ['asset_outages', 'sites.customer_count'],
    },
    'asai': {
        'name': 'Average Service Availability Index',
        'definition': (
            'Share of customer-hours in the window during which service was '
            'available.'
        ),
        'formula': '(window hours - SAIDI) / window hours x 100',
        'unit': '%',
        'direction': 'higher_is_better',
        'target': 99.9,
        'warning_threshold': 99.5,
        'critical_threshold': 99.0,
        'sources': ['asset_outages', 'sites.customer_count'],
    },
}


def _parse_as_of(value: Optional[str]) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(400, 'as_of must be an ISO date (YYYY-MM-DD)')


def _sustained_outage_rows(conn, site_id: Optional[int], start: datetime, end: datetime) -> list[dict]:
    sql = '''SELECT o.id,o.outage_no,o.asset_id,o.site_id,o.outage_type,o.status,
                    o.start_at,o.end_at,a.asset_no,a.name asset_name,s.site_code,
                    s.customer_count
             FROM asset_outages o
             JOIN assets a ON a.id=o.asset_id
             LEFT JOIN sites s ON s.id=o.site_id
             WHERE o.outage_type='Forced' AND o.end_at IS NOT NULL
               AND o.start_at<=? AND o.end_at>=?'''
    args: list = [end.isoformat(timespec='seconds'), start.isoformat(timespec='seconds')]
    if site_id is not None:
        sql += ' AND o.site_id=?'
        args.append(site_id)
    sql += ' ORDER BY o.start_at'
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _ongoing_outage_rows(conn, site_id: Optional[int], as_of_dt: datetime) -> list[dict]:
    sql = '''SELECT o.outage_no,o.start_at,a.asset_no,a.name asset_name,s.site_code
             FROM asset_outages o
             JOIN assets a ON a.id=o.asset_id
             LEFT JOIN sites s ON s.id=o.site_id
             WHERE o.outage_type='Forced' AND o.status='Open' AND o.start_at<=?'''
    args: list = [as_of_dt.isoformat(timespec='seconds')]
    if site_id is not None:
        sql += ' AND o.site_id=?'
        args.append(site_id)
    sql += ' ORDER BY o.start_at DESC LIMIT 50'
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _customer_scope(conn, site_id: Optional[int]) -> tuple[int, int]:
    """Return (total customers served, sites declaring a positive count)."""
    sql = "SELECT COUNT(*),COALESCE(SUM(customer_count),0) FROM sites WHERE COALESCE(customer_count,0)>0"
    args: list = []
    if site_id is not None:
        sql += ' AND id=?'
        args.append(site_id)
    row = conn.execute(sql, args).fetchone()
    return int(row[1] or 0), int(row[0] or 0)


def _window_metrics(
    outages: list[dict],
    window_start: datetime,
    window_end: datetime,
    total_customers: int,
) -> tuple[dict, list[dict]]:
    customers_interrupted = 0.0
    customer_hours = 0.0
    attributed = 0
    unattributed = 0
    contributors: list[dict] = []
    for outage in outages:
        duration = _application._outage_overlap_hours(
            outage['start_at'], outage['end_at'], window_start, window_end
        )
        if duration <= 0:
            continue
        customers = outage.get('customer_count')
        entry = {
            'outage_no': outage['outage_no'],
            'asset_no': outage.get('asset_no'),
            'asset_name': outage.get('asset_name'),
            'duration_hours': round(duration, 3),
            'customer_hours': None if customers is None else round(duration * float(customers), 3),
            'share_pct': None,
        }
        if customers is not None and customers > 0 and total_customers > 0:
            attributed += 1
            # SAIFI weights every interruption by the customers it affected.
            customers_interrupted += float(customers)
            customer_hours += duration * float(customers)
            contributors.append(entry)
        else:
            unattributed += 1
            entry['excluded_reason'] = 'site has no customer_count'
            contributors.append(entry)
    metrics = {
        'sustained_interruptions': attributed,
        'unattributed_interruptions': unattributed,
        'customer_interruption_hours': round(customer_hours, 3),
    }
    if total_customers > 0:
        saifi = customers_interrupted / total_customers
        saidi = customer_hours / total_customers
        caidi = (saidi / saifi) if customers_interrupted else None
        window_hours = max(0.001, (window_end - window_start).total_seconds() / 3600.0)
        asai = 100.0 * (window_hours - saidi) / window_hours
        metrics.update({
            'saifi': round(saifi, 4),
            'saidi': round(saidi, 4),
            'caidi': round(caidi, 4) if caidi is not None else None,
            'asai': round(asai, 4),
        })
    else:
        metrics.update({'saifi': None, 'saidi': None, 'caidi': None, 'asai': None})
    return metrics, contributors


def _change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return round(100.0 * (current - previous) / abs(previous), 2)


def compute_reliability_kpis(
    conn,
    site_id: Optional[int] = None,
    period_days: int = 365,
    as_of: Optional[str] = None,
) -> dict:
    as_of_date = _parse_as_of(as_of)
    window_end = min(
        datetime.combine(as_of_date, datetime.max.time().replace(microsecond=0)),
        datetime.now(),
    )
    window_start = window_end - timedelta(days=period_days)
    prev_end = window_start
    prev_start = prev_end - timedelta(days=period_days)

    total_customers, declaring_sites = _customer_scope(conn, site_id)
    outages = _sustained_outage_rows(conn, site_id, window_start, window_end)
    prev_outages = _sustained_outage_rows(conn, site_id, prev_start, prev_end)
    ongoing = _ongoing_outage_rows(conn, site_id, window_end)

    metrics, contributors = _window_metrics(outages, window_start, window_end, total_customers)
    prev_metrics, _prev_contributors = _window_metrics(prev_outages, prev_start, prev_end, total_customers)

    # Rank contributors by customer impact where attribution exists.
    ranked = sorted(
        contributors,
        key=lambda x: (x['customer_hours'] is None, -(x['customer_hours'] or 0)),
    )
    if total_customers > 0:
        for entry in ranked:
            if entry['customer_hours'] is not None:
                entry['share_pct'] = round(
                    100.0 * entry['customer_hours'] / max(metrics['customer_interruption_hours'], 1e-9), 2
                )
    top_contributors = ranked[:5]

    kpis = {}
    for kpi_id, definition in RELIABILITY_KPI_DEFINITIONS.items():
        current = metrics[kpi_id]
        previous = prev_metrics[kpi_id]
        kpis[kpi_id] = {
            **definition,
            'id': kpi_id,
            'value': current,
            'previous_value': previous,
            'change_pct': _change(current, previous),
            'missing_inputs': [] if total_customers > 0 else ['sites.customer_count'],
        }

    planned_sql = '''SELECT COUNT(*) FROM asset_outages
       WHERE outage_type='Planned' AND start_at<=? AND (end_at IS NULL OR end_at>=?)'''
    planned_args: list = [
        window_end.isoformat(timespec='seconds'),
        window_start.isoformat(timespec='seconds'),
    ]
    if site_id is not None:
        planned_sql += ' AND site_id=?'
        planned_args.append(site_id)
    planned = conn.execute(planned_sql, planned_args).fetchone()[0]

    latest_event = max(
        (str(o['end_at']) for o in outages), default=None
    )
    return {
        'kpi_family': 'utility_reliability',
        'period_days': period_days,
        'window_start': window_start.isoformat(timespec='seconds'),
        'window_end': window_end.isoformat(timespec='seconds'),
        'previous_window_start': prev_start.isoformat(timespec='seconds'),
        'previous_window_end': prev_end.isoformat(timespec='seconds'),
        'site_id': site_id,
        'customers_served': total_customers if total_customers > 0 else None,
        'sites_declaring_customers': declaring_sites,
        'kpis': kpis,
        'counts': {
            'sustained_interruptions': metrics['sustained_interruptions'],
            'unattributed_interruptions': metrics['unattributed_interruptions'],
            'ongoing_outages': len(ongoing),
            'planned_outages_in_window': int(planned),
        },
        'ongoing_outages': ongoing,
        'contributors': top_contributors,
        'data_freshness': {
            'latest_recorded_restoration': latest_event,
            'generated_at': _application.now(),
        },
    }


def install_kpi_routes() -> None:
    """Own the reliability KPI surface inside the analytics domain."""
    app = _application.app
    marker = '_euas_kpi_routes'
    if getattr(app.state, marker, False):
        return

    @app.get('/api/kpis/reliability')
    def reliability_kpis_route(
        site_id: Optional[int] = None,
        period_days: int = Query(365, ge=30, le=3650),
        as_of: Optional[str] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return compute_reliability_kpis(conn, site_id, period_days, as_of)

    @app.get('/api/kpis/reliability.csv')
    def reliability_kpis_csv_route(
        site_id: Optional[int] = None,
        period_days: int = Query(365, ge=30, le=3650),
        as_of: Optional[str] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            result = compute_reliability_kpis(conn, site_id, period_days, as_of)
        rows_out = [
            [kpi['id'], kpi['name'], kpi['value'], kpi['previous_value'],
             kpi['change_pct'], kpi['unit'], kpi['direction']]
            for kpi in result['kpis'].values()
        ]
        return _application.csv_response(
            'EUAS_reliability_kpis.csv',
            ['KPI', 'Name', 'Value', 'Previous Value', 'Change %', 'Unit', 'Direction'],
            rows_out,
        )

    @app.patch('/api/sites/{site_id}/customer-count')
    def set_site_customer_count_route(
        site_id: int,
        body: _application.SiteCustomerCountPatch,
        user=Depends(require_roles('admin')),
    ):
        # Customer counts drive regulatory indices (SAIDI/SAIFI), so the
        # change is audited with before/after values.
        with db() as conn:
            site = conn.execute(
                'SELECT id,site_code,customer_count FROM sites WHERE id=?',
                (site_id,),
            ).fetchone()
            if not site:
                raise HTTPException(404, 'Site not found')
            old_value = site['customer_count']
            conn.execute(
                'UPDATE sites SET customer_count=? WHERE id=?',
                (body.customer_count, site_id),
            )
            _application.audit(
                conn,
                user['id'],
                'UPDATE',
                'Sites',
                site['site_code'],
                {'customer_count': old_value},
                {'customer_count': body.customer_count},
            )
            return {
                'ok': True,
                'site_id': site_id,
                'customer_count': body.customer_count,
            }

    app.openapi_schema = None
    setattr(app.state, marker, True)
