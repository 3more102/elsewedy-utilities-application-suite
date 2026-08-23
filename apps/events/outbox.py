from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from urllib import request as urllib_request

from core.configuration import DB_BACKEND
from core.correlation import correlation_id as make_correlation_id
from core.database import now


OUTBOX_ACTIVE_STATES = ('Processing',)
OUTBOX_CLAIMABLE_STATES = ('Pending', 'Failed')
OUTBOX_TERMINAL_STATES = ('Delivered', 'DeadLetter', 'Skipped')


def _future(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec='seconds')


def _row(cursor):
    value = cursor.fetchone()
    return dict(value) if value else None


def _record_attempt(conn, event: dict, worker_id: str, stamp: str) -> None:
    conn.execute(
        """INSERT INTO event_delivery_attempts(event_id,attempt_no,worker_id,status,started_at)
           VALUES(?,?,?,'Processing',?)""",
        (event['id'], event['current_attempt_no'], worker_id, stamp),
    )


def emit_event(
    conn,
    event_type: str,
    aggregate_type: str,
    aggregate_id,
    payload: dict,
    *,
    correlation_id: str | None = None,
) -> dict:
    """Append a durable event to the transactional outbox."""
    event_no = 'EVT-' + uuid.uuid4().hex[:16].upper()
    stamp = now()
    corr = make_correlation_id(correlation_id)
    cur = conn.execute(
        """INSERT INTO event_outbox(
               event_no,event_type,aggregate_type,aggregate_id,payload_json,status,created_at,available_at,correlation_id
           ) VALUES(?,?,?,?,?,'Pending',?,?,?)""",
        (
            event_no,
            str(event_type or '').strip(),
            str(aggregate_type or '').strip(),
            str(aggregate_id),
            json.dumps(payload, ensure_ascii=False, default=str),
            stamp,
            stamp,
            corr,
        ),
    )
    return {'id': cur.lastrowid, 'event_no': event_no, 'correlation_id': corr}


def normalize_exhausted_outbox(conn, *, max_attempts: int, at: str | None = None) -> int:
    stamp = at or now()
    return conn.execute(
        """UPDATE event_outbox
           SET status='DeadLetter',processed_at=COALESCE(processed_at,?),lease_owner=NULL,lease_expires_at=NULL
           WHERE status='Failed' AND attempts>=?""",
        (stamp, max_attempts),
    ).rowcount


def recover_stuck_processing(conn, *, max_attempts: int, at: str | None = None) -> dict:
    """Recover dispatcher crashes after a Processing lease expires."""
    stamp = at or now()
    stuck = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM event_outbox
               WHERE status='Processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
               ORDER BY id""",
            (stamp,),
        ).fetchall()
    ]
    failed = dead_lettered = 0
    for event in stuck:
        terminal = int(event['attempts']) >= int(max_attempts)
        status = 'DeadLetter' if terminal else 'Failed'
        updated = conn.execute(
            """UPDATE event_outbox
               SET status=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,last_error='Dispatcher lease expired',processed_at=?,updated_at=?
               WHERE id=? AND status='Processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
            (status, stamp, stamp if terminal else None, stamp, event['id'], stamp),
        )
        if updated.rowcount != 1:
            continue
        conn.execute(
            """UPDATE event_delivery_attempts
               SET status='LeaseExpired',finished_at=?,error_message='Dispatcher lease expired'
               WHERE event_id=? AND attempt_no=? AND status='Processing'""",
            (stamp, event['id'], event['current_attempt_no']),
        )
        if terminal:
            dead_lettered += 1
        else:
            failed += 1
    return {'recovered': len(stuck), 'failed': failed, 'dead_lettered': dead_lettered}


