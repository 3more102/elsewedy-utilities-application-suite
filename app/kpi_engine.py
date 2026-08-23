"""Configurable KPI engine.

KPI definitions live in `kpi_definitions`; every calculation produces an
immutable row in `kpi_snapshots` with value, status, thresholds, provenance and
a persisted contributor breakdown so dashboards never dead-end at a number.

Calculations are always derived from real EUAS operational records (work
orders, maintenance plans, outages, alarms) through the provider registry
below. No provider may fabricate data: when there is no evidence, the value is
None and the status is UNKNOWN.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .database import now

DIRECTIONS = ('higher_is_better', 'lower_is_better')

# Registry of source-key -> calculator. Keys are stable identifiers stored on
# kpi_definitions.source_key; adding a new metric means registering a provider
# here, not writing bespoke dashboard SQL.
KPI_PROVIDERS: dict[str, object] = {}


def kpi_provider(*keys):
    def deco(fn):
        for key in keys:
            if key in KPI_PROVIDERS:
                raise RuntimeError(f'duplicate kpi provider key {key}')
            KPI_PROVIDERS[key] = fn
        return fn
    return deco


def evaluate_status(value, caution, alert, direction):
    """GREEN/AMBER/RED evaluation honouring threshold equality boundaries."""
    if direction not in DIRECTIONS:
        direction = 'higher_is_better'
    if value is None:
        return 'UNKNOWN'
    has_bounds = caution is not None or alert is not None
    if not has_bounds:
        return 'UNKNOWN'
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 'UNKNOWN'
    if direction == 'lower_is_better':
        if caution is not None and v <= float(caution):
            return 'GREEN'
        if alert is not None and v <= float(alert):
            return 'AMBER'
        return 'RED'
    if caution is not None and v >= float(caution):
        return 'GREEN'
    if alert is not None and v >= float(alert):
        return 'AMBER'
    return 'RED'


def window_bounds(window_days: int, as_of: str | None = None):
    """Inclusive [start, end] ISO window ending at as_of (default today)."""
    end_day = date.fromisoformat(as_of) if as_of else date.today()
    start_day = end_day - timedelta(days=max(1, int(window_days)) - 1)
    return start_day.isoformat(), end_day.isoformat()


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], '%Y-%m-%d')
        except ValueError:
            return None


def _hours_between(start, end):
    a, b = _parse_dt(start), _parse_dt(end)
    if not a or not b or b < a:
        return None
    return (b - a).total_seconds() / 3600.0


def _overlap_hours(start_at, end_at, win_start, win_end):
    """Hours an outage interval overlaps the window; open outages run to win_end."""
    s = _parse_dt(start_at)
    e = _parse_dt(end_at) or _parse_dt(win_end)
    ws = _parse_dt(f'{win_start}T00:00:00')
    we = _parse_dt(f'{win_end}T23:59:59')
    if not s or not ws or not we:
        return None
    if e and e < ws:
        return 0.0
    if s > we:
        return 0.0
    s = max(s, ws)
    e = min(e, we) if e else we
    if e < s:
        return 0.0
    return (e - s).total_seconds() / 3600.0


def _wo_scope_sql(scope, site_alias='l'):
    sql = ''
    args: list = []
    if scope.get('site_id'):
        sql += f' AND {site_alias}.site_id=?'
        args.append(int(scope['site_id']))
    return sql, args


def _scope_assets(conn, scope):
    """Distinct asset ids inside scope (used for exposure-hour denominators)."""
    sql = 'SELECT a.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE 1=1'
    args: list = []
    if scope.get('site_id'):
        sql += ' AND l.site_id=?'
        args.append(int(scope['site_id']))
    if scope.get('asset_id'):
        sql += ' AND a.id=?'
        args.append(int(scope['asset_id']))
    return [r[0] for r in conn.execute(sql, args).fetchall()]


def _work_orders_in_window(conn, ctx, extra_where='', extra_args=()):
    scope = ctx.get('scope') or {}
    sql = '''SELECT w.*,a.asset_no,a.criticality asset_criticality,l.site_id
             FROM work_orders w
             LEFT JOIN assets a ON a.id=w.asset_id
             LEFT JOIN locations l ON l.id=w.location_id
             WHERE 1=1'''
    args: list = []
    site_sql, site_args = _wo_scope_sql(scope)
    sql += site_sql
    args += site_args
    if extra_where:
        sql += ' AND ' + extra_where
        args += list(extra_args)
    order = ' ORDER BY w.id DESC LIMIT 200'
    return conn.execute(sql + order, args).fetchall(), sql[:-len(order)], args


def _ratio(num, den):
    if den is None or den == 0 or num is None:
        return None
    return round(100.0 * num / den, 2)


def _result(value, numerator=None, denominator=None, contributors=None,
            breakdown=None, freshness=None, formula=''):
    return {
        'value': value,
        'numerator': numerator,
        'denominator': denominator,
        'contributors': contributors or [],
        'breakdown': breakdown or [],
        'freshness': freshness,
        'formula': formula,
    }


# ---------- maintenance providers ----------

@kpi_provider('pm_compliance')
def _pm_compliance(conn, ctx):
    win_start, win_end = ctx['window_start'], ctx['window_end']
    rows_, _, _ = _work_orders_in_window(
        conn, ctx,
        "w.pm_plan_id IS NOT NULL AND w.work_type='Preventive' "
        "AND w.target_finish IS NOT NULL AND substr(w.target_finish,1,10)>=? "
        "AND substr(w.target_finish,1,10)<=? AND w.status NOT IN ('Cancelled','Rejected')",
        (win_start, win_end),
    )
    due, met, missed = len(rows_), 0, []
    for w in rows_:
        finished = str(w['actual_finish'] or '')[:10]
        target = str(w['target_finish'])[:10]
        compliant = w['status'] in ('Completed', 'Closed') and finished and finished <= target
        if compliant:
            met += 1
        else:
            missed.append(w)
    freshness = max((str(w['target_finish']) for w in rows_), default=None)
    return _result(
        _ratio(met, due), numerator=met, denominator=due,
        contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                       'label': w['title'], 'detail': f"status={w['status']} target={w['target_finish']}"} for w in missed],
        breakdown=_breakdown_by_site(rows_, lambda w: w['status'] in ('Completed', 'Closed')),
        freshness=freshness,
        formula='PM work orders completed on/before target finish ÷ PM work orders due in window × 100',
    )


@kpi_provider('schedule_compliance')
def _schedule_compliance(conn, ctx):
    win_start, win_end = ctx['window_start'], ctx['window_end']
    rows_, _, _ = _work_orders_in_window(
        conn, ctx,
        "w.status IN ('Completed','Closed') AND substr(w.actual_finish,1,10)>=? "
        "AND substr(w.actual_finish,1,10)<=? AND w.target_finish IS NOT NULL",
        (win_start, win_end),
    )
    on_time = sum(1 for w in rows_ if str(w['actual_finish'])[:10] <= str(w['target_finish'])[:10])
    return _result(
        _ratio(on_time, len(rows_)), numerator=on_time, denominator=len(rows_),
        contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                       'label': w['title'], 'detail': f"finished {str(w['actual_finish'])[:10]} vs target {str(w['target_finish'])[:10]}"}
                      for w in rows_ if str(w['actual_finish'])[:10] > str(w['target_finish'])[:10]],
        breakdown=_breakdown_by_site(rows_, lambda w: True),
        freshness=win_end,
        formula='Work orders finished on/before target ÷ work orders completed in window × 100',
    )


@kpi_provider('overdue_work_orders', 'backlog_open', 'critical_backlog')
def _open_work_counts(conn, ctx):
    key = ctx['source_key']
    today = ctx['window_end']
    if key == 'overdue_work_orders':
        where = "w.status NOT IN ('Completed','Closed','Cancelled') AND w.target_finish IS NOT NULL AND substr(w.target_finish,1,10)<?"
        args = (today,)
    elif key == 'critical_backlog':
        where = ("w.status NOT IN ('Completed','Closed','Cancelled') AND "
                 "(w.priority IN ('Emergency','Critical') OR a.criticality='Critical')")
        args = ()
    else:
        where = "w.status NOT IN ('Completed','Closed','Cancelled')"
        args = ()
    rows_, _, _ = _work_orders_in_window(conn, ctx, where, args)
    return _result(
        len(rows_) if rows_ else 0,
        numerator=len(rows_), denominator=None,
        contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                       'label': w['title'],
                       'detail': f"priority={w['priority']} status={w['status']} target={w['target_finish']} asset={w['asset_no'] or '-'}"}
                      for w in rows_],
        breakdown=_breakdown_by_site(rows_, lambda w: True),
        freshness=now(),
        formula='Open work orders matching the backlog definition at window end',
    )


@kpi_provider('emergency_work_pct', 'reactive_work_pct')
def _work_mix_pct(conn, ctx):
    key = ctx['source_key']
    win_start, win_end = ctx['window_start'], ctx['window_end']
    total_rows, _, _ = _work_orders_in_window(
        conn, ctx, "substr(w.created_at,1,10)>=? AND substr(w.created_at,1,10)<=?", (win_start, win_end))
    if key == 'emergency_work_pct':
        match = [w for w in total_rows if w['priority'] == 'Emergency' or w['work_type'] == 'Emergency']
        label = 'Emergency-priority work orders created in window'
    else:
        match = [w for w in total_rows if str(w['work_type']).startswith(('Corrective', 'Breakdown', 'Emergency'))]
        label = 'Corrective/breakdown work orders created in window'
    return _result(
        _ratio(len(match), len(total_rows)), numerator=len(match), denominator=len(total_rows),
        contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                       'label': w['title'], 'detail': f"type={w['work_type']} priority={w['priority']}"} for w in match],
        breakdown=_breakdown_by_site(match, lambda w: True),
        freshness=win_end,
        formula=f'{label} ÷ all work orders created in window × 100',
    )


@kpi_provider('mttr_hours')
def _mttr(conn, ctx):
    win_start, win_end = ctx['window_start'], ctx['window_end']
    rows_, _, _ = _work_orders_in_window(
        conn, ctx,
        "w.status IN ('Completed','Closed') AND w.actual_start IS NOT NULL AND w.actual_finish IS NOT NULL "
        "AND substr(w.actual_finish,1,10)>=? AND substr(w.actual_finish,1,10)<=? "
        "AND (w.work_type LIKE 'Corrective%' OR w.work_type='Breakdown' OR w.work_type='Emergency')",
        (win_start, win_end))
    hours = [(w, _hours_between(w['actual_start'], w['actual_finish'])) for w in rows_]
    hours = [(w, h) for w, h in hours if h is not None]
    total = sum(h for _, h in hours)
    value = round(total / len(hours), 2) if hours else None
    worst = sorted(hours, key=lambda x: -x[1])[:20]
    return _result(
        value, numerator=round(total, 2), denominator=len(hours),
        contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                       'label': w['title'], 'detail': f'repair hours={h:.1f}'} for w, h in worst],
        breakdown=_breakdown_by_site([w for w, _ in hours], lambda w: True),
        freshness=win_end,
        formula='Mean of (actual finish − actual start) hours across corrective completions in window',
    )


@kpi_provider('mtbf_hours', 'failure_count')
def _failures_based(conn, ctx):
    key = ctx['source_key']
    win_start, win_end = ctx['window_start'], ctx['window_end']
    rows_, _, _ = _work_orders_in_window(
        conn, ctx,
        "w.status IN ('Completed','Closed') AND substr(w.actual_finish,1,10)>=? AND substr(w.actual_finish,1,10)<=? "
        "AND (w.work_type LIKE 'Corrective%' OR w.work_type='Breakdown' OR w.work_type='Emergency')",
        (win_start, win_end))
    failures = len(rows_)
    assets = _scope_assets(conn, ctx.get('scope') or {})
    window_hours = max(1, int(ctx.get('window_days') or 30)) * 24.0
    operating_hours = window_hours * len(assets)
    if key == 'failure_count':
        return _result(
            failures, numerator=failures, denominator=None,
            contributors=[{'record_type': 'work_order', 'record_id': w['id'], 'record_code': w['wo_no'],
                           'label': w['title'], 'detail': f"asset={w['asset_no'] or '-'} type={w['work_type']}"} for w in rows_],
            breakdown=_breakdown_by_site(rows_, lambda w: True), freshness=win_end,
            formula='Corrective/breakdown completions in window')
    if not failures:
        # No failure evidence with known exposure => MTBF equals the full exposure.
        value = round(operating_hours, 2) if operating_hours else None
    else:
        value = round(operating_hours / failures, 2) if operating_hours else None
    per_asset = {}
    for w in rows_:
        if w['asset_id'] is not None:
            per_asset[w['asset_id']] = per_asset.get(w['asset_id'], 0) + 1
    return _result(
        value, numerator=operating_hours, denominator=failures,
        contributors=[{'record_type': 'asset', 'record_id': aid, 'record_code': None,
                       'label': f'asset #{aid}', 'detail': f'{count} failures in window'}
                      for aid, count in sorted(per_asset.items(), key=lambda kv: -kv[1])[:20]],
        breakdown=_breakdown_by_site(rows_, lambda w: True), freshness=win_end,
        formula='Scoped asset exposure hours ÷ recorded failures in window')


@kpi_provider('availability_pct', 'unplanned_downtime_hours')
def _outage_based(conn, ctx):
    key = ctx['source_key']
    win_start, win_end = ctx['window_start'], ctx['window_end']
    scope = ctx.get('scope') or {}
    sql = '''SELECT o.*,a.asset_no FROM asset_outages o LEFT JOIN assets a ON a.id=o.asset_id
             WHERE o.outage_type IN ('Forced','Planned','Maintenance')'''
    args: list = []
    if scope.get('site_id'):
        sql += ' AND o.site_id=?'
        args.append(int(scope['site_id']))
    if scope.get('asset_id'):
        sql += ' AND o.asset_id=?'
        args.append(int(scope['asset_id']))
    outage_rows = conn.execute(sql + ' ORDER BY o.id DESC LIMIT 500', args).fetchall()
    forced_total = planned_total = 0.0
    contributors = []
    for o in outage_rows:
        h = _overlap_hours(o['start_at'], o['end_at'], win_start, win_end)
        if not h:
            continue
        if o['outage_type'] == 'Forced':
            forced_total += h
            contributors.append({'record_type': 'outage', 'record_id': o['id'], 'record_code': o['outage_no'],
                                 'label': o['impact'] or o['outage_no'], 'detail': f'{h:.1f}h forced ({o["cause_code"] or "-"})'})
        else:
            planned_total += h
    downtime = unplanned = round(forced_total, 2)
    assets = _scope_assets(conn, scope)
    window_hours = max(1, int(ctx.get('window_days') or 30)) * 24.0
    exposure = window_hours * len(assets)
    if key == 'unplanned_downtime_hours':
        return _result(unplanned, numerator=unplanned, denominator=None, contributors=contributors,
                       freshness=win_end, formula='Sum of forced-outage overlap hours within window')
    value = round(max(0.0, 100.0 * (exposure - downtime) / exposure), 2) if exposure else None
    return _result(value, numerator=round(exposure - downtime, 2) if exposure else None, denominator=exposure or None,
                   contributors=contributors, freshness=win_end,
                   formula='(Exposure hours − outage hours) ÷ exposure hours × 100 over scoped assets')


@kpi_provider('active_critical_alarms', 'active_alarm_count')
def _alarm_counts(conn, ctx):
    key = ctx['source_key']
    severity_filter = " AND al.severity='Critical'" if key == 'active_critical_alarms' else ''
    scope = ctx.get('scope') or {}
    sql = '''SELECT al.*,a.asset_no FROM operational_alarms al
             LEFT JOIN assets a ON a.id=al.asset_id
             WHERE al.status IN ('Open','Acknowledged')''' + severity_filter
    args: list = []
    if scope.get('site_id'):
        sql += ' AND al.site_id=?'
        args.append(int(scope['site_id']))
    alarm_rows = conn.execute(sql + ' ORDER BY al.id DESC LIMIT 200', args).fetchall()
    label = 'Critical' if key == 'active_critical_alarms' else ''
    return _result(
        len(alarm_rows), numerator=len(alarm_rows), denominator=None,
        contributors=[{'record_type': 'alarm', 'record_id': r['id'], 'record_code': r['alarm_no'],
                       'label': r['message'], 'detail': f'severity={r["severity"]} opened={r["opened_at"]} asset={r["asset_no"] or "-"}'}
                      for r in alarm_rows],
        freshness=now(),
        formula=(f'Open/acknowledged {label} operational alarms at window end'.strip()),
    )


def _breakdown_by_site(row_list, include):
    counts: dict = {}
    for w in row_list:
        if not include(w):
            continue
        site = w['site_id'] if 'site_id' in w.keys() else None
        counts[site] = counts.get(site, 0) + 1
    return [{'group': 'site', 'key': k, 'count': v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def compute_kpi(conn, definition, as_of: str | None = None):
    """Compute a definition without persisting anything."""
    source_key = str(definition['source_key'])
    provider = KPI_PROVIDERS.get(source_key)
    if provider is None:
        raise ValueError(f'unsupported_source:{source_key}')
    window_days = int(definition['time_window_days'] or 30)
    win_start, win_end = window_bounds(window_days, as_of)
    try:
        scope = json.loads(definition['scope_json'] or '{}')
    except (TypeError, ValueError):
        scope = {}
    ctx = {
        'source_key': source_key,
        'window_days': window_days,
        'window_start': win_start,
        'window_end': win_end,
        'scope': scope if isinstance(scope, dict) else {},
        'filters': definition.get('filters_json'),
    }
    result = provider(conn, ctx)
    result['window_start'] = win_start
    result['window_end'] = win_end
    return result


def previous_snapshot(conn, kpi_id: int):
    return conn.execute(
        'SELECT * FROM kpi_snapshots WHERE kpi_id=? ORDER BY id DESC LIMIT 1', (kpi_id,)
    ).fetchone()


def persist_snapshot(conn, definition, result, actor_id=None):
    prev = previous_snapshot(conn, definition['id'])
    prev_value = prev['value'] if prev else None
    trend = None
    change_pct = None
    if result['value'] is not None and prev_value is not None:
        delta = round(float(result['value']) - float(prev_value), 4)
        trend = 'flat' if delta == 0 else ('up' if delta > 0 else 'down')
        if float(prev_value) != 0:
            change_pct = round(100.0 * delta / abs(float(prev_value)), 2)
    status = evaluate_status(result['value'], definition['caution_value'], definition['alert_value'], definition['direction'])
    calculated_at = now()
    cur = conn.execute(
        '''INSERT INTO kpi_snapshots(kpi_id,period_start,period_end,value,previous_value,trend,change_pct,status,
           target_value,caution_value,alert_value,numerator,denominator,contributors_json,data_freshness_at,
           provenance_json,calculated_at,calculated_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (definition['id'], result['window_start'], result['window_end'], result['value'], prev_value, trend, change_pct,
         status, definition['target_value'], definition['caution_value'], definition['alert_value'],
         result['numerator'], result['denominator'],
         json.dumps(result['breakdown'], ensure_ascii=False, sort_keys=True),
         result['freshness'],
         json.dumps({'source_key': definition['source_key'], 'provider': KPI_PROVIDERS[definition['source_key']].__name__,
                     'formula': result['formula'], 'scope': json.loads(definition['scope_json'] or '{}'),
                     'contributor_count': len(result['contributors'])}, ensure_ascii=False, sort_keys=True, default=str),
         calculated_at, actor_id))
    snapshot_id = cur.lastrowid
    return get_snapshot(conn, snapshot_id)


def get_snapshot(conn, snapshot_id: int):
    return dict(conn.execute('SELECT * FROM kpi_snapshots WHERE id=?', (snapshot_id,)).fetchone())


def evaluate_kpi(conn, definition, actor_id=None, as_of: str | None = None, persist=True):
    result = compute_kpi(conn, definition, as_of=as_of)
    if persist:
        snapshot = persist_snapshot(conn, definition, result, actor_id=actor_id)
    else:
        snapshot = None
    return result, snapshot
