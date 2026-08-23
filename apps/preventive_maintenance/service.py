from __future__ import annotations

from datetime import date, timedelta

from apps.approvals import create_approval
from apps.audit import audit
from apps.events import workflow_event
from apps.maintenance import ensure_work_sla
from apps.notifications import notify_once
from core.database import now
from core.shared import next_no


def _rows(cur):
    return [dict(row) for row in cur.fetchall()]


def is_plan_due(plan: dict, target: date) -> bool:
    trigger = plan.get('trigger_type')
    if trigger == 'Calendar':
        raw = plan.get('next_due')
        if not raw:
            return False
        try:
            return date.fromisoformat(str(raw)[:10]) <= target
        except (TypeError, ValueError):
            return False
    if trigger in ('Meter', 'Runtime', 'Usage'):
        interval = float(plan.get('meter_interval') or 0)
        return interval > 0 and float(plan.get('meter_reading') or 0) - float(plan.get('last_meter') or 0) >= interval
    if trigger == 'Condition':
        return plan.get('condition') in ('Warning', 'Poor', 'Critical')
    return False


def next_calendar_due(current_due, interval_days, target: date) -> str | None:
    if not current_due or not interval_days or int(interval_days) <= 0:
        return str(current_due)[:10] if current_due else None
    due = date.fromisoformat(str(current_due)[:10])
    step = timedelta(days=int(interval_days))
    while due <= target:
        due += step
    return due.isoformat()


def generate_due_work_orders(conn, actor_id: int, target: date) -> list[str]:
    """Generate at most one active work order per due PM plan in the current transaction."""
    generated: list[str] = []
    plans = _rows(conn.execute(
        '''SELECT p.*,a.asset_no,a.location_id,a.meter_reading,a.condition
           FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id WHERE p.active=1'''
    ))
    for plan in plans:
        if not is_plan_due(plan, target):
            continue
        if conn.execute(
            "SELECT id FROM work_orders WHERE pm_plan_id=? AND status NOT IN ('Closed','Cancelled')",
            (plan['id'],),
        ).fetchone():
            continue
        work_no = next_no(conn, 'work_orders', 'wo_no', 'WO-', 10026)
        cur = conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,description,asset_id,location_id,priority,status,work_type,requested_by,
                 target_start,target_finish,instructions,pm_plan_id,created_at,updated_at
               ) VALUES(?,?,?,?,?,?, 'Submitted','Preventive Maintenance',?,?,?,?,?,?,?)''',
            (
                work_no, plan['name'], plan['job_plan'], plan['asset_id'], plan['location_id'], plan['priority'],
                actor_id, target.isoformat(), target.isoformat(), plan['job_plan'], plan['id'], now(), now(),
            ),
        )
        ensure_work_sla(conn, cur.lastrowid)
        create_approval(
            conn, 'Work Management', 'work_order', cur.lastrowid, work_no,
            f'Approve {work_no} — {plan["name"]}', actor_id, assigned_role='supervisor',
        )
        workflow_event(
            conn, 'Work Management', 'work_order', cur.lastrowid, work_no,
            'AUTO SUBMIT', '', 'Submitted', actor_id, f'Generated from {plan["pm_no"]}',
        )
        next_due = plan.get('next_due')
        last_meter = plan.get('last_meter')
        if plan['trigger_type'] == 'Calendar':
            next_due = next_calendar_due(plan.get('next_due'), plan.get('interval_days'), target)
        if plan['trigger_type'] in ('Meter', 'Runtime', 'Usage'):
            last_meter = plan.get('meter_reading')
        conn.execute(
            'UPDATE maintenance_plans SET next_due=?,last_meter=?,last_generated=? WHERE id=?',
            (next_due, last_meter, now(), plan['id']),
        )
        audit(conn, actor_id, 'GENERATE WO', 'Preventive Maintenance', plan['pm_no'], '', work_no)
        generated.append(work_no)
        notify_once(
            conn, 'Preventive work generated', f'{work_no} generated from {plan["pm_no"]}', 'Info',
            None, 'planner', 'maintenance', plan['pm_no'],
        )
    return generated