def _claim_selected(conn, event_id: int, *, worker_id: str, lease_seconds: int, stamp: str) -> dict | None:
    expires = _future(lease_seconds)
    if DB_BACKEND == 'postgresql':
        candidate = _row(
            conn.execute(
                """SELECT id FROM event_outbox
                   WHERE id=? AND status IN ('Pending','Failed') AND available_at<=?
                   LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (event_id, stamp),
            )
        )
        if not candidate:
            return None
        updated = conn.execute(
            """UPDATE event_outbox
               SET status='Processing',attempts=attempts+1,current_attempt_no=CASE WHEN current_attempt_no<attempts THEN attempts+1 ELSE current_attempt_no+1 END,lease_owner=?,lease_expires_at=?,processed_at=NULL,updated_at=?
               WHERE id=? AND status IN ('Pending','Failed')""",
            (worker_id, expires, stamp, event_id),
        )
        if updated.rowcount != 1:
            return None
        claimed = _row(conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)))
    else:
        claimed = _row(
            conn.execute(
                """UPDATE event_outbox
                   SET status='Processing',attempts=attempts+1,current_attempt_no=CASE WHEN current_attempt_no<attempts THEN attempts+1 ELSE current_attempt_no+1 END,lease_owner=?,lease_expires_at=?,processed_at=NULL,updated_at=?
                   WHERE id=? AND status IN ('Pending','Failed') AND available_at<=?
                   RETURNING *""",
                (worker_id, expires, stamp, event_id, stamp),
            )
        )
    if claimed:
        _record_attempt(conn, claimed, worker_id, stamp)
    return claimed


def claim_outbox_event(
    conn,
    event_id: int,
    *,
    worker_id: str,
    max_attempts: int,
    lease_seconds: int = 20,
) -> dict | None:
    stamp = now()
    recover_stuck_processing(conn, max_attempts=max_attempts, at=stamp)
    normalize_exhausted_outbox(conn, max_attempts=max_attempts, at=stamp)
    return _claim_selected(conn, int(event_id), worker_id=worker_id, lease_seconds=lease_seconds, stamp=stamp)


def claim_next_outbox_event(
    conn,
    *,
    worker_id: str,
    max_attempts: int,
    lease_seconds: int = 20,
) -> dict | None:
    stamp = now()
    recover_stuck_processing(conn, max_attempts=max_attempts, at=stamp)
    normalize_exhausted_outbox(conn, max_attempts=max_attempts, at=stamp)
    expires = _future(lease_seconds)
    if DB_BACKEND == 'postgresql':
        candidate = _row(
            conn.execute(
                """SELECT id FROM event_outbox
                   WHERE status IN ('Pending','Failed') AND attempts<? AND available_at<=?
                   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (max_attempts, stamp),
            )
        )
        if not candidate:
            return None
        return _claim_selected(
            conn,
            int(candidate['id']),
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            stamp=stamp,
        )
    claimed = _row(
        conn.execute(
            """UPDATE event_outbox
               SET status='Processing',attempts=attempts+1,current_attempt_no=CASE WHEN current_attempt_no<attempts THEN attempts+1 ELSE current_attempt_no+1 END,lease_owner=?,lease_expires_at=?,processed_at=NULL,updated_at=?
               WHERE id=(SELECT id FROM event_outbox
                         WHERE status IN ('Pending','Failed') AND attempts<? AND available_at<=?
                         ORDER BY id LIMIT 1)
               AND status IN ('Pending','Failed')
               RETURNING *""",
            (worker_id, expires, stamp, max_attempts, stamp),
        )
    )
    if claimed:
        _record_attempt(conn, claimed, worker_id, stamp)
    return claimed


