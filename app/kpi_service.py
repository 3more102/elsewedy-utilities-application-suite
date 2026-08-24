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


OVERDUE_REQUISITION_DAYS = 7
STALE_SOURCE_HOURS = 24
SNAPSHOT_TTL_MINUTES = 15
_IN_CLAUSE_CHUNK = 400


def _chunked(items: list, size: int = _IN_CLAUSE_CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _asset_health_map(conn, asset_ids: list[int]) -> dict[int, dict]:
    """Set-based batch evaluation of the canonical per-asset health score.

    Reproduces app.application._asset_health exactly (same penalty weights,
    same caps, same banding) while replacing its per-asset query fan-out with
    five grouped statements. The equivalence is enforced by regression tests.
    """
    from .application import _asset_health as _canonical  # noqa: F401 (equivalence reference)

    unique_ids = sorted({int(a) for a in asset_ids})
    if not unique_ids:
        return {}
    today_s = date.today().isoformat()
    condition_penalty_map = {'Good': 0, 'Fair': 10, 'Warning': 25, 'Poor': 40, 'Critical': 55}
    criticality_penalty_map = {'Low': 0, 'Medium': 3, 'High': 7, 'Critical': 12}

    base: dict[int, dict] = {}
    for chunk in _chunked(unique_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f'SELECT id,condition,criticality,status FROM assets WHERE id IN ({placeholders})',
                chunk)):
            base[r['id']] = dict(r)

    work_agg: dict[int, dict] = {}
    for chunk in _chunked(unique_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f'''SELECT asset_id,
                      SUM(CASE WHEN priority IN ('Emergency','Critical','High') THEN 1 ELSE 0 END) high,
                      SUM(CASE WHEN target_finish IS NOT NULL AND target_finish<? THEN 1 ELSE 0 END) overdue
                    FROM work_orders
                    WHERE status NOT IN ('Completed','Closed','Cancelled')
                      AND asset_id IN ({placeholders}) GROUP BY asset_id''',
                [today_s] + chunk)):
            work_agg[r['asset_id']] = {
                'high': int(r['high'] or 0), 'overdue': int(r['overdue'] or 0)}

    failed_inspections: dict[int, int] = {}
    sla_breaches: dict[int, int] = {}
    alarm_agg: dict[int, dict] = {}
    for chunk in _chunked(unique_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f"SELECT asset_id, COUNT(*) c FROM inspections"
                f" WHERE result='Fail' AND asset_id IN ({placeholders}) GROUP BY asset_id", chunk)):
            failed_inspections[r['asset_id']] = int(r['c'])
        for r in _rows(conn.execute(
                f'''SELECT w.asset_id, COUNT(*) c FROM work_order_sla s
                      JOIN work_orders w ON w.id=s.work_order_id
                     WHERE (s.response_status='Breached' OR s.resolution_status='Breached')
                       AND w.asset_id IN ({placeholders}) GROUP BY w.asset_id''', chunk)):
            sla_breaches[r['asset_id']] = int(r['c'])
        for r in _rows(conn.execute(
                f"""SELECT asset_id, COUNT(*) total,
                           SUM(CASE WHEN severity='Critical' THEN 1 ELSE 0 END) critical
                      FROM operational_alarms
                     WHERE status IN ('Open','Acknowledged') AND asset_id IN ({placeholders})
                     GROUP BY asset_id""", chunk)):
            alarm_agg[r['asset_id']] = {'total': int(r['total']), 'critical': int(r['critical'] or 0)}

    out: dict[int, dict] = {}
    for asset_id in unique_ids:
        row = base.get(asset_id)
        if row is None:
            continue
        condition = row.get('condition')
        criticality = row.get('criticality')
        status = row.get('status')
        work = work_agg.get(asset_id, {'high': 0, 'overdue': 0})
        alarms = alarm_agg.get(asset_id, {'total': 0, 'critical': 0})
        penalties = {
            'condition': condition_penalty_map.get(condition, 15),
            'criticality': criticality_penalty_map.get(criticality, 3),
            'status': 0 if status in ('Operating', 'Standby') else (
                10 if status in ('Under Maintenance', 'Restricted') else 25),
            'priority_work': min(25, work['high'] * 7),
            'overdue_work': min(20, work['overdue'] * 5),
            'failed_inspections': min(16, int(failed_inspections.get(asset_id, 0)) * 8),
            'sla_breaches': min(10, int(sla_breaches.get(asset_id, 0)) * 5),
            'operational_alarms': min(18, alarms['total'] * 5 + alarms['critical'] * 5),
        }
        score = max(0, min(100, 100 - sum(penalties.values())))
        band = ('Healthy' if score >= 85 else 'Monitor' if score >= 70
                else 'Warning' if score >= 50 else 'Critical')
        out[asset_id] = {
            'asset_id': asset_id, 'score': round(score, 1), 'risk_band': band,
            'factors': penalties,
            'open_priority_work': work['high'], 'overdue_work': work['overdue'],
            'failed_inspections': int(failed_inspections.get(asset_id, 0)),
            'sla_breaches': int(sla_breaches.get(asset_id, 0)),
            'condition': condition, 'criticality': criticality, 'status': status,
        }
    return out


# --------------------------------------------------------------------------- #
# KPI snapshot materialization
# --------------------------------------------------------------------------- #

def _snapshot_scope_key(f: ExecutiveFilters) -> str:
    return '|'.join([
        f"site={f.site_id}", f"region={f.region}",
        f"class={f.asset_type_id}", f"crit={f.criticality}"])


def _snapshot_window_key(f: ExecutiveFilters) -> str:
    w = f.window()
    return f"{w['period_start']}..{w['period_end']}#{w['period_days']}"


