from __future__ import annotations

from datetime import date, datetime, timedelta


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def outage_overlap_hours(start_value, end_value, window_start: datetime, window_end: datetime) -> float:
    try:
        start = _dt(start_value)
    except Exception:
        return 0.0
    try:
        end = _dt(end_value) if end_value else min(datetime.now(), window_end)
    except Exception:
        end = min(datetime.now(), window_end)
    left = max(start, window_start)
    right = min(end, window_end)
    return max(0.0, (right - left).total_seconds() / 3600.0)


def asset_reliability_rows(conn, period_days: int = 365, site_id: int | None = None) -> list[dict]:
    today = date.today()
    cutoff = today - timedelta(days=period_days)
    window_end = datetime.now()
    sql = '''SELECT a.id,a.asset_no,a.name,a.commissioning_date,a.criticality,a.condition,
                    s.id site_id,s.site_code,s.name site_name
             FROM assets a LEFT JOIN locations l ON l.id=a.location_id
             LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1'''
    args = []
    if site_id is not None:
        sql += ' AND s.id=?'
        args.append(site_id)
    assets = _rows(conn.execute(sql, args))
    result = []
    for asset in assets:
        start = cutoff
        if asset.get('commissioning_date'):
            try:
                start = max(start, date.fromisoformat(str(asset['commissioning_date'])[:10]))
            except Exception:
                pass
        window_start = datetime.combine(start, datetime.min.time())
        period_hours = max(24.0, (window_end - window_start).total_seconds() / 3600.0)
        outages = _rows(
            conn.execute(
                '''SELECT * FROM asset_outages WHERE asset_id=? AND outage_type='Forced'
                   AND start_at<=? AND (end_at IS NULL OR end_at>=?) ORDER BY start_at''',
                (asset['id'], window_end.isoformat(timespec='seconds'), window_start.isoformat(timespec='seconds')),
            )
        )
        if outages:
            failure_count = len(outages)
            downtime = sum(outage_overlap_hours(x['start_at'], x.get('end_at'), window_start, window_end) for x in outages)
            source = 'outage_events'
        else:
            failures = _rows(
                conn.execute(
                    '''SELECT id,wo_no,actual_hours,actual_cost,COALESCE(actual_finish,created_at) event_date FROM work_orders
                       WHERE asset_id=? AND status IN ('Completed','Closed')
                       AND (work_type LIKE 'Corrective%' OR work_type='Breakdown')
                       AND COALESCE(actual_finish,created_at)>=?''',
                    (asset['id'], start.isoformat()),
                )
            )
            failure_count = len(failures)
            downtime = sum(float(x.get('actual_hours') or 0) for x in failures)
            source = 'work_order_hours_fallback' if failures else 'no_failures'
        uptime = max(0.0, period_hours - downtime)
        mttr = round(downtime / failure_count, 2) if failure_count else 0.0
        mtbf = round(uptime / failure_count, 2) if failure_count else None
        availability = round(100 * uptime / period_hours, 3) if period_hours else 100.0
        cost = conn.execute(
            'SELECT COALESCE(SUM(amount),0) FROM maintenance_cost_ledger WHERE asset_id=? AND posted_at>=?',
            (asset['id'], start.isoformat()),
        ).fetchone()[0] or 0
        result.append({
            **asset,
            'period_days': max(1, (today - start).days),
            'period_hours': round(period_hours, 1),
            'failures': failure_count,
            'downtime_hours': round(downtime, 2),
            'downtime_source': source,
            'mtbf_hours': mtbf,
            'mttr_hours': mttr,
            'availability_pct': availability,
            'maintenance_cost': round(float(cost), 2),
        })
    return result


def site_reliability_rows(conn, period_days: int = 365) -> list[dict]:
    assets = asset_reliability_rows(conn, period_days, None)
    sites: dict[int, dict] = {}
    for asset in assets:
        key = asset.get('site_id')
        if key is None:
            continue
        site = sites.setdefault(key, {
            'site_id': key,
            'site_code': asset.get('site_code'),
            'site_name': asset.get('site_name'),
            'assets': 0,
            'failures': 0,
            'period_hours': 0.0,
            'downtime_hours': 0.0,
            'maintenance_cost': 0.0,
        })
        site['assets'] += 1
        site['failures'] += asset['failures']
        site['period_hours'] += asset['period_hours']
        site['downtime_hours'] += asset['downtime_hours']
        site['maintenance_cost'] += asset['maintenance_cost']
    result = []
    for site in sites.values():
        uptime = max(0.0, site['period_hours'] - site['downtime_hours'])
        failures = site['failures']
        site['mtbf_hours'] = round(uptime / failures, 2) if failures else None
        site['mttr_hours'] = round(site['downtime_hours'] / failures, 2) if failures else 0.0
        site['availability_pct'] = round(100 * uptime / site['period_hours'], 3) if site['period_hours'] else 100.0
        site['maintenance_cost'] = round(site['maintenance_cost'], 2)
        site['period_hours'] = round(site['period_hours'], 1)
        site['downtime_hours'] = round(site['downtime_hours'], 2)
        result.append(site)
    return sorted(result, key=lambda x: (x['availability_pct'], x['site_name'] or ''))