def deliver_claimed_outbox(
    conn,
    event: dict,
    *,
    worker_id: str,
    webhook_url: str,
    webhook_secret: str,
    max_attempts: int,
    app_version: str,
    retry_base_seconds: int = 30,
    urlopen=urllib_request.urlopen,
    request_factory=urllib_request.Request,
) -> dict:
    """Deliver one already-leased outbox row and persist delivery evidence."""
    current = _row(conn.execute('SELECT * FROM event_outbox WHERE id=?', (event['id'],)))
    if not current or current['status'] != 'Processing' or current.get('lease_owner') != worker_id:
        return {'status': current['status'] if current else 'Missing', 'delivered': False, 'idempotent': True}

    stamp = now()
    if not webhook_url:
        conn.execute(
            """UPDATE event_outbox
               SET status='Skipped',processed_at=?,last_error='Webhook not configured',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
               WHERE id=?""",
            (stamp, stamp, current['id']),
        )
        conn.execute(
            """UPDATE event_delivery_attempts SET status='Skipped',finished_at=?,error_message='Webhook not configured'
               WHERE event_id=? AND attempt_no=? AND worker_id=?""",
            (stamp, current['id'], current['current_attempt_no'], worker_id),
        )
        return {'status': 'Skipped', 'delivered': False, 'idempotent': False}

    body = json.dumps(
        {
            'event_no': current['event_no'],
            'event_type': current['event_type'],
            'aggregate_type': current['aggregate_type'],
            'aggregate_id': current['aggregate_id'],
            'payload': json.loads(current['payload_json']),
            'created_at': current['created_at'],
            'correlation_id': current.get('correlation_id') or '',
        },
        ensure_ascii=False,
    ).encode()
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'EUAS/' + app_version,
        'X-EUAS-Event': current['event_type'],
        'X-EUAS-Event-ID': current['event_no'],
        'X-Correlation-ID': current.get('correlation_id') or '',
    }
    if webhook_secret:
        headers['X-EUAS-Signature'] = 'sha256=' + hmac.new(
            webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
    try:
        req = request_factory(webhook_url, data=body, headers=headers, method='POST')
        with urlopen(req, timeout=5) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f'Webhook HTTP {response.status}')
        stamp = now()
        conn.execute(
            """UPDATE event_outbox
               SET status='Delivered',processed_at=?,last_error='',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
               WHERE id=?""",
            (stamp, stamp, current['id']),
        )
        conn.execute(
            """UPDATE event_delivery_attempts SET status='Delivered',finished_at=?,error_message=''
               WHERE event_id=? AND attempt_no=? AND worker_id=?""",
            (stamp, current['id'], current['current_attempt_no'], worker_id),
        )
        return {'status': 'Delivered', 'delivered': True, 'idempotent': False}
    except Exception as exc:
        stamp = now()
        error = str(exc)[:500]
        terminal = int(current['attempts']) >= int(max_attempts)
        status = 'DeadLetter' if terminal else 'Failed'
        delay = min(max(0, int(retry_base_seconds)) * (2 ** max(0, int(current['attempts']) - 1)), 86400)
        available = stamp if terminal else _future(delay)
        conn.execute(
            """UPDATE event_outbox
               SET status=?,available_at=?,processed_at=?,last_error=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
               WHERE id=?""",
            (status, available, stamp if terminal else None, error, stamp, current['id']),
        )
        conn.execute(
            """UPDATE event_delivery_attempts SET status=?,finished_at=?,error_message=?
               WHERE event_id=? AND attempt_no=? AND worker_id=?""",
            (status, stamp, error, current['id'], current['current_attempt_no'], worker_id),
        )
        return {'status': status, 'delivered': False, 'idempotent': False, 'error': error}


def process_outbox(
    conn,
    *,
    webhook_url: str,
    webhook_secret: str,
    max_attempts: int,
    app_version: str,
    urlopen=urllib_request.urlopen,
    request_factory=urllib_request.Request,
) -> dict:
    """Compatibility dispatcher using the same lease-safe single-event primitive as workers."""
    dead_lettered = normalize_exhausted_outbox(conn, max_attempts=max_attempts)
    recovered = recover_stuck_processing(conn, max_attempts=max_attempts)
    dead_lettered += recovered['dead_lettered']
    delivered = failed = skipped = 0
    for _ in range(100):
        item = claim_next_outbox_event(
            conn,
            worker_id='inline-outbox-dispatcher',
            max_attempts=max_attempts,
            lease_seconds=20,
        )
        if not item:
            break
        result = deliver_claimed_outbox(
            conn,
            item,
            worker_id='inline-outbox-dispatcher',
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            max_attempts=max_attempts,
            app_version=app_version,
            urlopen=urlopen,
            request_factory=request_factory,
        )
        if result['status'] == 'Delivered':
            delivered += 1
        elif result['status'] == 'DeadLetter':
            dead_lettered += 1
        elif result['status'] == 'Skipped':
            skipped += 1
        elif result['status'] == 'Failed':
            failed += 1
    return {
        'delivered': delivered,
        'failed': failed,
        'dead_lettered': dead_lettered,
        'skipped': skipped,
        'processed': delivered + failed + dead_lettered + skipped,
    }


def rearm_outbox_event(conn, event_id: int) -> dict | None:
    """Reset any outbox row for an explicit operator retry and clear dispatcher ownership."""
    row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
    if not row:
        return None
    event = dict(row)
    stamp = now()
    conn.execute(
        """UPDATE event_outbox
           SET status='Pending',attempts=0,processed_at=NULL,last_error='',available_at=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
           WHERE id=?""",
        (stamp, stamp, event_id),
    )
    return event
