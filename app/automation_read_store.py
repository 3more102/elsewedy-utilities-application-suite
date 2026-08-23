from __future__ import annotations

from datetime import date

from fastapi import Depends, Query

from . import application as _application
from .auth import require_roles
from .database import db


AUTOMATION_READ_ROLES = ('admin', 'maintenance_manager', 'executive')


def _one(cursor):
    row = cursor.fetchone()
    return dict(row) if row else None


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def automation_status(
    user=Depends(require_roles(*AUTOMATION_READ_ROLES)),
):
    """Return the established automation status read model unchanged."""
    with db() as conn:
        last = _one(
            conn.execute(
                '''SELECT jr.*,u.full_name actor_name
                   FROM job_runs jr LEFT JOIN users u ON u.id=jr.actor_id
                   ORDER BY jr.id DESC LIMIT 1'''
            )
        )
        pending = conn.execute(
            "SELECT COUNT(*) FROM approval_requests WHERE status='Pending'"
        ).fetchone()[0]
        due_pm = conn.execute(
            '''SELECT COUNT(*) FROM maintenance_plans
               WHERE active=1 AND trigger_type='Calendar'
                 AND next_due IS NOT NULL AND next_due<=?''',
            (date.today().isoformat(),),
        ).fetchone()[0]
        low = conn.execute(
            '''SELECT COUNT(*) FROM inventory_items
               WHERE current_stock-reserved_stock<=reorder_point'''
        ).fetchone()[0]
        overdue = conn.execute(
            '''SELECT COUNT(*) FROM work_orders
               WHERE target_finish IS NOT NULL AND target_finish<?
                 AND status NOT IN ('Completed','Closed','Cancelled')''',
            (date.today().isoformat(),),
        ).fetchone()[0]
        sla_breaches = conn.execute(
            '''SELECT COUNT(*) FROM work_order_sla s
               JOIN work_orders w ON w.id=s.work_order_id
               WHERE w.status NOT IN ('Completed','Closed','Cancelled')
                 AND (s.response_status='Breached' OR s.resolution_status='Breached')'''
        ).fetchone()[0]
        outbox_pending = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed')"
        ).fetchone()[0]
        outbox_exhausted = conn.execute(
            '''SELECT COUNT(*) FROM event_outbox
               WHERE status IN ('Pending','Failed') AND attempts>=?''',
            (_application.OUTBOX_MAX_ATTEMPTS,),
        ).fetchone()[0]

    return {
        'version': _application.APP_VERSION,
        'scheduler_enabled': _application.AUTOMATION_INTERVAL_MINUTES > 0,
        'interval_minutes': _application.AUTOMATION_INTERVAL_MINUTES,
        'webhook_configured': bool(_application.EVENT_WEBHOOK_URL),
        'last_run': last,
        'queue': {
            'due_pm': due_pm,
            'low_stock': low,
            'overdue_work': overdue,
            'pending_approvals': pending,
            'sla_breaches': sla_breaches,
            'outbox_pending': outbox_pending,
            'outbox_exhausted': outbox_exhausted,
        },
    }


def automation_runs(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(require_roles(*AUTOMATION_READ_ROLES)),
):
    """Return recent automation runs with the historical ordering and bound."""
    with db() as conn:
        return _rows(
            conn.execute(
                '''SELECT jr.*,u.full_name actor_name
                   FROM job_runs jr LEFT JOIN users u ON u.id=jr.actor_id
                   ORDER BY jr.id DESC LIMIT ?''',
                (limit,),
            )
        )


def install_automation_read_routes() -> None:
    """Move automation read models into a focused module without API drift."""
    app = _application.app
    marker = '_euas_automation_read_store'
    if getattr(app.state, marker, False):
        return

    replacements = {
        ('GET', '/api/automation/status'),
        ('GET', '/api/automation/runs'),
    }
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and method in set(getattr(route, 'methods', set()) or set())
            for method, path in replacements
        )
    ]

    app.get('/api/automation/status')(automation_status)
    app.get('/api/automation/runs')(automation_runs)

    _application.automation_status = automation_status
    _application.automation_runs = automation_runs
    setattr(app.state, marker, True)
