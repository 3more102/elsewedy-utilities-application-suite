from __future__ import annotations

from datetime import date, timedelta

from apps.inventory import work_order_parts_readiness
from apps.workforce import forecast_bucket_start, week_capacity


def _rows(cur):
    return [dict(row) for row in cur.fetchall()]


def maintenance_forecast(conn, horizon_days: int = 90, site_id: int | None = None, *, today: date | None = None) -> dict:
    start = today or date.today()
    end = start + timedelta(days=horizon_days)
    weeks: dict[str, dict] = {}
    cursor = forecast_bucket_start(start)
    while cursor <= end:
        capacity = week_capacity(conn, cursor, site_id)
        weeks[cursor.isoformat()] = {
            'week_start': cursor.isoformat(), 'pm_jobs': 0, 'backlog_jobs': 0, 'demand_hours': 0.0,
            'capacity_hours': capacity['capacity_hours'], 'technicians': capacity['technicians'],
            'capacity_source': capacity['source'], 'parts_ready_jobs': 0, 'parts_shortage_jobs': 0,
            'parts_unknown_jobs': 0, 'craft_demand': {}, 'craft_capacity': capacity['craft_capacity'],
        }
        cursor += timedelta(days=7)

    site_clause = ''
    args: list[object] = []
    if site_id is not None:
        site_clause = ' AND l.site_id=?'
        args.append(site_id)
    plans = _rows(conn.execute(
        "SELECT p.*,a.asset_no,l.site_id FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id "
        "LEFT JOIN locations l ON l.id=a.location_id WHERE p.active=1 AND p.next_due IS NOT NULL" + site_clause,
        args,
    ))
    for plan in plans:
        try:
            due = date.fromisoformat(str(plan['next_due'])[:10])
        except (TypeError, ValueError):
            continue
        if start <= due <= end:
            bucket = weeks.get(forecast_bucket_start(due).isoformat())
            if bucket:
                bucket['pm_jobs'] += 1
                bucket['demand_hours'] += 2.0
                bucket['parts_unknown_jobs'] += 1

    work_args: list[object] = []
    work_site = ''
    if site_id is not None:
        work_site = ' AND l.site_id=?'
        work_args.append(site_id)
    work_orders = _rows(conn.execute(
        "SELECT w.*,l.site_id FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id "
        "WHERE w.status NOT IN ('Completed','Closed','Cancelled')" + work_site,
        work_args,
    ))
    for work in work_orders:
        raw = work.get('target_start') or work.get('target_finish') or start.isoformat()
        try:
            due = date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            due = start
        if due < start:
            due = start
        if due > end:
            continue
        bucket = weeks.get(forecast_bucket_start(due).isoformat())
        if not bucket:
            continue
        demand = float(work.get('estimated_hours') or 2.0)
        bucket['backlog_jobs'] += 1
        bucket['demand_hours'] += demand
        readiness = work_order_parts_readiness(conn, work['id'])
        if readiness['state'] == 'Ready':
            bucket['parts_ready_jobs'] += 1
        elif readiness['state'] == 'Shortage':
            bucket['parts_shortage_jobs'] += 1
        else:
            bucket['parts_unknown_jobs'] += 1
        crafts = _rows(conn.execute(
            """SELECT c.craft_code,r.planned_hours FROM work_order_craft_requirements r
               JOIN crafts c ON c.id=r.craft_id WHERE r.work_order_id=?""",
            (work['id'],),
        ))
        if crafts:
            for craft in crafts:
                code = craft['craft_code']
                bucket['craft_demand'][code] = round(bucket['craft_demand'].get(code, 0.0) + float(craft['planned_hours'] or 0), 1)
        else:
            bucket['craft_demand']['UNASSIGNED'] = round(bucket['craft_demand'].get('UNASSIGNED', 0.0) + demand, 1)

    output: list[dict] = []
    for bucket in weeks.values():
        bucket['demand_hours'] = round(bucket['demand_hours'], 1)
        capacity = float(bucket['capacity_hours'] or 0)
        bucket['utilization_pct'] = round(100 * bucket['demand_hours'] / capacity, 1) if capacity else (100.0 if bucket['demand_hours'] else 0.0)
        bucket['capacity_state'] = 'Over Capacity' if bucket['utilization_pct'] > 100 else 'High' if bucket['utilization_pct'] >= 80 else 'Available'
        craft_states = {}
        for code, demand in bucket['craft_demand'].items():
            craft_capacity = float(bucket['craft_capacity'].get(code, 0))
            craft_states[code] = {
                'demand_hours': round(demand, 1), 'capacity_hours': round(craft_capacity, 1),
                'shortage_hours': round(max(0, demand - craft_capacity), 1),
            }
        bucket['craft_states'] = craft_states
        output.append(bucket)

    total_capacity = round(sum(row['capacity_hours'] for row in output), 1)
    return {
        'horizon_days': horizon_days, 'technicians': max([row['technicians'] for row in output] or [0]),
        'weekly_capacity_hours': round(output[0]['capacity_hours'], 1) if output else 0,
        'capacity_source': output[0]['capacity_source'] if output else 'none', 'weeks': output,
        'summary': {
            'pm_jobs': sum(row['pm_jobs'] for row in output), 'backlog_jobs': sum(row['backlog_jobs'] for row in output),
            'demand_hours': round(sum(row['demand_hours'] for row in output), 1), 'capacity_hours': total_capacity,
            'peak_utilization_pct': max([row['utilization_pct'] for row in output] or [0]),
            'parts_shortage_jobs': sum(row['parts_shortage_jobs'] for row in output),
            'parts_ready_jobs': sum(row['parts_ready_jobs'] for row in output),
        },
    }
