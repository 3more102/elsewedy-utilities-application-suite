from __future__ import annotations

from datetime import date, timedelta


def _rows(cur):
    return [dict(row) for row in cur.fetchall()]


def forecast_bucket_start(value: date) -> date:
    """Return the Monday bucket for a planning date."""
    return value - timedelta(days=value.weekday())


def parse_days_of_week(value) -> set[int]:
    """Parse stored weekday CSV; invalid/empty values retain the legacy Mon-Fri default."""
    result: set[int] = set()
    for raw in str(value or '0,1,2,3,4').split(','):
        try:
            day = int(raw.strip())
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            result.add(day)
    return result or {0, 1, 2, 3, 4}


def week_capacity(conn, week_start: date, site_id: int | None = None) -> dict:
    """Calculate technician capacity from real shift/profile and absence data."""
    week_end = week_start + timedelta(days=6)
    sql = """SELECT tp.*,u.full_name,u.username,c.craft_code,c.name craft_name,s.site_code,s.name site_name
             FROM technician_profiles tp JOIN users u ON u.id=tp.user_id JOIN roles r ON r.id=u.role_id
             LEFT JOIN crafts c ON c.id=tp.craft_id LEFT JOIN sites s ON s.id=tp.home_site_id
             WHERE tp.active=1 AND u.active=1 AND r.code='technician'"""
    args: list[object] = []
    if site_id is not None:
        sql += ' AND tp.home_site_id=?'
        args.append(site_id)
    techs = _rows(conn.execute(sql, args))
    details: list[dict] = []
    craft_capacity: dict[str, float] = {}
    total = 0.0
    for tech in techs:
        assignments = _rows(conn.execute(
            """SELECT tsa.*,st.paid_hours,st.shift_code,st.name shift_name FROM technician_shift_assignments tsa
               JOIN shift_templates st ON st.id=tsa.shift_id WHERE tsa.user_id=? AND tsa.active=1 AND st.active=1
               AND tsa.effective_from<=? AND (tsa.effective_to IS NULL OR tsa.effective_to>=?)""",
            (tech['user_id'], week_end.isoformat(), week_start.isoformat()),
        ))
        scheduled = 0.0
        if assignments:
            for offset in range(7):
                day = week_start + timedelta(days=offset)
                day_hours = 0.0
                for assignment in assignments:
                    if day.weekday() not in parse_days_of_week(assignment.get('days_of_week')):
                        continue
                    if str(assignment['effective_from'])[:10] > day.isoformat():
                        continue
                    if assignment.get('effective_to') and str(assignment['effective_to'])[:10] < day.isoformat():
                        continue
                    day_hours = max(day_hours, float(assignment.get('paid_hours') or 0))
                scheduled += day_hours
        else:
            scheduled = float(tech.get('weekly_hours') or 40)

        absence = 0.0
        absences = _rows(conn.execute(
            """SELECT * FROM technician_absences WHERE user_id=? AND status='Approved'
               AND start_date<=? AND end_date>=?""",
            (tech['user_id'], week_end.isoformat(), week_start.isoformat()),
        ))
        for row in absences:
            try:
                absence_start = date.fromisoformat(str(row['start_date'])[:10])
                absence_end = date.fromisoformat(str(row['end_date'])[:10])
            except (TypeError, ValueError):
                continue
            for offset in range(7):
                day = week_start + timedelta(days=offset)
                if absence_start <= day <= absence_end and day.weekday() < 5:
                    absence += float(row.get('hours_per_day') or 8)

        efficiency = max(0.0, min(100.0, float(tech.get('efficiency_pct') or 100)))
        available = round(max(0.0, scheduled - absence) * efficiency / 100.0, 1)
        total += available
        craft = tech.get('craft_code') or 'UNASSIGNED'
        craft_capacity[craft] = round(craft_capacity.get(craft, 0.0) + available, 1)
        details.append({
            'user_id': tech['user_id'], 'username': tech['username'], 'name': tech['full_name'],
            'craft_code': tech.get('craft_code'), 'craft_name': tech.get('craft_name'),
            'site_code': tech.get('site_code'), 'site_name': tech.get('site_name'),
            'scheduled_hours': round(scheduled, 1), 'absence_hours': round(absence, 1),
            'efficiency_pct': float(tech.get('efficiency_pct') or 100), 'available_hours': available,
        })

    if not techs:
        count = int(conn.execute(
            "SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='technician' AND u.active=1"
        ).fetchone()[0])
        total = float(count) * 40.0
        return {
            'technicians': count, 'capacity_hours': round(total, 1),
            'craft_capacity': {'UNASSIGNED': round(total, 1)}, 'details': [], 'source': 'role_fallback',
        }
    return {
        'technicians': len(techs), 'capacity_hours': round(total, 1),
        'craft_capacity': craft_capacity, 'details': details, 'source': 'workforce_schedule',
    }
