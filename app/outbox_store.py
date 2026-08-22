from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


OUTBOX_RETRY_ROLES = ('admin', 'maintenance_manager')


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _claim_delivery(conn, snapshot: dict) -> dict | None:
    """Claim one exact outbox generation before any external side effect.

    The selected status+attempt pair is part of the claim. A concurrent worker
    that selected the same stale row blocks on PostgreSQL and then fails the
    predicate after the first worker commits, including when that first attempt
    failed and advanced the attempt generation.
    """
    claimed = conn.execute(
        '''UPDATE event_outbox
           SET attempts=attempts+1
           WHERE id=? AND status=? AND attempts=? AND attempts<?''',
        (
            snapshot['id'],
            snapshot['status'],
            snapshot['attempts'],
            _application.OUTBOX_MAX_ATTEMPTS,
        ),
    )
    if not _rowcount_one(claimed):
        return None
    row = conn.execute(
        'SELECT * FROM event_outbox WHERE id=?',
        (snapshot['id'],),
    ).fetchone()
    return dict(row) if row else None


def process_outbox_atomic(conn) -> dict:
    """Deliver eligible outbox events with one concurrent sender per generation.

    This prevents duplicate sends caused by simultaneous automation workers. It
    intentionally retains at-least-once crash semantics: if the process dies
    after the remote endpoint accepts the POST but before the database commits,
    the stable X-EUAS-Event-ID remains the receiver-side deduplication key.
    """
    items = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM event_outbox
               WHERE status IN ('Pending','Failed') AND attempts<?
               ORDER BY id LIMIT 100''',
            (_application.OUTBOX_MAX_ATTEMPTS,),
        ).fetchall()
    ]
    delivered = failed = skipped = 0

    for snapshot in items:
        item = _claim_delivery(conn, snapshot)
        if item is None:
            continue

        attempts = int(item['attempts'])
        if not _application.EVENT_WEBHOOK_URL:
            conn.execute(
                '''UPDATE event_outbox
                   SET status='Skipped',processed_at=?,last_error='Webhook not configured'
                   WHERE id=? AND attempts=?''',
                (now(), item['id'], attempts),
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
            'User-Agent': 'EUAS/' + _application.APP_VERSION,
            'X-EUAS-Event': item['event_type'],
            'X-EUAS-Event-ID': item['event_no'],
        }
        if _application.EVENT_WEBHOOK_SECRET:
            headers['X-EUAS-Signature'] = 'sha256=' + hmac.new(
                _application.EVENT_WEBHOOK_SECRET.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()

        try:
            request = _application.urllib_request.Request(
                _application.EVENT_WEBHOOK_URL,
                data=body,
                headers=headers,
                method='POST',
            )
            with _application.urllib_request.urlopen(request, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f'Webhook HTTP {response.status}')
            conn.execute(
                '''UPDATE event_outbox
                   SET status='Delivered',processed_at=?,last_error=''
                   WHERE id=? AND attempts=?''',
                (now(), item['id'], attempts),
            )
            delivered += 1
        except Exception as exc:
            conn.execute(
                '''UPDATE event_outbox
                   SET status='Failed',processed_at=NULL,last_error=?
                   WHERE id=? AND attempts=?''',
                (str(exc)[:500], item['id'], attempts),
            )
            failed += 1

    return {'delivered': delivered, 'failed': failed, 'skipped': skipped}


def retry_outbox_event_atomic(conn, event_id: int, user: dict) -> dict:
    """Requeue one stable event generation without racing an in-flight sender."""
    initial_row = conn.execute(
        'SELECT * FROM event_outbox WHERE id=?',
        (event_id,),
    ).fetchone()
    if not initial_row:
        raise KeyError('Outbox event not found')
    initial = dict(initial_row)

    # Obtain the same row lock used by delivery mutation. If a sender is already
    # in flight this waits for it; afterwards generation comparison below makes
    # the stale retry a no-op instead of re-queuing a just-delivered event.
    locked = conn.execute(
        'UPDATE event_outbox SET status=status WHERE id=?',
        (event_id,),
    )
    if not _rowcount_one(locked):
        raise KeyError('Outbox event not found')
    fresh_row = conn.execute(
        'SELECT * FROM event_outbox WHERE id=?',
        (event_id,),
    ).fetchone()
    if not fresh_row:
        raise KeyError('Outbox event not found')
    fresh = dict(fresh_row)

    generation_changed = (
        fresh['status'] != initial['status']
        or int(fresh['attempts']) != int(initial['attempts'])
    )
    if generation_changed or fresh['status'] == 'Pending':
        return {'ok': True, 'event_no': fresh['event_no']}

    changed = conn.execute(
        '''UPDATE event_outbox
           SET status='Pending',processed_at=NULL,last_error=''
           WHERE id=? AND status=? AND attempts=?''',
        (event_id, fresh['status'], fresh['attempts']),
    )
    if not _rowcount_one(changed):
        return {'ok': True, 'event_no': fresh['event_no']}

    append_audit(
        conn,
        user['id'],
        'RETRY',
        'Integration Events',
        fresh['event_no'],
        fresh['status'],
        'Pending',
    )
    return {'ok': True, 'event_no': fresh['event_no']}


def install_outbox_atomicity() -> None:
    """Install the atomic processor and preserve the historical retry API."""
    app = _application.app
    marker = '_euas_outbox_delivery_atomicity'
    if getattr(app.state, marker, False):
        return

    _application._process_outbox = process_outbox_atomic

    path = '/api/events/outbox/{event_id}/retry'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def retry_outbox_event_route(
        event_id: int,
        user=Depends(require_roles(*OUTBOX_RETRY_ROLES)),
    ):
        try:
            with db() as conn:
                return retry_outbox_event_atomic(conn, event_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))

    _application.retry_outbox_event = retry_outbox_event_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