def source_watermark(conn) -> Optional[str]:
    """Latest mutation timestamp across every table feeding executive KPIs.

    Used for snapshot invalidation: a stored snapshot whose calculated_at is
    older than this watermark was computed before at least one relevant
    mutation and must be recomputed. audit_logs covers every audited business
    mutation; the remaining columns cover machine-driven paths that bypass the
    audit trail (telemetry ingestion, alarm state transitions).
    """
    return conn.execute(
        '''SELECT MAX(ts) FROM (
             SELECT MAX(created_at) ts FROM audit_logs
             UNION ALL SELECT MAX(updated_at) FROM asset_outages
             UNION ALL SELECT MAX(start_at) FROM asset_outages
             UNION ALL SELECT MAX(opened_at) FROM operational_alarms
             UNION ALL SELECT MAX(last_seen_at) FROM operational_alarms
             UNION ALL SELECT MAX(captured_at) FROM telemetry_readings
             UNION ALL SELECT MAX(ingested_at) FROM telemetry_readings
             UNION ALL SELECT MAX(updated_at) FROM telemetry_channels
             UNION ALL SELECT MAX(reserved_at) FROM inventory_reservations
             UNION ALL SELECT MAX(issued_at) FROM work_order_materials
             UNION ALL SELECT MAX(created_at) FROM purchase_requisitions
             UNION ALL SELECT MAX(approved_at) FROM purchase_requisitions
             UNION ALL SELECT MAX(actual_receipt) FROM purchase_orders
             UNION ALL SELECT MAX(created_at) FROM technician_absences
             UNION ALL SELECT MAX(created_at) FROM inventory_transactions
             UNION ALL SELECT MAX(posted_at) FROM maintenance_cost_ledger
             UNION ALL SELECT MAX(created_at) FROM safety_incidents
             UNION ALL SELECT MAX(occurred_at) FROM safety_incidents
           )''').fetchone()[0]


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def read_snapshot(conn, f: ExecutiveFilters) -> Optional[dict]:
    """Return a still-valid cached snapshot payload, or None when unusable."""
    row = conn.execute(
        'SELECT payload_json, calculated_at, source_latest_at FROM kpi_snapshot'
        ' WHERE scope_key=? AND window_key=?',
        (_snapshot_scope_key(f), _snapshot_window_key(f))).fetchone()
    if row is None:
        return None
    import json as _json
    try:
        payload = _json.loads(row['payload_json'])
    except (TypeError, ValueError):
        return None
    calculated_at = _parse_ts(row['calculated_at'])
    if calculated_at is None:
        return None
    age_seconds = max(0.0, (datetime.now() - calculated_at).total_seconds())
    if age_seconds > SNAPSHOT_TTL_MINUTES * 60:
        return None  # TTL bounds staleness from unwatermarked mutations
    watermark_now = _parse_ts(source_watermark(conn))
    watermark_then = _parse_ts(row['source_latest_at']) or calculated_at
    # Strict comparison: any tracked mutation strictly after the stored
    # watermark forces recomputation. Sub-second mutation races are bounded by
    # the TTL above rather than pretending second-level precision.
    if watermark_now and watermark_now > watermark_then:
        return None
    payload.setdefault('snapshot', {})
    payload['snapshot'] = {
        'served_from_cache': True,
        'calculated_at': row['calculated_at'],
        'age_seconds': round(age_seconds, 1),
        'ttl_minutes': SNAPSHOT_TTL_MINUTES,
    }
    return payload


def write_snapshot(conn, f: ExecutiveFilters, payload: dict) -> bool:
    """Atomically upsert one scoped snapshot; never partially visible.

    Returns False (without raising) on storage problems so callers fall back to
    live computation — e.g. legacy databases where the table has not migrated.
    """
    import json as _json
    watermark = source_watermark(conn)
    try:
        conn.execute(
            '''INSERT INTO kpi_snapshot(scope_key,window_key,payload_json,
                                        source_latest_at,calculated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(scope_key,window_key) DO UPDATE SET
                 payload_json=excluded.payload_json,
                 source_latest_at=excluded.source_latest_at,
                 calculated_at=excluded.calculated_at''',
            (_snapshot_scope_key(f), _snapshot_window_key(f),
             _json.dumps(payload, ensure_ascii=False, default=str),
             watermark, datetime.now().isoformat(timespec='seconds')))
        return True
    except Exception:
        return False


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
            'SELECT o.*, s.customer_count FROM asset_outages o'
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
        if f.region:
            # Region scoping rides on the sites join already present in o_sql.
            o_sql += ' AND s.region=?'
            o_args.append(f.region)
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
        'SELECT COALESCE(SUM(customer_count),0) FROM sites WHERE id IN (%s)'
        % (','.join('?' * len(site_ids)) or 'NULL'),
        tuple(site_ids),
    ).fetchone()[0] or 0) if site_ids else 0
    configured = customers_total > 0

    saidi_minutes = saifi = caidi_minutes = None
    if configured:
        customer_minutes = 0.0
        interruptions = 0
        for x in current['outages']:
            served = int(x.get('customer_count') or 0)
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

    def _week_bucket(ts_value) -> str:
        try:
            d = datetime.fromisoformat(str(ts_value)[:19]).date()
        except (TypeError, ValueError):
            d = date.today()
        return (d - timedelta(days=d.weekday())).isoformat()

    weekly: dict[str, dict] = {}
    for x in current['outages']:
        b = weekly.setdefault(_week_bucket(x['start_at']),
                              {'period': None, 'outages': 0, 'downtime_hours': 0.0,
                               'mttr_hours': 0.0})
        b['outages'] += 1
        b['downtime_hours'] = round(b['downtime_hours'] + x['overlap_hours'], 2)
    trend = []
    for key in sorted(weekly):
        b = weekly[key]
        b['period'] = key
        b['mttr_hours'] = round(b['downtime_hours'] / b['outages'], 2) if b['outages'] else 0.0
        trend.append(b)

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
        'customer_count_total': customers_total,
        'trend': trend,
    }


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #

