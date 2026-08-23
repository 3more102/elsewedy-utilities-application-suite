from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from urllib import request as urllib_request

from core.database import now


def emit_event(conn, event_type: str, aggregate_type: str, aggregate_id, payload: dict) -> dict:
    """Append a durable event to the existing transactional outbox."""
    event_no = 'EVT-' + uuid.uuid4().hex[:16].upper()
    cur = conn.execute(
        """INSERT INTO event_outbox(
               event_no,event_type,aggregate_type,aggregate_id,payload_json,status,created_at
           ) VALUES(?,?,?,?,?,'Pending',?)""",
        (
            event_no,
            event_type,
            aggregate_type,
            str(aggregate_id),
            json.dumps(payload, ensure_ascii=False, default=str),
            now(),
        ),
    )
    return {'id': cur.lastrowid, 'event_no': event_no}


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
    """Deliver retryable outbox rows and move exhausted failures to DeadLetter."""
    terminal_at = now()
    dead_lettered = conn.execute(
        "UPDATE event_outbox SET status='DeadLetter',processed_at=COALESCE(processed_at,?) "
        "WHERE status='Failed' AND attempts>=?",
        (terminal_at, max_attempts),
    ).rowcount
    items = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM event_outbox WHERE status IN ('Pending','Failed') "
            "AND attempts<? ORDER BY id LIMIT 100",
            (max_attempts,),
        ).fetchall()
    ]
    delivered = failed = skipped = 0
    for item in items:
        attempts = item['attempts'] + 1
        if not webhook_url:
            conn.execute(
                "UPDATE event_outbox SET status='Skipped',attempts=?,processed_at=?,"
                "last_error='Webhook not configured' WHERE id=?",
                (attempts, now(), item['id']),
            )
            skipped += 1
            continue

        body = json.dumps(
            {
                'event_no': item['event_no'],
                'event_type': item['event_type'],
                'aggregate_type': item['aggregate_type'],
                'aggregate_id': item['aggregate_id'],
                'payload': json.loads(item['payload_json']),
                'created_at': item['created_at'],
            },
            ensure_ascii=False,
        ).encode()
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'EUAS/' + app_version,
            'X-EUAS-Event': item['event_type'],
            'X-EUAS-Event-ID': item['event_no'],
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
            conn.execute(
                "UPDATE event_outbox SET status='Delivered',attempts=?,processed_at=?,last_error='' WHERE id=?",
                (attempts, now(), item['id']),
            )
            delivered += 1
        except Exception as exc:
            error = str(exc)[:500]
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE event_outbox SET status='DeadLetter',attempts=?,processed_at=?,last_error=? WHERE id=?",
                    (attempts, now(), error, item['id']),
                )
                dead_lettered += 1
            else:
                conn.execute(
                    "UPDATE event_outbox SET status='Failed',attempts=?,processed_at=NULL,last_error=? WHERE id=?",
                    (attempts, error, item['id']),
                )
                failed += 1
    return {
        'delivered': delivered,
        'failed': failed,
        'dead_lettered': dead_lettered,
        'skipped': skipped,
        'processed': delivered + failed + dead_lettered + skipped,
    }


def rearm_outbox_event(conn, event_id: int) -> dict | None:
    """Reset any outbox row for an explicit operator retry."""
    row = conn.execute('SELECT * FROM event_outbox WHERE id=?', (event_id,)).fetchone()
    if not row:
        return None
    event = dict(row)
    conn.execute(
        "UPDATE event_outbox SET status='Pending',attempts=0,processed_at=NULL,last_error='' WHERE id=?",
        (event_id,),
    )
    return event
