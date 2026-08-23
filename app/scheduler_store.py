from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

from . import application as _application
from .database import db


SCHEDULER_TRIGGER = 'scheduler'


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _recent_scheduler_success(conn, actor_id: int, interval_minutes: int) -> dict | None:
    """Return the latest successful scheduler run still inside its quiet period."""
    interval = max(1, int(interval_minutes))
    row = conn.execute(
        '''SELECT id,run_no,status,actor_id,as_of,started_at,finished_at
           FROM job_runs
           WHERE trigger_source=? AND actor_id=? AND status='Succeeded'
           ORDER BY id DESC LIMIT 1''',
        (SCHEDULER_TRIGGER, actor_id),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    stamp = result.get('finished_at') or result.get('started_at')
    try:
        completed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    cutoff = datetime.now() - timedelta(minutes=interval)
    return result if completed >= cutoff else None


def run_scheduled_automation_once(
    conn,
    actor_id: int,
    interval_minutes: int | None = None,
):
    """Run one scheduler cycle with a database-backed cross-replica singleton gate.

    The system actor row is used only as a portable transactional mutex. On
    PostgreSQL the no-op update takes a row lock; on SQLite it participates in
    the database write transaction. A second replica therefore waits until the
    first cycle commits, then observes the recent successful scheduler run and
    returns an idempotent skip instead of executing the business payload again.

    Failed cycles are deliberately not suppression markers so another replica
    can provide immediate failover after the first transaction has rolled back.
    """
    interval = max(
        1,
        int(
            interval_minutes
            if interval_minutes is not None
            else (_application.AUTOMATION_INTERVAL_MINUTES or 1)
        ),
    )
    locked = conn.execute(
        'UPDATE users SET active=active WHERE id=?',
        (actor_id,),
    )
    if not _rowcount_one(locked):
        return {'status': 'Skipped', 'reason': 'scheduler_actor_missing'}

    recent = _recent_scheduler_success(conn, actor_id, interval)
    if recent:
        return {
            'status': 'Skipped',
            'reason': 'recent_scheduler_success',
            'run_no': recent['run_no'],
            'as_of': recent['as_of'],
        }

    # Resolve dynamically so the outbox post-commit hardening installed by the
    # composition layer remains in force for scheduled execution.
    return _application._execute_automation(
        conn,
        actor_id,
        SCHEDULER_TRIGGER,
    )


async def automation_loop_singleton() -> None:
    """Preserve the legacy cadence while suppressing duplicate replica runs."""
    delay = max(60, int(_application.AUTOMATION_INTERVAL_MINUTES) * 60)
    await asyncio.sleep(delay)
    while True:
        try:
            with db() as conn:
                system = conn.execute(
                    "SELECT id FROM users WHERE username='system'"
                ).fetchone()
                if system:
                    run_scheduled_automation_once(
                        conn,
                        int(system['id']),
                        int(_application.AUTOMATION_INTERVAL_MINUTES),
                    )
        except Exception:
            _application.logger.exception('EUAS scheduled automation run failed')
        await asyncio.sleep(delay)


def install_distributed_scheduler_singleton() -> None:
    """Install the singleton scheduler loop without changing public API routes."""
    app = _application.app
    marker = '_euas_distributed_scheduler_singleton'
    if getattr(app.state, marker, False):
        return

    _application._automation_loop = automation_loop_singleton
    main_module = sys.modules.get(f'{__package__}.main')
    if main_module is not None:
        setattr(main_module, '_automation_loop', automation_loop_singleton)

    setattr(app.state, marker, True)