def compute_asset_kpis(conn, f: ExecutiveFilters) -> dict:
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

    scoped_ids = [r['id'] for r in _rows(conn.execute(
        'SELECT a.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1' + scope_sql, scope_args))]
    identity: dict[int, dict] = {}
    for chunk in _chunked(scoped_ids):
        for r in _rows(conn.execute(
                'SELECT id,asset_no,name FROM assets WHERE id IN (%s)'
                % ','.join('?' * len(chunk)), chunk)):
            identity[r['id']] = dict(r)
    health_rows = []
    for asset_id, h in _asset_health_map(conn, scoped_ids).items():
        ident = identity.get(asset_id, {})
        health_rows.append({**ident, **h})
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

    # Overdue aging: where is backlog stuck longest? (decision: triage order)
    today_d = date.today()
    age_buckets = {'1-7d': 0, '8-30d': 0, '31-90d': 0, '90d+': 0}
    for r in _rows(conn.execute(
            'SELECT w.target_finish' + base +
            f' AND {_OPEN_WO} AND w.target_finish IS NOT NULL AND w.target_finish<?',
            scope_args + [w['period_end']])):
        try:
            overdue_days = (today_d - date.fromisoformat(str(r['target_finish'])[:10])).days
        except (TypeError, ValueError):
            continue
        if overdue_days <= 0:
            continue
        if overdue_days <= 7:
            age_buckets['1-7d'] += 1
        elif overdue_days <= 30:
            age_buckets['8-30d'] += 1
        elif overdue_days <= 90:
            age_buckets['31-90d'] += 1
        else:
            age_buckets['90d+'] += 1

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
        'overdue_by_age_bucket': age_buckets,
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
    """Inventory/procurement KPIs honoring site/region scope where real
    dimensions exist (warehouses carry site; requisitions carry site).

    Parts-blocked work is reservation-engine-exact: a requirement is blocked
    only when remaining_required > reserved_for_work + free_stock, mirroring
    _work_order_parts_readiness (required minus issued, reservations with
    status Reserved/Partially Issued, and unreserved free stock).
    Purchase-order metrics are organization-wide: purchase_orders carry no
    site dimension in the schema.
    """
    w = f.window()
    inv_scope_sql, inv_scope_args = ('', [])
    if f.site_id is not None:
        inv_scope_sql += ' AND s.id=?'
        inv_scope_args.append(f.site_id)
    if f.region:
        inv_scope_sql += ' AND s.region=?'
        inv_scope_args.append(f.region)

    stockouts = _rows(conn.execute(
        'SELECT i.id, i.item_no, i.name, i.current_stock, i.reserved_stock, i.reorder_point,'
        ' w.name warehouse_name FROM inventory_items i'
        ' JOIN warehouses w ON w.id=i.warehouse_id LEFT JOIN sites s ON s.id=w.site_id'
        ' WHERE i.current_stock-i.reserved_stock<=0' + inv_scope_sql +
        ' ORDER BY i.item_no LIMIT 50', inv_scope_args))
    reserved_value = conn.execute(
        'SELECT COALESCE(SUM(i.reserved_stock*i.unit_price),0) FROM inventory_items i'
        ' JOIN warehouses wh ON wh.id=i.warehouse_id LEFT JOIN sites s ON s.id=wh.site_id'
        ' WHERE 1=1' + inv_scope_sql, inv_scope_args).fetchone()[0] or 0
    ati_value = conn.execute(
        'SELECT COALESCE(SUM((i.current_stock-i.reserved_stock)*i.unit_price),0)'
        ' FROM inventory_items i JOIN warehouses wh ON wh.id=i.warehouse_id'
        ' LEFT JOIN sites s ON s.id=wh.site_id WHERE 1=1' + inv_scope_sql,
        inv_scope_args).fetchone()[0] or 0

    def _blocked(extra_priority: str = '') -> int:
        sql = (
            'SELECT COUNT(DISTINCT r.work_order_id) FROM work_order_requirements r'
            ' JOIN inventory_items i ON i.id=r.inventory_item_id'
            ' JOIN work_orders w ON w.id=r.work_order_id'
            ' LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id'
            ' LEFT JOIN (SELECT work_order_id ao, inventory_item_id ai, SUM(quantity) issued'
            '   FROM work_order_materials GROUP BY work_order_id,inventory_item_id) m'
            '   ON m.ao=r.work_order_id AND m.ai=r.inventory_item_id'
            ' LEFT JOIN (SELECT work_order_id ro, inventory_item_id ri,'
            '   SUM(quantity-issued_quantity) reserved FROM inventory_reservations'
            "   WHERE status IN ('Reserved','Partially Issued')"
            '   GROUP BY work_order_id,inventory_item_id) rv'
            '   ON rv.ro=r.work_order_id AND rv.ri=r.inventory_item_id'
            " WHERE r.status<>'Cancelled' AND " + _OPEN_WO +
            ' AND (r.quantity-COALESCE(m.issued,0)) >'
            ' (COALESCE(rv.reserved,0)'
            ' + CASE WHEN i.current_stock-i.reserved_stock>0'
            '   THEN i.current_stock-i.reserved_stock ELSE 0 END)' + extra_priority)
        args: list = []
        if f.site_id is not None:
            sql += ' AND s.id=?'
            args.append(f.site_id)
        if f.region:
            sql += ' AND s.region=?'
            args.append(f.region)
        return int(conn.execute(sql, args).fetchone()[0])

    blocked = _blocked()
    blocked_high_risk = _blocked(" AND w.priority IN ('Emergency','Critical','High')")

    pr_scope_sql, pr_scope_args = '', []
    if f.site_id is not None:
        pr_scope_sql += ' LEFT JOIN sites ps ON ps.id=pr.site_id WHERE 1=1 AND pr.site_id=?'
        pr_scope_args.append(f.site_id)
    elif f.region:
        pr_scope_sql += ' LEFT JOIN sites ps ON ps.id=pr.site_id WHERE 1=1 AND ps.region=?'
        pr_scope_args.append(f.region)
    overdue_prs = _rows(conn.execute(
        'SELECT pr.id, pr.pr_no, pr.title, pr.created_at FROM purchase_requisitions pr'
        + (pr_scope_sql if pr_scope_args else ' WHERE 1=1') +
        " AND pr.status='Submitted' AND pr.created_at<?"
        ' ORDER BY pr.created_at LIMIT 20',
        pr_scope_args + [(datetime.now() - timedelta(days=OVERDUE_REQUISITION_DAYS)).isoformat(timespec='seconds')]))
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
    scope_sql, scope_args = _wo_scope(f)
    rows_ = _rows(conn.execute(
        'SELECT w.id, w.wo_no, w.title, w.priority, w.work_type, w.status, w.target_finish,'
        ' w.safety_requirements, a.id asset_id, a.asset_no, a.criticality, a.condition,' 
        ' s.name site_name' +
        ' FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id'
        ' LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id'
        ' WHERE ' + _OPEN_WO + scope_sql, scope_args))
    wo_asset_ids = [r['asset_id'] for r in rows_ if r['asset_id']]
    health_map = _asset_health_map(conn, wo_asset_ids)
    wo_ids = [r['id'] for r in rows_]

    # Batched lookups replace four per-work-order queries (alarms, outages,
    # SLA state, parts shortages); identical inputs to the per-row form.
    alarm_counts: dict[int, int] = {}
    open_outage_assets: set[int] = set()
    for chunk in _chunked(wo_asset_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f"""SELECT asset_id, COUNT(*) c FROM operational_alarms
                     WHERE status IN ('Open','Acknowledged') AND asset_id IN ({placeholders})
                     GROUP BY asset_id""", chunk)):
            alarm_counts[r['asset_id']] = int(r['c'])
        for r in _rows(conn.execute(
                f"SELECT DISTINCT asset_id FROM asset_outages"
                f" WHERE status='Open' AND asset_id IN ({placeholders})", chunk)):
            open_outage_assets.add(int(r['asset_id']))
    sla_state: dict[int, dict] = {}
    for chunk in _chunked(wo_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f'''SELECT work_order_id, response_status, resolution_status
                      FROM work_order_sla WHERE work_order_id IN ({placeholders})''', chunk)):
            sla_state[r['work_order_id']] = dict(r)
    blocked_wo_ids: set[int] = set()
    for chunk in _chunked(wo_ids):
        placeholders = ','.join('?' * len(chunk))
        for r in _rows(conn.execute(
                f'''SELECT DISTINCT r.work_order_id wid FROM work_order_requirements r
                      JOIN inventory_items i ON i.id=r.inventory_item_id
                      LEFT JOIN (SELECT work_order_id ao, inventory_item_id ai,
                        SUM(quantity) issued FROM work_order_materials
                        GROUP BY work_order_id,inventory_item_id) m
                        ON m.ao=r.work_order_id AND m.ai=r.inventory_item_id
                      LEFT JOIN (SELECT work_order_id ro, inventory_item_id ri,
                        SUM(quantity-issued_quantity) reserved FROM inventory_reservations
                        WHERE status IN ('Reserved','Partially Issued')
                        GROUP BY work_order_id,inventory_item_id) rv
                        ON rv.ro=r.work_order_id AND rv.ri=r.inventory_item_id
                     WHERE r.status<>'Cancelled' AND r.work_order_id IN ({placeholders})
                       AND (r.quantity-COALESCE(m.issued,0)) >
                       (COALESCE(rv.reserved,0)
                        + CASE WHEN i.current_stock-i.reserved_stock>0
                          THEN i.current_stock-i.reserved_stock ELSE 0 END)''', chunk)):
            blocked_wo_ids.add(int(r['wid']))

    today_s = date.today().isoformat()
    scored: list[dict] = []
    critical_count = 0
    blocked_high_risk = 0
    for wo in rows_:
        wo['overdue_days'] = 0
        if wo.get('target_finish') and str(wo['target_finish'])[:10] < today_s:
            delta = date.today() - date.fromisoformat(str(wo['target_finish'])[:10])
            wo['overdue_days'] = delta.days
        wo['active_alarms'] = alarm_counts.get(wo['asset_id'], 0) if wo['asset_id'] else 0
        wo['open_outage'] = bool(wo['asset_id'] and wo['asset_id'] in open_outage_assets)
        wo['safety_relevant'] = bool(str(wo.get('safety_requirements') or '').strip()) or \
            wo.get('priority') == 'Emergency'
        wo['parts_blocked'] = wo['id'] in blocked_wo_ids
        sla = sla_state.get(wo['id'], {})
        wo['response_breached'] = sla.get('response_status') == 'Breached'
        wo['resolution_breached'] = sla.get('resolution_status') == 'Breached'
        health = health_map.get(wo['asset_id']) if wo['asset_id'] else None
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
# Maintenance cost roll-ups
# --------------------------------------------------------------------------- #

