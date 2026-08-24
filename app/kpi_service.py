"""Executive utilities KPI service (Maximo-class operational intelligence).

Deterministic, set-based aggregation over the existing EUAS operational tables.
No predictive/ML claims: every signal carries an explicit ``kind`` label drawn
from {trend, anomaly, deterioration, risk_indicator}. All formulas are defined
here once so API, dashboards, exports and tests cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .database import db  # noqa: F401  (re-export convenience for operators)

OVERDUE_REQUISITION_DAYS = 7
STALE_SOURCE_HOURS = 24


# --------------------------------------------------------------------------- #
# Filters & windows
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExecutiveFilters:
    period_end: Optional[str] = None
    period_days: int = 30
    site_id: Optional[int] = None
    region: Optional[str] = None
    asset_type_id: Optional[int] = None
    criticality: Optional[str] = None

    def window(self) -> dict:
        try:
            end = date.fromisoformat(str(self.period_end)[:10])
        except (TypeError, ValueError):
            end = date.today()
        start = end - timedelta(days=max(1, self.period_days) - 1)
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=max(1, self.period_days) - 1)
        return {
            'period_start': start.isoformat(),
            'period_end': end.isoformat(),
            'period_days': max(1, self.period_days),
            'previous_start': prev_start.isoformat(),
            'previous_end': prev_end.isoformat(),
        }


def _asset_scope(f: ExecutiveFilters, alias_a='a') -> tuple[str, list]:
    """WHERE fragment scoping assets by site/region/class/criticality."""
    clause = ''
    args: list = []
    if f.site_id is not None:
        clause += ' AND s.id=?'
        args.append(f.site_id)
    if f.region:
        clause += ' AND s.region=?'
        args.append(f.region)
    if f.asset_type_id is not None:
        clause += f' AND {alias_a}.asset_type_id=?'
        args.append(f.asset_type_id)
    if f.criticality:
        clause += f' AND {alias_a}.criticality=?'
        args.append(f.criticality)
    return clause, args


def _outage_overlap_hours(start_value, end_value, win_start: datetime, win_end: datetime) -> float:
    try:
        start = datetime.fromisoformat(str(start_value))
    except (TypeError, ValueError):
        return 0.0
    if end_value:
        try:
            end = datetime.fromisoformat(str(end_value))
        except (TypeError, ValueError):
            end = win_end
    else:
        end = min(datetime.now(), win_end)
    left = max(start, win_start)
    right = min(end, win_end)
    return max(0.0, (right - left).total_seconds() / 3600.0)


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Reliability (SAIDI / SAIFI / CAIDI / availability / outages)
# --------------------------------------------------------------------------- #

def compute_reliability(conn, f: ExecutiveFilters) -> dict:
    w = f.window()
    scope_sql, scope_args = _asset_scope(f)
    assets = _rows(conn.execute(
        'SELECT a.id, s.id site_id FROM assets a'
        ' LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE a.commissioning_date IS NOT NULL OR 1=1' + scope_sql,
        scope_args,
    ))
    asset_ids = [a['id'] for a in assets]
    site_ids = {a['site_id'] for a in assets if a['site_id']}

    def _window_metrics(win_start_iso: str, win_end_iso: str) -> dict:
        win_start = datetime.fromisoformat(win_start_iso + 'T00:00:00')
        win_end = datetime.fromisoformat(win_end_iso + 'T23:59:59')
        window_hours = max(1.0, (win_end - win_start).total_seconds() / 3600.0)
        outages: list[dict] = []
        # Outage rows are fetched per window with one grouped query over the
        # scoped sites; asset-level attribution stays exact via o.asset_id.
        o_sql = (
            'SELECT o.*, s.customers_served FROM asset_outages o'
            ' JOIN assets a2 ON a2.id=o.asset_id'
            ' LEFT JOIN locations l ON l.id=a2.location_id'
            ' LEFT JOIN sites s ON s.id=l.site_id'
            ' WHERE o.outage_type=\'Forced\''
            ' AND o.start_at<=? AND (o.end_at IS NULL OR o.end_at>=?)'
        )
        o_args: list = [win_end_iso + 'T23:59:59', win_start_iso]
        if f.site_id is not None:
            o_sql += ' AND o.site_id=?'
            o_args.append(f.site_id)
        for row in _rows(conn.execute(o_sql, o_args)):
            hours = _outage_overlap_hours(row['start_at'], row.get('end_at'), win_start, win_end)
            if hours <= 0:
                continue
            row['overlap_hours'] = round(hours, 3)
            outages.append(row)
        downtime = sum(x['overlap_hours'] for x in outages)
        failures = len(outages)
        period_hours = window_hours * max(len(asset_ids), 1)
        uptime = max(0.0, period_hours - downtime)
        availability = round(100 * uptime / period_hours, 2) if period_hours else 100.0
        return {
            'outages': outages,
            'failure_count': failures,
            'downtime_hours': round(downtime, 2),
            'period_hours': round(period_hours, 1),
            'availability_pct': availability,
        }

    current = _window_metrics(w['period_start'], w['period_end'])
    previous = _window_metrics(w['previous_start'], w['previous_end'])

    customers_total = int(conn.execute(
        'SELECT COALESCE(SUM(customers_served),0) FROM sites WHERE id IN (%s)'
        % (','.join('?' * len(site_ids)) or 'NULL'),
        tuple(site_ids),
    ).fetchone()[0] or 0) if site_ids else 0
    configured = customers_total > 0

    saidi_minutes = saifi = caidi_minutes = None
    if configured:
        customer_minutes = 0.0
        interruptions = 0
        for x in current['outages']:
            served = int(x.get('customers_served') or 0)
            customer_minutes += x['overlap_hours'] * 60.0 * served
            interruptions += served
        saidi_minutes = round(customer_minutes / customers_total, 2)
        saifi = round(interruptions / customers_total, 4)
        caidi_minutes = round(saidi_minutes / saifi, 2) if saifi else None

    planned = int(conn.execute(
        "SELECT COUNT(*) FROM asset_outages o JOIN assets a ON a.id=o.asset_id"
        " LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id"
        " WHERE o.outage_type<>'Forced' AND o.start_at>=? AND o.start_at<?" + scope_sql,
        [w['period_start'] + 'T00:00:00', w['period_end'] + 'T23:59:59'] + scope_args,
    ).fetchone()[0])

    durations = sorted((x['overlap_hours'] for x in current['outages']), reverse=True)
    weekly: dict[str, dict] = {}
    for x in current['outages']:
        bucket = str(x['start_at'])[:10]
        b = weekly.setdefault(bucket, {'period': bucket, 'outages': 0, 'downtime_hours': 0.0})
        b['outages'] += 1
        b['downtime_hours'] = round(b['downtime_hours'] + x['overlap_hours'], 2)
    trend = sorted(weekly.values(), key=lambda x: x['period'])

    return {
        'availability_pct': current['availability_pct'],
        'availability_previous_pct': previous['availability_pct'],
        'total_downtime_hours': current['downtime_hours'],
        'total_downtime_hours_previous': previous['downtime_hours'],
        'period_hours': current['period_hours'],
        'outage_count': current['failure_count'],
        'unplanned_outages': current['failure_count'],
        'planned_outages': planned,
        'avg_outage_duration_hours': round(sum(durations) / len(durations), 2) if durations else 0.0,
        'saidi_minutes': saidi_minutes,
        'saifi': saifi,
        'caidi_minutes': caidi_minutes,
        'customers_basis': 'configured' if configured else 'unconfigured',
        'customers_served_total': customers_total,
        'trend': trend,
    }


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #

def compute_asset_kpis(conn, f: ExecutiveFilters) -> dict:
    from .application import _asset_health  # deferred: single canonical scorer

    scope_sql, scope_args = _asset_scope(f)
    counts = _rows(conn.execute(
        'SELECT COUNT(*) total,'
        " SUM(CASE WHEN a.status='Operating' THEN 1 ELSE 0 END) operating,"
        " SUM(CASE WHEN a.status='Standby' THEN 1 ELSE 0 END) standby,"
        " SUM(CASE WHEN a.criticality='Critical' THEN 1 ELSE 0 END) critical_total,"
        " SUM(CASE WHEN a.criticality='Critical' AND a.status NOT IN ('Operating','Standby') THEN 1 ELSE 0 END) critical_down,"
        " SUM(CASE WHEN a.condition IN ('Warning','Poor','Critical') THEN 1 ELSE 0 END) condition_attention"
        ' FROM assets a LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql,
        scope_args,
    ))[0]

    health_rows = [_asset_health(conn, r['id'])
                   for r in _rows(conn.execute(
                       'SELECT a.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id'
                       ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql,
                       scope_args))]
    distribution = {'Healthy': 0, 'Monitor': 0, 'Warning': 0, 'Critical': 0}
    for h in health_rows:
        distribution[h['risk_band']] = distribution.get(h['risk_band'], 0) + 1

    eol_cutoff = (date.today() + timedelta(days=90)).isoformat()
    eol = _rows(conn.execute(
        'SELECT a.id,a.asset_no,a.name,a.warranty_expiry,a.criticality FROM assets a'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE a.warranty_expiry IS NOT NULL AND a.warranty_expiry<=?" + scope_sql +
        ' ORDER BY a.warranty_expiry LIMIT 10',
        [eol_cutoff] + scope_args,
    ))

    cutoff90 = (date.today() - timedelta(days=90)).isoformat()
    repeat_failures = _rows(conn.execute(
        'SELECT a.id,a.asset_no,a.name,COUNT(*) failures_90d,MAX(w.actual_finish) last_failure'
        ' FROM work_orders w JOIN assets a ON a.id=w.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE w.status IN ('Completed','Closed') AND w.work_type LIKE 'Corrective%'"
        ' AND COALESCE(w.actual_finish,w.created_at)>=?' + scope_sql +
        ' GROUP BY a.id HAVING COUNT(*)>=2 ORDER BY failures_90d DESC,a.asset_no LIMIT 10',
        [cutoff90] + scope_args,
    ))

    high_risk = sorted(health_rows, key=lambda h: (h['score'],))[:10]
    total = int(counts['total'] or 0)
    down = total - int(counts['operating'] or 0) - int(counts['standby'] or 0)
    return {
        'total': total,
        'operating': int(counts['operating'] or 0),
        'standby': int(counts['standby'] or 0),
        'down': down,
        'critical_total': int(counts['critical_total'] or 0),
        'critical_down': int(counts['critical_down'] or 0),
        'condition_attention': int(counts['condition_attention'] or 0),
        'health_distribution': distribution,
        'high_risk_assets': [
            {'id': h['asset_id'], 'asset_no': h['asset_no'], 'name': h['name'],
             'score': h['score'], 'band': h['risk_band']} for h in high_risk],
        'end_of_life_exposure': eol,
        'repeat_failure_assets': repeat_failures,
    }


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #

_OPEN_WO = "w.status NOT IN ('Completed','Closed','Cancelled')"
_PRIORITY_ORDER = "CASE w.priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC"


def _wo_scope(f: ExecutiveFilters) -> tuple[str, list]:
    clause, args = _asset_scope(f)
    if f.criticality:
        # criticality filter already applied through the asset join above
        pass
    return clause, args


def compute_maintenance_kpis(conn, f: ExecutiveFilters) -> dict:
    from .application import _workforce_week_capacity  # deferred canonical capacity

    w = f.window()
    scope_sql, scope_args = _wo_scope(f)
    base = (' FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id'
            ' LEFT JOIN locations l ON l.id=w.location_id'
            ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql)

    def scalar(sql_extra: str, extra_args: list | None = None, sql_base: str = base) -> int:
        return int(conn.execute('SELECT COUNT(*)' + sql_base + sql_extra,
                                list(scope_args) + (extra_args or [])).fetchone()[0])

    open_wo = scalar(' AND ' + _OPEN_WO)
    overdue_wo = scalar(f' AND {_OPEN_WO} AND w.target_finish IS NOT NULL AND w.target_finish<?', [w['period_end']])
    emergency_wo = scalar(f" AND {_OPEN_WO} AND (w.priority='Emergency' OR w.work_type='Emergency')")
    high_risk_overdue = scalar(
        f" AND {_OPEN_WO} AND w.priority IN ('Emergency','Critical','High')"
        ' AND w.target_finish IS NOT NULL AND w.target_finish<?',
        [w['period_end']])
    unassigned_critical = scalar(
        f" AND {_OPEN_WO} AND w.priority IN ('Emergency','Critical') AND w.assigned_to IS NULL")

    backlog_rows = _rows(conn.execute(
        'SELECT COALESCE(SUM(w.estimated_hours),0) hours, COUNT(*) jobs' + base +
        ' AND ' + _OPEN_WO, scope_args))
    backlog_hours = float(backlog_rows[0]['hours'] or 0)
    capacity = _workforce_week_capacity(conn, date.today(), f.site_id)
    weekly_capacity = float(capacity.get('capacity_hours') or 0)

    pm_total = int(conn.execute(
        'SELECT COUNT(*) FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE p.active=1" + scope_sql, scope_args).fetchone()[0])
    pm_overdue = int(conn.execute(
        'SELECT COUNT(*) FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE p.active=1 AND p.trigger_type='Calendar' AND p.next_due IS NOT NULL AND p.next_due<?"
        + scope_sql, [w['period_end']] + scope_args).fetchone()[0])
    pm_compliance = round(100 * (pm_total - pm_overdue) / pm_total, 1) if pm_total else 100.0

    scheduled = int(conn.execute(
        'SELECT COUNT(*)' + base + ' AND w.target_finish>=? AND w.target_finish<?'
        " AND w.status<>'Cancelled'",
        scope_args + [w['period_start'], w['period_end'] + 'T23:59:59']).fetchone()[0])
    met = int(conn.execute(
        'SELECT COUNT(*)' + base + ' AND w.target_finish>=? AND w.target_finish<?'
        " AND w.status IN ('Completed','Closed') AND w.actual_finish IS NOT NULL"
        ' AND w.actual_finish<=w.target_finish',
        scope_args + [w['period_start'], w['period_end'] + 'T23:59:59']).fetchone()[0])
    schedule_compliance = round(100 * met / scheduled, 1) if scheduled else 100.0

    rel_failures = int(conn.execute(
        'SELECT COUNT(*)' + base +
        " AND w.status IN ('Completed','Closed') AND w.work_type LIKE 'Corrective%'"
        ' AND COALESCE(w.actual_finish,w.created_at)>=?',
        scope_args + [(date.today() - timedelta(days=90)).isoformat()]).fetchone()[0])
    completed_90 = int(conn.execute(
        'SELECT COUNT(*)' + base +
        " AND w.status IN ('Completed','Closed') AND COALESCE(w.actual_finish,w.created_at)>=?",
        scope_args + [(date.today() - timedelta(days=90)).isoformat()]).fetchone()[0])
    repeat_failure_rate = round(100 * rel_failures / completed_90, 1) if completed_90 else 0.0

    mttr = mtbf = None
    rel = compute_reliability(conn, f)
    if rel['outage_count']:
        mttr = round(rel['total_downtime_hours'] / rel['outage_count'], 2)
        uptime = max(0.0, rel['period_hours'] - rel['total_downtime_hours'])
        mtbf = round(uptime / rel['outage_count'], 2)

    by_priority = _rows(conn.execute(
        'SELECT w.priority, COUNT(*) count' + base + ' AND ' + _OPEN_WO +
        ' GROUP BY w.priority ORDER BY ' + _PRIORITY_ORDER, scope_args))

    return {
        'open_wo': open_wo,
        'overdue_wo': overdue_wo,
        'emergency_wo': emergency_wo,
        'high_risk_overdue_wo': high_risk_overdue,
        'unassigned_critical_wo': unassigned_critical,
        'pm_compliance_pct': pm_compliance,
        'schedule_compliance_pct': schedule_compliance,
        'backlog_hours': round(backlog_hours, 1),
        'backlog_weeks': round(backlog_hours / weekly_capacity, 1) if weekly_capacity else None,
        'weekly_capacity_hours': round(weekly_capacity, 1),
        'mtbf_hours': mtbf,
        'mttr_hours': mttr,
        'repeat_failure_rate_pct': repeat_failure_rate,
        'by_priority': by_priority,
    }


# --------------------------------------------------------------------------- #
# Condition / alarms
# --------------------------------------------------------------------------- #

def compute_condition_kpis(conn, f: ExecutiveFilters) -> dict:
    scope_sql, scope_args = _asset_scope(f)
    active_where = "oa.status IN ('Open','Acknowledged')"
    active = int(conn.execute(
        'SELECT COUNT(*) FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + active_where + scope_sql, scope_args).fetchone()[0])
    critical = int(conn.execute(
        'SELECT COUNT(*) FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE " + active_where + " AND oa.severity='Critical'" + scope_sql,
        scope_args).fetchone()[0])
    unacknowledged = int(conn.execute(
        'SELECT COUNT(*) FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        " WHERE oa.status='Open'" + scope_sql, scope_args).fetchone()[0])

    storms = _rows(conn.execute(
        'SELECT tc.id channel_id, tc.channel_code, tc.name channel_name, a.id asset_id,'
        ' a.asset_no, a.name asset_name, oa.occurrence_count, oa.severity, oa.alarm_no'
        ' FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id'
        ' JOIN assets a ON a.id=oa.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + active_where + ' AND oa.occurrence_count>=3' + scope_sql +
        ' ORDER BY oa.occurrence_count DESC LIMIT 10', scope_args))

    repeated_assets = _rows(conn.execute(
        'SELECT a.id asset_id, a.asset_no, a.name asset_name, COUNT(*) alarm_count,'
        " SUM(CASE WHEN oa.severity='Critical' THEN 1 ELSE 0 END) critical_count"
        ' FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + active_where + scope_sql +
        ' GROUP BY a.id HAVING COUNT(*)>=2 ORDER BY alarm_count DESC LIMIT 10', scope_args))

    stale_cut = (datetime.now() - timedelta(hours=STALE_SOURCE_HOURS)).isoformat(timespec='seconds')
    latest_source = conn.execute(
        'SELECT MAX(ts) FROM ('
        ' SELECT MAX(oa.last_seen_at) ts FROM operational_alarms oa'
        ' JOIN assets a ON a.id=oa.asset_id LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql +
        ' UNION ALL SELECT MAX(tc.last_reading_at) FROM telemetry_channels tc'
        ' JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql + ')',
        scope_args + scope_args).fetchone()[0]

    return {
        'active_alarms': active,
        'critical_active_alarms': critical,
        'unacknowledged_alarms': unacknowledged,
        'alarm_storms': storms,
        'repeated_alarm_assets': repeated_assets,
        'latest_source_timestamp': latest_source,
    }


# --------------------------------------------------------------------------- #
# Inventory & procurement
# --------------------------------------------------------------------------- #

def compute_inventory_procurement_kpis(conn, f: ExecutiveFilters) -> dict:
    w = f.window()
    stockouts = _rows(conn.execute(
        'SELECT i.id, i.item_no, i.name, i.current_stock, i.reserved_stock, i.reorder_point,'
        ' w.name warehouse_name FROM inventory_items i'
        ' LEFT JOIN warehouses w ON w.id=i.warehouse_id'
        ' WHERE i.current_stock-i.reserved_stock<=0 ORDER BY i.item_no LIMIT 50'))
    reserved_value = conn.execute(
        'SELECT COALESCE(SUM(i.reserved_stock*i.unit_price),0) FROM inventory_items i'
    ).fetchone()[0] or 0
    ati_value = conn.execute(
        'SELECT COALESCE(SUM((i.current_stock-i.reserved_stock)*i.unit_price),0)'
        ' FROM inventory_items i').fetchone()[0] or 0

    blocked = int(conn.execute(
        'SELECT COUNT(DISTINCT r.work_order_id) FROM work_order_requirements r'
        ' JOIN inventory_items i ON i.id=r.inventory_item_id'
        ' JOIN work_orders w ON w.id=r.work_order_id'
        " WHERE r.status<>'Cancelled' AND " + _OPEN_WO +
        ' AND (i.current_stock-i.reserved_stock)<r.quantity').fetchone()[0])
    blocked_high_risk = int(conn.execute(
        'SELECT COUNT(DISTINCT r.work_order_id) FROM work_order_requirements r'
        ' JOIN inventory_items i ON i.id=r.inventory_item_id'
        ' JOIN work_orders w ON w.id=r.work_order_id'
        " WHERE r.status<>'Cancelled' AND " + _OPEN_WO +
        " AND w.priority IN ('Emergency','Critical','High')"
        ' AND (i.current_stock-i.reserved_stock)<r.quantity').fetchone()[0])

    overdue_prs = _rows(conn.execute(
        'SELECT pr.id, pr.pr_no, pr.title, pr.created_at FROM purchase_requisitions pr'
        " WHERE pr.status='Submitted' AND pr.created_at<?"
        ' ORDER BY pr.created_at LIMIT 20',
        [(datetime.now() - timedelta(days=OVERDUE_REQUISITION_DAYS)).isoformat(timespec='seconds')]))
    overdue_pos = int(conn.execute(
        "SELECT COUNT(*) FROM purchase_orders po WHERE po.expected_delivery IS NOT NULL"
        " AND po.expected_delivery<? AND po.status NOT IN ('Received','Cancelled')",
        [w['period_end']]).fetchone()[0])
    lead_time_days = None
    receipts = _rows(conn.execute(
        'SELECT po.order_date, po.actual_receipt FROM purchase_orders po'
        ' WHERE po.actual_receipt IS NOT NULL AND po.order_date IS NOT NULL'))
    spans: list[float] = []
    for r in receipts:
        try:
            delta = (datetime.fromisoformat(str(r['actual_receipt'])[:19])
                     - datetime.fromisoformat(str(r['order_date'])[:19])).total_seconds() / 86400.0
        except (TypeError, ValueError):
            continue
        if delta >= 0:
            spans.append(delta)
    if spans:
        lead_time_days = round(sum(spans) / len(spans), 1)

    return {
        'stockout_items': len(stockouts),
        'stockouts': stockouts[:10],
        'work_blocked_by_parts': blocked,
        'blocked_high_risk_work': blocked_high_risk,
        'reserved_stock_value': round(float(reserved_value), 2),
        'available_to_issue_value': round(float(ati_value), 2),
        'overdue_requisitions': overdue_prs,
        'overdue_purchase_orders': overdue_pos,
        'avg_po_lead_time_days': lead_time_days,
    }


# --------------------------------------------------------------------------- #
# Workforce
# --------------------------------------------------------------------------- #

_ACTIVE_DISPATCH_STATES = ('Dispatched', 'Accepted', 'En Route', 'On Site')


def compute_workforce_kpis(conn, f: ExecutiveFilters) -> dict:
    technicians = int(conn.execute(
        "SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id"
        " WHERE r.code='technician' AND u.active=1").fetchone()[0])
    dispatched = int(conn.execute(
        'SELECT COUNT(DISTINCT d.technician_user_id) FROM dispatch_assignments d'
        ' WHERE d.status IN (%s)' % ','.join('?' * len(_ACTIVE_DISPATCH_STATES)),
        _ACTIVE_DISPATCH_STATES).fetchone()[0])
    off_today = int(conn.execute(
        "SELECT COUNT(DISTINCT ta.user_id) FROM technician_absences ta"
        " WHERE ta.status='Approved' AND ta.start_date<=? AND ta.end_date>=?",
        [date.today().isoformat(), date.today().isoformat()]).fetchone()[0])

    scope_sql, scope_args = _wo_scope(f)
    unassigned_critical = _rows(conn.execute(
        'SELECT w.id, w.wo_no, w.title, w.priority, w.target_finish, a.asset_no' +
        ' FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id'
        ' LEFT JOIN locations l ON l.id=w.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id'
        f" WHERE {_OPEN_WO} AND w.priority IN ('Emergency','Critical') AND w.assigned_to IS NULL"
        + scope_sql + ' ORDER BY ' + _PRIORITY_ORDER + ', w.id LIMIT 10', scope_args))
    sla_breached = int(conn.execute(
        'SELECT COUNT(*) FROM work_orders w JOIN work_order_sla sla ON sla.work_order_id=w.id'
        ' LEFT JOIN assets a ON a.id=w.asset_id LEFT JOIN locations l ON l.id=w.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id'
        f" WHERE {_OPEN_WO} AND (sla.response_status='Breached' OR sla.resolution_status='Breached')"
        + scope_sql, scope_args).fetchone()[0])
    workload = _rows(conn.execute(
        'SELECT u.id user_id, u.full_name name, COUNT(*) open_wo,'
        " SUM(CASE WHEN w.priority IN ('Emergency','Critical') THEN 1 ELSE 0 END) critical_open"
        ' FROM work_orders w JOIN users u ON u.id=w.assigned_to'
        ' LEFT JOIN assets a ON a.id=w.asset_id'
        ' LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + _OPEN_WO + scope_sql +
        ' GROUP BY u.id ORDER BY open_wo DESC LIMIT 10', scope_args))

    skill_blockers = int(conn.execute(
        'SELECT COUNT(*) FROM work_orders w'
        ' LEFT JOIN work_order_craft_requirements cr ON cr.work_order_id=w.id'
        f' WHERE {_OPEN_WO} AND w.assigned_to IS NULL AND cr.craft_id IS NOT NULL'
        ' AND NOT EXISTS ('
        '   SELECT 1 FROM technician_profiles tp JOIN crafts c ON c.id=tp.craft_id'
        '   WHERE tp.user_id IS NOT NULL AND c.id=cr.craft_id AND tp.active=1)').fetchone()[0])

    available = max(0, technicians - dispatched - off_today)
    return {
        'technicians_total': technicians,
        'technicians_available': available,
        'technicians_dispatched': dispatched,
        'technicians_off_today': off_today,
        'unassigned_critical_work': unassigned_critical,
        'sla_breached_open': sla_breached,
        'workload_by_technician': workload,
        'skill_blockers': skill_blockers,
    }


# --------------------------------------------------------------------------- #
# Risk-weighted backlog
# --------------------------------------------------------------------------- #

_PRIORITY_BASE = {'Emergency': 40, 'Critical': 32, 'High': 24, 'Medium': 12, 'Low': 6}
_CRITICALITY_FACTOR = {'Critical': 1.5, 'High': 1.25, 'Medium': 1.0, 'Low': 0.85}


def risk_score_work_order(row: dict) -> tuple[float, dict]:
    """Deterministic, explainable risk score in [0,100] with component breakdown."""
    components: dict[str, float] = {}
    base = _PRIORITY_BASE.get(row.get('priority'), 6)
    factor = _CRITICALITY_FACTOR.get(row.get('criticality'), 1.0)
    components['priority_x_criticality'] = round(base * factor, 1)

    health = row.get('health_score')
    if health is None:
        components['asset_health_penalty'] = 5.0  # unknown condition: cautious default
    else:
        components['asset_health_penalty'] = round(max(0.0, 100 - float(health)) / 100 * 20, 1)

    overdue_days = row.get('overdue_days') or 0
    components['overdue_exposure'] = round(min(20.0, overdue_days * 1.0), 1)
    if row.get('response_breached'):
        components['sla_response_breach'] = 5.0
    if row.get('resolution_breached'):
        components['sla_resolution_breach'] = 10.0
    alarms = row.get('active_alarms') or 0
    components['active_alarms'] = round(min(15.0, alarms * 5.0), 1)
    if row.get('open_outage'):
        components['open_outage_impact'] = 12.0
    if row.get('safety_relevant'):
        components['safety_relevance'] = 10.0
    if row.get('parts_blocked'):
        components['parts_blocked'] = 8.0

    score = min(100.0, round(sum(components.values()), 1))
    return score, components


def risk_weighted_backlog(conn, f: ExecutiveFilters, limit: int = 25) -> dict:
    from .application import _asset_health, _work_order_parts_readiness  # deferred

    scope_sql, scope_args = _wo_scope(f)
    rows_ = _rows(conn.execute(
        'SELECT w.id, w.wo_no, w.title, w.priority, w.work_type, w.status, w.target_finish,'
        ' w.safety_requirements, a.id asset_id, a.asset_no, a.criticality, a.condition,' 
        ' s.name site_name' +
        ' FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id'
        ' LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + _OPEN_WO + scope_sql, scope_args))
    today_s = date.today().isoformat()
    scored: list[dict] = []
    critical_count = 0
    blocked_high_risk = 0
    for wo in rows_:
        wo['overdue_days'] = 0
        if wo.get('target_finish') and str(wo['target_finish'])[:10] < today_s:
            delta = date.today() - date.fromisoformat(str(wo['target_finish'])[:10])
            wo['overdue_days'] = delta.days
        wo['active_alarms'] = int(conn.execute(
            "SELECT COUNT(*) FROM operational_alarms WHERE asset_id=? AND status IN ('Open','Acknowledged')",
            (wo['asset_id'],)).fetchone()[0]) if wo['asset_id'] else 0
        wo['open_outage'] = bool(conn.execute(
            "SELECT id FROM asset_outages WHERE asset_id=? AND status='Open'",
            (wo['asset_id'],)).fetchone()) if wo['asset_id'] else False
        wo['safety_relevant'] = bool(str(wo.get('safety_requirements') or '').strip()) or \
            wo.get('priority') == 'Emergency'
        readiness = _work_order_parts_readiness(conn, wo['id'])
        wo['parts_blocked'] = readiness['state'] == 'Shortage'
        sla = conn.execute(
            'SELECT response_status,resolution_status FROM work_order_sla WHERE work_order_id=?',
            (wo['id'],)).fetchone()
        sla = dict(sla) if sla else {}
        wo['response_breached'] = sla.get('response_status') == 'Breached'
        wo['resolution_breached'] = sla.get('resolution_status') == 'Breached'
        health = _asset_health(conn, wo['asset_id']) if wo['asset_id'] else None
        wo['health_score'] = health['score'] if health else None
        wo['health_band'] = health['risk_band'] if health else 'Unknown'
        score, components = risk_score_work_order(wo)
        if wo['priority'] in ('Emergency', 'Critical'):
            critical_count += 1
        if wo['parts_blocked'] and wo['priority'] in ('Emergency', 'Critical', 'High'):
            blocked_high_risk += 1
        scored.append({
            'id': wo['id'], 'wo_no': wo['wo_no'], 'title': wo['title'],
            'priority': wo['priority'], 'status': wo['status'],
            'asset_no': wo['asset_no'], 'site_name': wo['site_name'],
            'criticality': wo['criticality'], 'health_band': wo['health_band'],
            'overdue_days': wo['overdue_days'], 'risk_score': score,
            'components': components, 'parts_blocked': wo['parts_blocked'],
            'safety_relevant': wo['safety_relevant'],
            'sla_breached': bool(wo['response_breached'] or wo['resolution_breached']),
        })
    scored.sort(key=lambda x: (-x['risk_score'], x['overdue_days'] * -1, x['wo_no']))
    risk_weighted_total = round(sum(x['risk_score'] for x in scored), 1)
    return {
        'summary': {
            'total_open': len(scored),
            'critical_count': critical_count,
            'risk_weighted_backlog': risk_weighted_total,
            'average_risk': round(risk_weighted_total / len(scored), 1) if scored else 0.0,
            'blocked_high_risk': blocked_high_risk,
        },
        'rows': scored[:limit],
    }


# --------------------------------------------------------------------------- #
# Explainable KPI changes (period vs previous period)
# --------------------------------------------------------------------------- #

def explain_kpi_changes(conn, f: ExecutiveFilters) -> dict:
    w = f.window()

    def outage_drivers(win_start: str, win_end: str) -> list[dict]:
        win_start_dt = datetime.fromisoformat(win_start + 'T00:00:00')
        win_end_dt = datetime.fromisoformat(win_end + 'T23:59:59')
        drivers: list[dict] = []
        for row in _rows(conn.execute(
                'SELECT o.*, a.asset_no, a.name asset_name FROM asset_outages o'
                ' JOIN assets a ON a.id=o.asset_id'
                " WHERE o.outage_type='Forced' AND o.start_at<=? AND (o.end_at IS NULL OR o.end_at>=?)",
                [win_end + 'T23:59:59', win_start])):
            hours = _outage_overlap_hours(row['start_at'], row.get('end_at'), win_start_dt, win_end_dt)
            if hours > 0:
                drivers.append({
                    'kind': 'unplanned_outage',
                    'label': f"{row['asset_no']} unplanned outage",
                    'hours': round(hours, 2),
                    'link': {'module': 'operations', 'record': row['outage_no'], 'id': row['id']},
                })
        drivers.sort(key=lambda d: d['hours'], reverse=True)
        return drivers[:5]

    rel_now = compute_reliability(conn, f)
    prev_scope = ExecutiveFilters(
        period_end=w['previous_end'], period_days=f.period_days, site_id=f.site_id,
        region=f.region, asset_type_id=f.asset_type_id, criticality=f.criticality)
    rel_prev = compute_reliability(conn, prev_scope)

    explanations: dict[str, Any] = {}
    avail_delta = round(rel_now['availability_pct'] - rel_prev['availability_pct'], 2)
    explanations['availability'] = {
        'current': rel_now['availability_pct'],
        'previous': rel_prev['availability_pct'],
        'delta': avail_delta,
        'drivers': outage_drivers(w['period_start'], w['period_end']),
    }

    mttr_delta = None
    if rel_now['outage_count'] and rel_prev['outage_count']:
        mttr_now = rel_now['total_downtime_hours'] / rel_now['outage_count']
        mttr_prev = rel_prev['total_downtime_hours'] / rel_prev['outage_count']
        mttr_delta = round(mttr_now - mttr_prev, 2)
    slowest = _rows(conn.execute(
        'SELECT w.wo_no, w.title, w.actual_hours, a.asset_no FROM work_orders w'
        ' LEFT JOIN assets a ON a.id=w.asset_id'
        " WHERE w.status IN ('Completed','Closed') AND w.work_type LIKE 'Corrective%'"
        ' AND COALESCE(w.actual_finish,w.created_at)>=?'
        ' ORDER BY w.actual_hours DESC LIMIT 3', [w['period_start']]))
    explanations['mttr'] = {
        'current': rel_now['total_downtime_hours'] / rel_now['outage_count'] if rel_now['outage_count'] else None,
        'delta': mttr_delta,
        'drivers': [
            {'kind': 'long_repair', 'label': f"{r['asset_no'] or '—'} repair {r['wo_no']}",
             'hours': float(r['actual_hours'] or 0)}
            for r in slowest if (r['actual_hours'] or 0) > 0],
    }
    explanations['reliability'] = {
        'outages_current': rel_now['outage_count'],
        'outages_previous': rel_prev['outage_count'],
        'downtime_delta_hours': round(rel_now['total_downtime_hours'] - rel_prev['total_downtime_hours'], 2),
        'drivers': outage_drivers(w['period_start'], w['period_end']),
    }

    maint_now = compute_maintenance_kpis(conn, f)
    maint_prev = compute_maintenance_kpis(conn, prev_scope)
    explanations['pm_compliance'] = {
        'current': maint_now['pm_compliance_pct'],
        'previous': maint_prev['pm_compliance_pct'],
        'delta': round(maint_now['pm_compliance_pct'] - maint_prev['pm_compliance_pct'], 1),
    }
    explanations['backlog'] = {
        'current': maint_now['open_wo'],
        'previous': maint_prev['open_wo'],
        'delta': maint_now['open_wo'] - maint_prev['open_wo'],
    }
    explanations['emergency_share'] = {
        'current': maint_now['emergency_wo'],
        'previous': maint_prev['emergency_wo'],
        'delta': maint_now['emergency_wo'] - maint_prev['emergency_wo'],
    }
    return explanations


# --------------------------------------------------------------------------- #
# Deterministic deterioration signals (no ML claims)
# --------------------------------------------------------------------------- #

def _linear_slope_per_day(points: list[tuple[float, datetime]]) -> Optional[float]:
    if len(points) < 2:
        return None
    t0 = points[0][1]
    xs = [max(0.0, (p[1] - t0).total_seconds() / 86400.0) for p in points]
    ys = [p[0] for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def compute_deterioration_signals(conn, f: ExecutiveFilters, limit: int = 30) -> dict:
    w = f.window()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    signals: list[dict] = []

    channels = _rows(conn.execute(
        'SELECT tc.*, a.asset_no, a.name asset_name FROM telemetry_channels tc'
        ' JOIN assets a ON a.id=tc.asset_id WHERE tc.active=1'))
    for ch in channels:
        readings = _rows(conn.execute(
            'SELECT value, captured_at FROM telemetry_readings WHERE channel_id=?'
            ' AND captured_at>=? ORDER BY captured_at', (ch['id'], cutoff)))
        if not readings:
            continue
        points = []
        for r in readings:
            try:
                points.append((float(r['value']), datetime.fromisoformat(str(r['captured_at']))))
            except (TypeError, ValueError):
                continue
        span = None
        for hi_key, lo_key in (('critical_high', 'critical_low'), ('warning_high', 'warning_low')):
            if ch.get(hi_key) is not None and ch.get(lo_key) is not None:
                span = abs(float(ch[hi_key]) - float(ch[lo_key]))
                break
        if not span:
            values = [p[0] for p in points]
            span = max(values) - min(values)
        slope = _linear_slope_per_day(points)
        slope_pct = round(100 * slope / span, 3) if (slope is not None and span) else None

        excursions = 0
        critical_excursions = 0
        for value, _ts in points:
            level, _thr = _channel_level(ch, value)
            if level == 'Warning':
                excursions += 1
            elif level == 'Critical':
                excursions += 1
                critical_excursions += 1
        last_value = points[-1][0]
        worsening_toward_high = (
            slope_pct is not None and slope_pct >= 1.0
            and ch.get('warning_high') is not None
            and last_value >= float(ch['warning_high']) * 0.9)
        worsening_toward_low = (
            slope_pct is not None and slope_pct <= -1.0
            and ch.get('warning_low') is not None
            and last_value <= float(ch['warning_low']) * 1.1)
        if critical_excursions:
            kind = 'anomaly'
        elif worsening_toward_high or worsening_toward_low:
            kind = 'deterioration'
        elif slope_pct is not None and abs(slope_pct) >= 0.5:
            kind = 'trend'
        else:
            continue
        signals.append({
            'kind': kind,
            'subject_type': 'channel',
            'id': ch['id'],
            'code': ch['channel_code'],
            'label': f"{ch['asset_no']} — {ch['name']}",
            'detail': {
                'slope_pct_of_span_per_day': slope_pct,
                'excursions_30d': excursions,
                'critical_excursions_30d': critical_excursions,
                'last_value': last_value,
                'unit': ch.get('unit'),
                'samples': len(points),
            },
            'link': {'module': 'telemetry', 'record': ch['channel_code'], 'asset_id': ch['asset_id']},
        })

    storm_channels = {s['channel_id'] for s in compute_condition_kpis(conn, f)['alarm_storms']}
    for ch in channels:
        if ch['id'] in storm_channels:
            signals.append({
                'kind': 'risk_indicator',
                'subject_type': 'channel',
                'id': ch['id'],
                'code': ch['channel_code'],
                'label': f"{ch['asset_no']} — recurring alarm {ch['name']}",
                'detail': {'basis': 'active alarm recurrence count >= 3'},
                'link': {'module': 'telemetry', 'record': ch['channel_code'], 'asset_id': ch['asset_id']},
            })

    repeat = compute_asset_kpis(conn, f)['repeat_failure_assets']
    for r in repeat:
        signals.append({
            'kind': 'risk_indicator',
            'subject_type': 'asset',
            'id': r['id'],
            'code': r['asset_no'],
            'label': f"{r['asset_no']} — {r['failures_90d']} corrective failures in 90 days",
            'detail': {'failures_90d': r['failures_90d'], 'last_failure': r['last_failure']},
            'link': {'module': 'assets', 'record': r['asset_no'], 'asset_id': r['id']},
        })

    order = {'deterioration': 0, 'anomaly': 1, 'risk_indicator': 2, 'trend': 3}
    signals.sort(key=lambda s: (order.get(s['kind'], 9), s['code']))
    return {
        'signals': signals[:limit],
        'labels_used': ['trend', 'anomaly', 'deterioration', 'risk_indicator'],
        'note': 'Deterministic trend analytics only; no failure probabilities are implied.',
        'calculated_at': datetime.now().isoformat(timespec='seconds'),
    }


def _channel_level(channel: dict, value: float) -> tuple[Optional[str], Optional[float]]:
    checks = [
        ('Critical', 'critical_high', 'high'), ('Critical', 'critical_low', 'low'),
        ('Warning', 'warning_high', 'high'), ('Warning', 'warning_low', 'low'),
    ]
    for severity, key, direction in checks:
        threshold = channel.get(key)
        if threshold is None:
            continue
        threshold = float(threshold)
        if direction == 'high' and value >= threshold:
            return severity, threshold
        if direction == 'low' and value <= threshold:
            return severity, threshold
    return None, None


# --------------------------------------------------------------------------- #
# Freshness + snapshot assembly
# --------------------------------------------------------------------------- #

def compute_freshness(conn, f: ExecutiveFilters, sections: dict) -> dict:
    calculated_at = datetime.now().isoformat(timespec='seconds')
    latest = None
    for section in sections.values():
        candidate = section.get('latest_source_timestamp') if isinstance(section, dict) else None
        if candidate and (latest is None or str(candidate) > str(latest)):
            latest = candidate
    stale = False
    if latest:
        try:
            age_hours = (datetime.now() - datetime.fromisoformat(str(latest))).total_seconds() / 3600.0
            stale = age_hours > STALE_SOURCE_HOURS
        except (TypeError, ValueError):
            stale = True
    else:
        stale = True
    return {
        'calculated_at': calculated_at,
        'latest_source_timestamp': latest,
        'stale_after_hours': STALE_SOURCE_HOURS,
        'state': 'stale' if stale else 'current',
    }


def executive_snapshot(conn, f: ExecutiveFilters) -> dict:
    reliability = compute_reliability(conn, f)
    assets = compute_asset_kpis(conn, f)
    maintenance = compute_maintenance_kpis(conn, f)
    condition = compute_condition_kpis(conn, f)
    inventory = compute_inventory_procurement_kpis(conn, f)
    workforce = compute_workforce_kpis(conn, f)
    backlog = risk_weighted_backlog(conn, f, limit=10)
    sections = {
        'reliability': reliability,
        'assets': assets,
        'maintenance': maintenance,
        'condition': condition,
        'inventory_procurement': inventory,
    }
    payload = {
        'window': f.window(),
        'filters_applied': {
            'site_id': f.site_id, 'region': f.region, 'asset_type_id': f.asset_type_id,
            'criticality': f.criticality, 'period_days': f.period_days,
            'period_end': f.period_end,
        },
        'freshness': compute_freshness(conn, f, sections),
        'reliability': reliability,
        'assets': assets,
        'maintenance': maintenance,
        'condition': condition,
        'inventory_procurement': inventory,
        'workforce': workforce,
        'risk_backlog_summary': backlog['summary'],
        'top_risk_contributors': backlog['rows'][:10],
        'explanations': explain_kpi_changes(conn, f),
    }
    return payload
