from __future__ import annotations

from apps.jobs import enqueue_job
from core.database import db, now

from .outbox import claim_outbox_event, deliver_claimed_outbox, recover_stuck_processing


def enqueue_outbox_dispatch_job(conn, event_id: int, *, max_attempts: int) -> dict | None:
    """Enqueue one specific eligible outbox attempt with a stable deduplication key."""
    stamp = now()
    recover_stuck_processing(conn, max_attempts=max_attempts, at=stamp)
    row = conn.execute(
        """SELECT * FROM event_outbox
           WHERE id=? AND status IN ('Pending','Failed') AND attempts<? AND available_at<=?""",
        (int(event_id), max_attempts, stamp),
    ).fetchone()
    if not row:
        return None
    event = dict(row)
    return enqueue_job(
        conn,
        job_type='event.dispatch',
        payload={'event_id': event['id'], 'event_no': event['event_no']},
        priority=50,
        max_attempts=max(2, int(max_attempts)),
        correlation_id=event.get('correlation_id') or None,
        deduplication_key=f"event.dispatch:{event['event_no']}:{event['attempts']}",
    )


def enqueue_outbox_dispatch_jobs(conn, *, max_attempts: int, limit: int = 100) -> dict:
    """Create at most one durable worker job for each currently eligible delivery attempt."""
    stamp = now()
    recovered = recover_stuck_processing(conn, max_attempts=max_attempts, at=stamp)
    rows = conn.execute(
        """SELECT * FROM event_outbox
           WHERE status IN ('Pending','Failed') AND attempts<? AND available_at<=?
           ORDER BY id LIMIT ?""",
        (max_attempts, stamp, max(1, min(int(limit), 1000))),
    ).fetchall()
    created = replayed = 0
    jobs = []
    for row in rows:
        event = dict(row)
        job = enqueue_outbox_dispatch_job(conn, event['id'], max_attempts=max_attempts)
        if not job:
            continue
        jobs.append(job)
        if job.get('idempotent_replay'):
            replayed += 1
        else:
            created += 1
    return {'created': created, 'existing': replayed, 'jobs': jobs, 'recovered': recovered}


def make_event_dispatch_handler(
    *,
    webhook_url: str,
    webhook_secret: str,
    max_attempts: int,
    app_version: str,
    retry_base_seconds: int = 30,
    urlopen=None,
    request_factory=None,
):
    """Return the real `event.dispatch` worker handler without registering fake global handlers."""
    kwargs = {}
    if urlopen is not None:
        kwargs['urlopen'] = urlopen
    if request_factory is not None:
        kwargs['request_factory'] = request_factory

    def handler(payload: dict, context):
        event_id = int(payload['event_id'])
        with db() as conn:
            claimed = claim_outbox_event(
                conn,
                event_id,
                worker_id=context.worker_id,
                max_attempts=max_attempts,
                lease_seconds=20,
            )
            if not claimed:
                row = conn.execute('SELECT status FROM event_outbox WHERE id=?', (event_id,)).fetchone()
                return {'status': row['status'] if row else 'Missing', 'idempotent': True}
            return deliver_claimed_outbox(
                conn,
                claimed,
                worker_id=context.worker_id,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
                max_attempts=max_attempts,
                app_version=app_version,
                retry_base_seconds=retry_base_seconds,
                **kwargs,
            )

    return handler