def compute_cost_kpis(conn, f: ExecutiveFilters) -> dict:
    """Maintenance cost for the window vs the previous window.

    Costs are attributed through maintenance_cost_ledger -> asset scope.
    Ledger entries without an asset belong to organization-wide totals and are
    excluded from site/region/class/criticality-scoped views (schema reality,
    stated explicitly rather than silently misattributed).
    """
    w = f.window()
    scope_sql, scope_args = _asset_scope(f)
    base_join = (
        ' FROM maintenance_cost_ledger c'
        ' LEFT JOIN assets a ON a.id=c.asset_id'
        ' LEFT JOIN locations l ON l.id=a.location_id'
        ' LEFT JOIN sites s ON s.id=l.site_id')

    def _window_total(win_start: str, win_end: str) -> float:
        return float(conn.execute(
            'SELECT COALESCE(SUM(c.amount),0)' + base_join +
            ' WHERE 1=1' + scope_sql +
            ' AND c.posted_at>=? AND c.posted_at<?',
            scope_args + [win_start + 'T00:00:00', win_end + 'T23:59:59']).fetchone()[0] or 0)

    current_total = round(_window_total(w['period_start'], w['period_end']), 2)
    previous_total = round(_window_total(w['previous_start'], w['previous_end']), 2)

    by_site = _rows(conn.execute(
        'SELECT s.id site_id, s.site_code, s.name site_name,'
        ' COALESCE(SUM(c.amount),0) amount' + base_join +
        ' WHERE 1=1' + scope_sql +
        ' AND c.posted_at>=? AND c.posted_at<? AND s.id IS NOT NULL'
        ' GROUP BY s.id ORDER BY amount DESC LIMIT 12',
        scope_args + [w['period_start'] + 'T00:00:00', w['period_end'] + 'T23:59:59']))
    by_criticality = _rows(conn.execute(
        "SELECT a.criticality band, COALESCE(SUM(c.amount),0) amount, COUNT(*) entries" +
        base_join + ' WHERE 1=1' + scope_sql +
        ' AND c.posted_at>=? AND c.posted_at<? AND a.id IS NOT NULL'
        ' GROUP BY a.criticality ORDER BY amount DESC',
        scope_args + [w['period_start'] + 'T00:00:00', w['period_end'] + 'T23:59:59']))
    top_assets = _rows(conn.execute(
        'SELECT a.id asset_id, a.asset_no, a.name asset_name, a.criticality,'
        ' COALESCE(SUM(c.amount),0) amount' + base_join +
        ' WHERE 1=1' + scope_sql +
        ' AND c.posted_at>=? AND c.posted_at<? AND a.id IS NOT NULL'
        ' GROUP BY a.id ORDER BY amount DESC LIMIT 10',
        scope_args + [w['period_start'] + 'T00:00:00', w['period_end'] + 'T23:59:59']))

    return {
        'maintenance_cost_window': current_total,
        'maintenance_cost_previous': previous_total,
        'cost_delta': round(current_total - previous_total, 2),
        'by_site': [{**r, 'amount': round(float(r['amount']), 2)} for r in by_site],
        'by_criticality': [
            {**r, 'band': r['band'], 'amount': round(float(r['amount']), 2)}
            for r in by_criticality],
        'top_cost_assets': top_assets,
        'attribution_note': ('entries without an asset are included in window '
                             'totals only when unscoped'),
    }


