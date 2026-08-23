from __future__ import annotations

from datetime import datetime

from core.database import now

JOB_METRIC_STATES = ('Pending', 'Leased', 'Running', 'RetryScheduled', 'DeadLetter')


def _age_seconds(value: str | None, stamp: str) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, (datetime.fromisoformat(stamp) - datetime.fromisoformat(value)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def job_metrics_snapshot(conn) -> dict:
    stamp = now()
    counts = {state: 0 for state in JOB_METRIC_STATES}
    for row in conn.execute(
        "SELECT status,COUNT(*) count FROM jobs WHERE status IN ('Pending','Leased','Running','RetryScheduled','DeadLetter') GROUP BY status"
    ).fetchall():
        counts[str(row['status'])] = int(row['count'])
    active_workers = [dict(row) for row in conn.execute("SELECT worker_id,last_heartbeat_at FROM workers WHERE status='Active'").fetchall()]
    heartbeat_ages = [_age_seconds(worker.get('last_heartbeat_at'), stamp) for worker in active_workers]
    total_attempts = int(conn.execute('SELECT COUNT(*) FROM job_attempts').fetchone()[0])
    failed_attempts = int(conn.execute("SELECT COUNT(*) FROM job_attempts WHERE status IN ('Failed','LeaseExpired')").fetchone()[0])
    return {
        'pending': counts['Pending'],
        'leased': counts['Leased'],
        'running': counts['Running'],
        'retry_scheduled': counts['RetryScheduled'],
        'dead_letter': counts['DeadLetter'],
        'active_workers': len(active_workers),
        'worker_heartbeat_age_seconds': max(heartbeat_ages, default=0.0),
        'execution_total': total_attempts,
        'execution_failure_total': failed_attempts,
    }


def job_metric_lines(snapshot: dict) -> list[str]:
    return [
        f"euas_jobs_pending {snapshot['pending']}",
        f"euas_jobs_leased {snapshot['leased']}",
        f"euas_jobs_running {snapshot['running']}",
        f"euas_jobs_retry_scheduled {snapshot['retry_scheduled']}",
        f"euas_jobs_dead_letter {snapshot['dead_letter']}",
        f"euas_worker_heartbeat_age_seconds {snapshot['worker_heartbeat_age_seconds']:.3f}",
        f"euas_job_execution_total {snapshot['execution_total']}",
        f"euas_job_execution_failure_total {snapshot['execution_failure_total']}",
    ]