# --------------------------------------------------------------------------- #
# HSE / incident intelligence (real safety_incidents data only)
# --------------------------------------------------------------------------- #

HSE_HIGH_RISK_SCORE = 12  # matches the domain's own 'High HSE risk' escalation


def _risk_band(risk_score) -> str:
    """Existing EUAS risk-band mapping (see legacy analytics endpoint)."""
    try:
        score = float(risk_score or 0)
    except (TypeError, ValueError):
        return 'Low'
    if score >= 15:
        return 'Extreme'
    if score >= 10:
        return 'High'
    if score >= 5:
        return 'Medium'
    return 'Low'


def compute_hse_kpis(conn, f: ExecutiveFilters) -> dict:
    """Safety/incident KPIs from safety_incidents only.

    Metrics that would require data EUAS does not store (investigation due
    dates, structured corrective-action lifecycle, exposure hours) are reported
    explicitly as unavailable instead of approximated. Contributor rankings are
    correlations drawn from actual incident records, not inferred causality.
    """
    w = f.window()
    scope_sql, scope_args = _asset_scope(f, alias_a='a')
    base = (
        ' FROM safety_incidents h'
        ' LEFT JOIN sites s ON s.id=h.site_id'
        ' LEFT JOIN locations l ON l.id=h.location_id'
        ' LEFT JOIN assets a ON a.id=h.asset_id'
        ' WHERE 1=1' + scope_sql)

    open_where = " AND h.status NOT IN ('Closed','Cancelled')"

    def _count(extra_where: str, extra_args: list | None = None) -> int:
        return int(conn.execute(
            'SELECT COUNT(*)' + base + extra_where,
            list(scope_args) + (extra_args or [])).fetchone()[0])

    open_incidents = _count(open_where)
    high_risk_open = _count(open_where + ' AND h.risk_score>=?',
                            [HSE_HIGH_RISK_SCORE])

    cur_args = [w['period_start'] + 'T00:00:00', w['period_end'] + 'T23:59:59']
    prev_args = [w['previous_start'] + 'T00:00:00', w['previous_end'] + 'T23:59:59']
    incidents_current = _count(' AND h.created_at>=? AND h.created_at<?', cur_args)
    incidents_previous = _count(' AND h.created_at>=? AND h.created_at<?', prev_args)
    high_risk_current = _count(
        ' AND h.risk_score>=? AND h.created_at>=? AND h.created_at<?',
        [HSE_HIGH_RISK_SCORE] + cur_args)
    high_risk_previous = _count(
        ' AND h.risk_score>=? AND h.created_at>=? AND h.created_at<?',
        [HSE_HIGH_RISK_SCORE] + prev_args)

    severity_distribution = {str(n): 0 for n in range(1, 6)}
    for r in _rows(conn.execute(
            'SELECT h.severity, COUNT(*) c' + base +
            ' AND h.created_at>=? AND h.created_at<? GROUP BY h.severity',
            scope_args + cur_args)):
        severity_distribution[str(int(r['severity']))] = int(r['c'])
    risk_bands: dict[str, int] = {}
    for r in _rows(conn.execute(
            'SELECT h.risk_score, COUNT(*) c' + base + ' GROUP BY h.risk_score',
            scope_args)):
        band = _risk_band(r['risk_score'])
        risk_bands[band] = risk_bands.get(band, 0) + int(r['c'])

    # Weekly trend inside the current window.
    trend_rows = _rows(conn.execute(
        'SELECT h.created_at, h.risk_score' + base +
        ' AND h.created_at>=? AND h.created_at<?',
        scope_args + cur_args))
    weekly: dict[str, dict] = {}
    today_d = date.today()
    for r in trend_rows:
        try:
            d = datetime.fromisoformat(str(r['created_at'])[:19]).date()
        except (TypeError, ValueError):
            continue
        bucket = (d - timedelta(days=d.weekday())).isoformat()
        b = weekly.setdefault(bucket, {'period': None, 'incidents': 0, 'high_risk': 0})
        b['incidents'] += 1
        if float(r['risk_score'] or 0) >= HSE_HIGH_RISK_SCORE:
            b['high_risk'] += 1
    trend = []
    for key in sorted(weekly):
        b = weekly[key]
        b['period'] = key
        trend.append(b)

    # Days since last high-risk incident (occurrence date preferred).
    last_high = conn.execute(
        'SELECT MAX(COALESCE(h.occurred_at,h.created_at))' + base +
        ' AND h.risk_score>=?', scope_args + [HSE_HIGH_RISK_SCORE]).fetchone()[0]
    days_since_high = None
    if last_high:
        try:
            days_since_high = (today_d - datetime.fromisoformat(str(last_high)[:19]).date()).days
        except (TypeError, ValueError):
            days_since_high = None

    def _contributors(group_expr: str, label_field: str, id_field: str | None) -> list[dict]:
        rows_ = _rows(conn.execute(
            f'SELECT {group_expr} group_value, COUNT(*) c,'
            f" SUM(CASE WHEN h.risk_score>={HSE_HIGH_RISK_SCORE} THEN 1 ELSE 0 END) high,"
            f' MIN(h.id) example_id, MIN(h.incident_no) example_no' + base +
            ' AND h.created_at>=? AND h.created_at<?' +
            (f' AND {id_field} IS NOT NULL' if id_field else '') +
            ' GROUP BY ' + group_expr + ' ORDER BY c DESC LIMIT 8',
            scope_args + cur_args))
        return [{'label': r['group_value'], 'incidents': int(r['c']),
                 'high_risk': int(r['high'] or 0),
                 'example_incident_id': r['example_id'],
                 'example_incident_no': r['example_no']} for r in rows_]

    by_site = _contributors('s.name', 's.name', 'h.site_id')
    by_type = _contributors('h.incident_type', 'h.incident_type', 'h.incident_type')
    by_asset = _contributors('a.asset_no', 'a.asset_no', 'h.asset_id')

    # Repeat locations/assets: more than one incident in the trailing 90 days.
    cutoff90 = (datetime.now() - timedelta(days=90)).isoformat(timespec='seconds')
    repeat_locations = _rows(conn.execute(
        'SELECT l.id location_id, l.name location_name, COUNT(*) incidents' + base +
        ' AND h.location_id IS NOT NULL AND h.created_at>=?' +
        ' GROUP BY l.id HAVING COUNT(*)>=2 ORDER BY incidents DESC LIMIT 10',
        scope_args + [cutoff90]))
    repeat_assets = _rows(conn.execute(
        'SELECT a.id asset_id, a.asset_no, a.name asset_name, COUNT(*) incidents' + base +
        ' AND h.asset_id IS NOT NULL AND h.created_at>=?' +
        ' GROUP BY a.id HAVING COUNT(*)>=2 ORDER BY incidents DESC LIMIT 10',
        scope_args + [cutoff90]))

    # Deterministic recommendations (KPI -> action): every entry derives from
    # stored records and carries the drill identifier for its subject.
    recommendations: list[dict] = []
    seen_subjects: set[tuple] = set()

    def _push(rec: dict) -> None:
        subject = (rec['kind'], rec.get('incident_id') or rec.get('asset_id')
                   or rec.get('location_id') or rec.get('label'))
        if subject in seen_subjects:
            return
        seen_subjects.add(subject)
        recommendations.append(rec)

    high_risk_open_rows = _rows(conn.execute(
        'SELECT h.id incident_id, h.incident_no, h.risk_score' + base +
        open_where + ' AND h.risk_score>=? ORDER BY h.risk_score DESC',
        scope_args + [HSE_HIGH_RISK_SCORE]))
    for r in high_risk_open_rows:
        _push({'kind': 'corrective_action_needed', 'severity': 'Critical',
               'incident_id': int(r['incident_id']), 'incident_no': r['incident_no'],
               'risk_score': float(r['risk_score'] or 0),
               'note': 'High-risk incident still open - corrective action required'})
    for r in repeat_assets:
        _push({'kind': 'repeat_incident', 'severity': 'Warning',
               'asset_id': int(r['asset_id']), 'asset_no': r['asset_no'],
               'incidents': int(r['incidents']),
               'note': f"{int(r['incidents'])} incidents on this asset within 90 days"})
    for r in repeat_locations:
        _push({'kind': 'repeat_incident', 'severity': 'Warning',
               'location_id': int(r['location_id']), 'label': r['location_name'],
               'incidents': int(r['incidents']),
               'note': f"{int(r['incidents'])} incidents at this location within 90 days"})
    if high_risk_current >= 2:
        _push({'kind': 'risk_indicator', 'severity': 'Warning',
               'label': f'{high_risk_current} high-risk incidents in the current window',
               'note': 'Clustered high-risk incidents indicate escalating exposure'})

    return {
        'open_incidents': open_incidents,
        'high_risk_open': high_risk_open,
        'high_risk_definition': f'risk_score >= {HSE_HIGH_RISK_SCORE} (domain escalation threshold)',
        'incidents_current': incidents_current,
        'incidents_previous': incidents_previous,
        'incidents_delta': incidents_current - incidents_previous,
        'incidents_delta_pct': (
            round(100 * (incidents_current - incidents_previous) / incidents_previous, 1)
            if incidents_previous else None),
        'high_risk_current': high_risk_current,
        'high_risk_previous': high_risk_previous,
        'high_risk_delta': high_risk_current - high_risk_previous,
        'days_since_last_high_risk': days_since_high,
        'severity_distribution_window': severity_distribution,
        'risk_band_distribution': risk_bands,
        'trend': trend,
        'contributors_by_site': by_site,
        'contributors_by_type': by_type,
        'contributors_by_asset': by_asset,
        'recommendations': recommendations,
        'repeat_locations_90d': [
            {**r, 'incidents': int(r['incidents'])} for r in repeat_locations],
        'repeat_assets_90d': [
            {**r, 'incidents': int(r['incidents'])} for r in repeat_assets],
        'correlation_note': ('contributor rankings correlate incident records; '
                             'causality requires completed investigation data'),
        'unavailable': {
            'overdue_investigations':
                'no investigation due-date field exists on safety_incidents',
            'corrective_action_closure_rate':
                'corrective actions are stored as free text without lifecycle state',
            'trir_ltifr_exposure_rates':
                'no exposure-hours denominator data is stored',
        },
    }


# --------------------------------------------------------------------------- #
# Parts-shortage drill-down (KPI -> action)
# --------------------------------------------------------------------------- #

def compute_parts_shortages(conn, f: ExecutiveFilters, limit: int = 50) -> dict:
    """Exact per-line material shortages blocking open work.

    Same reservation-engine math as the parts-blocked KPI and
    _work_order_parts_readiness, exposed at line level so planners can act
    (reserve, expedite or re-plan) without opening each work order.
    Honors site/region scoping through the work order's location.
    """
    sql = (
        '''SELECT r.work_order_id wo_id, w.wo_no, w.priority, a.asset_no,
                  i.id item_id, i.item_no, i.name item_name, i.unit,
                  r.quantity required_qty, COALESCE(m.issued,0) issued_qty,
                  COALESCE(rv.reserved,0) reserved_for_work,
                  CASE WHEN i.current_stock-i.reserved_stock>0
                       THEN i.current_stock-i.reserved_stock ELSE 0 END free_stock,
                  s.name site_name
             FROM work_order_requirements r
             JOIN work_orders w ON w.id=r.work_order_id
             JOIN inventory_items i ON i.id=r.inventory_item_id
             LEFT JOIN assets a ON a.id=w.asset_id
             LEFT JOIN locations l ON l.id=w.location_id
             LEFT JOIN sites st ON st.id=l.site_id
             LEFT JOIN sites s ON s.id=st.id
             LEFT JOIN (SELECT work_order_id ao, inventory_item_id ai, SUM(quantity) issued
                          FROM work_order_materials GROUP BY work_order_id,inventory_item_id) m
               ON m.ao=r.work_order_id AND m.ai=r.inventory_item_id
             LEFT JOIN (SELECT work_order_id ro, inventory_item_id ri,
               SUM(quantity-issued_quantity) reserved FROM inventory_reservations
               WHERE status IN ('Reserved','Partially Issued')
               GROUP BY work_order_id,inventory_item_id) rv
               ON rv.ro=r.work_order_id AND rv.ri=r.inventory_item_id''')
    where = (" WHERE r.status<>'Cancelled' AND " + _OPEN_WO +
             ' AND (r.quantity-COALESCE(m.issued,0)) >'
             ' (COALESCE(rv.reserved,0)'
             ' + CASE WHEN i.current_stock-i.reserved_stock>0'
             '   THEN i.current_stock-i.reserved_stock ELSE 0 END)')
    args: list = []
    if f.site_id is not None:
        where += ' AND s.id=?'
        args.append(f.site_id)
    if f.region:
        where += ' AND s.region=?'
        args.append(f.region)
    rows_ = _rows(conn.execute(sql + where + ' ORDER BY w.priority DESC, i.item_no LIMIT ?',
                               args + [limit]))
    lines = [{
        'wo_id': r['wo_id'], 'wo_no': r['wo_no'], 'priority': r['priority'],
        'asset_no': r['asset_no'], 'item_no': r['item_no'], 'item_name': r['item_name'],
        'unit': r['unit'],
        'required_qty': float(r['required_qty'] or 0),
        'issued_qty': float(r['issued_qty'] or 0),
        'reserved_for_work': float(r['reserved_for_work'] or 0),
        'free_stock': float(r['free_stock'] or 0),
        'outstanding_short': round(float(r['required_qty'] or 0)
                                   - float(r['issued_qty'] or 0)
                                   - float(r['reserved_for_work'] or 0)
                                   - float(r['free_stock'] or 0), 3),
        'site_name': r['site_name'],
    } for r in rows_]
    return {
        'summary': {
            'blocked_work_orders': len({x['wo_id'] for x in lines}),
            'short_lines': len(lines),
            'high_risk_lines': sum(1 for x in lines
                                   if x['priority'] in ('Emergency', 'Critical', 'High')),
        },
        'lines': lines,
    }


def snapshot_export_rows(snapshot: dict) -> list[list]:
    """Deterministic flat rows for CSV export of one executive snapshot.

    Reuses an already-computed snapshot payload; export never recalculates.
    Values are scalars only — nested contributor/trend structures are out of
    scope for the tabular contract and remain available through the JSON API.
    """
    r = snapshot['reliability']
    m = snapshot['maintenance']
    a = snapshot['assets']
    c = snapshot['condition']
    ip = snapshot['inventory_procurement']
    wf = snapshot['workforce']
    cost = snapshot['costs']
    hse = snapshot.get('hse', {})
    fresh = snapshot['freshness']

    def row(family, metric, value, previous='', delta=''):
        return [family, metric, value, previous, delta]

    rows = [
        row('meta', 'calculated_at', fresh['calculated_at']),
        row('meta', 'freshness_state', fresh['state']),
        row('meta', 'latest_source_timestamp', fresh.get('latest_source_timestamp') or ''),
        row('reliability', 'availability_pct', r['availability_pct'],
            r['availability_previous_pct'], ''),
        row('reliability', 'saidi_minutes', r['saidi_minutes'] if r['saidi_minutes'] is not None else 'unavailable'),
        row('reliability', 'saifi', r['saifi'] if r['saifi'] is not None else 'unavailable'),
        row('reliability', 'caidi_minutes', r['caidi_minutes'] if r['caidi_minutes'] is not None else 'unavailable'),
        row('reliability', 'unplanned_outages', r['unplanned_outages']),
        row('reliability', 'planned_outages', r['planned_outages']),
        row('maintenance', 'open_wo', m['open_wo']),
        row('maintenance', 'overdue_wo', m['overdue_wo']),
        row('maintenance', 'emergency_wo', m['emergency_wo']),
        row('maintenance', 'pm_compliance_pct', m['pm_compliance_pct']),
        row('maintenance', 'schedule_compliance_pct', m['schedule_compliance_pct']),
        row('maintenance', 'backlog_hours', m['backlog_hours']),
        row('maintenance', 'mtbf_hours', m['mtbf_hours'] if m['mtbf_hours'] is not None else 'no failures'),
        row('maintenance', 'mttr_hours', m['mttr_hours'] if m['mttr_hours'] is not None else 'no failures'),
        row('assets', 'total', a['total']),
        row('assets', 'down', a['down']),
        row('assets', 'critical_down', a['critical_down']),
        row('condition', 'active_alarms', c['active_alarms']),
        row('condition', 'critical_active_alarms', c['critical_active_alarms']),
        row('inventory', 'stockout_items', ip['stockout_items']),
        row('inventory', 'work_blocked_by_parts', ip['work_blocked_by_parts']),
        row('procurement', 'overdue_purchase_orders', ip['overdue_purchase_orders']),
        row('workforce', 'technicians_available', wf['technicians_available']),
        row('workforce', 'sla_breached_open', wf['sla_breached_open']),
        row('costs', 'maintenance_cost_window', cost['maintenance_cost_window'],
            cost['maintenance_cost_previous'], cost['cost_delta']),
    ]
    if hse:
        rows += [
            row('hse', 'open_incidents', hse['open_incidents']),
            row('hse', 'high_risk_open', hse['high_risk_open']),
            row('hse', 'incidents_current', hse['incidents_current'],
                hse['incidents_previous'], hse['incidents_delta']),
            row('hse', 'days_since_last_high_risk',
                hse['days_since_last_high_risk'] if hse['days_since_last_high_risk'] is not None else 'none recorded'),
        ]
    return rows


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
# Asset-level KPI dossier (drill target: Enterprise -> ... -> Asset -> ...)
# --------------------------------------------------------------------------- #

def compute_asset_kpi_profile(conn, asset_id: int, f: Optional[ExecutiveFilters] = None) -> dict:
    """Single-asset operational depth: every number traces to source records."""
    from .application import _asset_health  # deferred canonical scorer

    f = f or ExecutiveFilters(period_days=90)
    w = f.window()
    asset = get_asset_row(conn, asset_id)
    if asset is None:
        return {}

    win_start_dt = datetime.fromisoformat(w['period_start'] + 'T00:00:00')
    win_end_dt = datetime.fromisoformat(w['period_end'] + 'T23:59:59')
    window_hours = max(1.0, (win_end_dt - win_start_dt).total_seconds() / 3600.0)

    outages = []
    for row in _rows(conn.execute(
            'SELECT * FROM asset_outages WHERE asset_id=? AND start_at<=?'
            ' AND (end_at IS NULL OR end_at>=?) ORDER BY start_at',
            [asset_id, w['period_end'] + 'T23:59:59', w['period_start']])):
        hours = _outage_overlap_hours(row['start_at'], row.get('end_at'), win_start_dt, win_end_dt)
        if hours > 0:
            row['overlap_hours'] = round(hours, 2)
            outages.append(row)
    downtime = round(sum(x['overlap_hours'] for x in outages), 2)
    forced = [x for x in outages if x.get('outage_type') == 'Forced']
    availability = round(100 * max(0.0, window_hours - downtime) / window_hours, 2)

    corrective = _rows(conn.execute(
        '''SELECT id,wo_no,title,status,priority,actual_hours,actual_cost,
                  COALESCE(actual_finish,created_at) finished FROM work_orders
             WHERE asset_id=? AND status IN ('Completed','Closed')
               AND work_type LIKE 'Corrective%'
               AND COALESCE(actual_finish,created_at)>=?
             ORDER BY finished DESC''',
        (asset_id, (date.today() - timedelta(days=90)).isoformat())))
    open_wo = _rows(conn.execute(
        '''SELECT w.id,w.wo_no,w.title,w.priority,w.status,w.target_finish FROM work_orders w
             WHERE w.asset_id=? AND '''+_OPEN_WO+' ORDER BY '+_PRIORITY_ORDER, (asset_id,)))
    overdue_open = [x for x in open_wo if x.get('target_finish')
                    and str(x['target_finish'])[:10] < date.today().isoformat()]

    alarms_active = int(conn.execute(
        "SELECT COUNT(*) FROM operational_alarms WHERE asset_id=? AND status IN ('Open','Acknowledged')",
        (asset_id,)).fetchone()[0])
    alarms_recent = _rows(conn.execute(
        '''SELECT oa.alarm_no,oa.severity,oa.status,oa.message,oa.occurrence_count,oa.opened_at,tc.channel_code
             FROM operational_alarms oa LEFT JOIN telemetry_channels tc ON tc.id=oa.channel_id
            WHERE oa.asset_id=? AND oa.opened_at>=? ORDER BY oa.opened_at DESC LIMIT 10''',
        (asset_id, w['period_start'])))

    materials = _rows(conn.execute(
        '''SELECT m.quantity,m.unit_cost,i.item_no,i.name item_name,w.wo_no
             FROM work_order_materials m JOIN inventory_items i ON i.id=m.inventory_item_id
             JOIN work_orders w ON w.id=m.work_order_id
            WHERE w.asset_id=? AND m.issued_at>=? ORDER BY m.issued_at DESC LIMIT 20''',
        (asset_id, w['period_start'])))
    material_value = round(sum(float(x['quantity']) * float(x['unit_cost'] or 0) for x in materials), 2)

    technicians = _rows(conn.execute(
        '''SELECT u.id user_id,u.full_name name,COUNT(*) wo_count,
                  COALESCE(SUM(l.hours),0) hours
             FROM labor_entries l JOIN users u ON u.id=l.user_id
            WHERE l.work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)
              AND l.work_date>=?
            GROUP BY u.id ORDER BY hours DESC LIMIT 10''',
        (asset_id, w['period_start'])))

    health = _asset_health(conn, asset_id)
    channels = _rows(conn.execute(
        'SELECT id, channel_code, name FROM telemetry_channels WHERE asset_id=?', (asset_id,)))
    channel_signals = []
    if channels:
        payload = compute_deterioration_signals(conn, f, limit=100)
        by_code = {s['code']: s for s in payload['signals']}
        channel_signals = [by_code[c['channel_code']] for c in channels if c['channel_code'] in by_code]

    return {
        'asset': {'id': asset['id'], 'asset_no': asset['asset_no'], 'name': asset['name'],
                  'criticality': asset['criticality'], 'condition': asset['condition'],
                  'status': asset['status'], 'site_name': asset['site_name']},
        'health': health,
        'window': w,
        'reliability': {
            'availability_pct': availability,
            'outage_count': len(forced),
            'downtime_hours': downtime,
            'mtbf_hours': round(max(0.0, window_hours - downtime) / len(forced), 2) if forced else None,
            'mttr_hours': round(downtime / len(forced), 2) if forced else None,
            'outages': [
                {'record': x['outage_no'], 'type': x.get('outage_type'),
                 'start_at': x['start_at'], 'end_at': x.get('end_at'),
                 'hours': x['overlap_hours'], 'work_order_id': x.get('work_order_id')}
                for x in outages],
        },
        'failures_90d': [
            {'wo_no': x['wo_no'], 'title': x['title'], 'hours': float(x['actual_hours'] or 0),
             'finished': x['finished']} for x in corrective],
        'repeat_failure_count': len(corrective),
        'open_work': [{'wo_no': x['wo_no'], 'title': x['title'], 'priority': x['priority'],
                       'status': x['status'], 'overdue': x in overdue_open} for x in open_wo],
        'alarms': {'active': alarms_active, 'recent': alarms_recent},
        'materials_consumed': {'lines': materials, 'value': material_value},
        'technicians': technicians,
        'deterioration_signals': channel_signals,
        'calculated_at': datetime.now().isoformat(timespec='seconds'),
    }


def get_asset_row(conn, asset_id: int) -> Optional[dict]:
    rows_ = _rows(conn.execute(
        '''SELECT a.*, s.name site_name FROM assets a
             LEFT JOIN locations l ON l.id=a.location_id
             LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?''', (asset_id,)))
    return rows_[0] if rows_ else None


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


def _compute_live_snapshot(conn, f: ExecutiveFilters) -> dict:
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
    costs = compute_cost_kpis(conn, f)
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
        'costs': costs,
        'hse': compute_hse_kpis(conn, f),
        'risk_backlog_summary': backlog['summary'],
        'top_risk_contributors': backlog['rows'][:10],
        'explanations': explain_kpi_changes(conn, f),
    }
    return payload


def executive_snapshot(conn, f: ExecutiveFilters, *, use_cache: bool = True) -> dict:
    """Scoped executive snapshot with safe materialization.

    Cache correctness contract:
    - scope key covers every filter that changes results (site, region, asset
      class, criticality); window key covers the full calculation window.
    - a cached payload is served only when no tracked source table mutated
      after its calculation AND it is younger than SNAPSHOT_TTL_MINUTES. The
      TTL bounds staleness from mutations without reliable timestamps.
    - storage failures fall back to live computation; snapshots are written in
      a single atomic upsert so no partial payload is ever visible.
    """
    if not use_cache:
        payload = _compute_live_snapshot(conn, f)
        payload['snapshot'] = {'served_from_cache': False}
        return payload
    try:
        cached = read_snapshot(conn, f)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    payload = _compute_live_snapshot(conn, f)
    payload['snapshot'] = {'served_from_cache': False}
    stored = False
    try:
        stored = write_snapshot(conn, f, payload)
    except Exception:
        stored = False
    payload.setdefault('snapshot', {})
    payload['snapshot']['materialized'] = bool(stored)
    return payload
