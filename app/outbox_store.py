from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .config import OUTBOX_LEASE_SECONDS
from .database import db, now


OUTBOX_RETRY_ROLES = ('admin', 'maintenance_manager')
_legacy_execute_automation = _application._execute_automation


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _lease_cutoff() -> str:
    return (datetime.now() - timedelta(seconds=OUTBOX_LEASE_SECONDS)).isoformat(
        timespec='seconds'
    )


def _lease_is_active(row: dict) -> bool:
    stamp = row.get('processed_at')
    return bool(stamp and str(stamp) > _lease_cutoff())


def _claim_delivery(conn, snapshot: dict) -> dict | None:
    """Durably lease one exact outbox generation before outbound I/O.

    ``attempts`` is the generation fence and ``processed_at`` is the lease token
    while a Pending event is in flight. The claim is committed before the
    network call so no database row lock is held across webhook latency.

    A stale lease may be reclaimed after ``OUTBOX_LEASE_SECONDS``. Reclaiming
    increments the generation; any older sender can still have produced the
    external side effect (at-least-once delivery), but its final database write
    is fenced out by the generation+lease predicate. Receivers continue to use
    the stable X-EUAS-Event-ID for deduplication.
    """
    status = str(snapshot.get('status') or '')
    if status not in ('Pending', 'Failed'):
        return None

    previous_lease = snapshot.get('processed_at')
    if status == 'Pending' and previous_lease and _lease_is_active(snapshot):
        return None

    claim_stamp = now()
    if status == 'Pending' and previous_lease is None:
        claimed = conn.execute(
            '''UPDATE event_outbox
               SET attempts=attempts+1,processed_at=?,last_error=''
               WHERE id=? AND status='Pending' AND attempts=?
                 AND processed_at IS NULL AND attempts<?''',
            (
                claim_stamp,
                snapshot['id'],
                snapshot['attempts'],
                _application.OUTBOX_MAX_ATTEMPTS,
            ),
        )
    elif status == 'Pending':
        claimed = conn.execute(
            '''UPDATE event_outbox
               SET attempts=attempts+1,processed_at=?,last_error=''
               WHERE id=? AND status='Pending' AND attempts=?
                 AND processed_at=? AND attempts<?''',
            (
                claim_stamp,
                snapshot['id'],
                snapshot['attempts'],
                previous_lease,
                _application.OUTBOX_MAX_ATTEMPTS,
            ),
        )
    else:
        claimed = conn.execute(
            '''UPDATE event_outbox
               SET status='Pending',attempts=attempts+1,processed_at=?,last_error=''
               WHERE id=? AND status='Failed' AND attempts=? AND attempts<?''',
            (
                claim_stamp,
                snapshot['id'],
                snapshot['attempts'],
                _application.OUTBOX_MAX_ATTEMPTS,
            ),
        )

    if not _rowcount_one(claimed):
        return None

    # Persist the lease before any external side effect. This is the key
    # difference from the old row-lock-across-network implementation.
    conn.commit()
    row = conn.execute(
        'SELECT * FROM event_outbox WHERE id=?',
        (snapshot['id'],),
    ).fetchone()
    return dict(row) if row else None


def _finalize_claim(
    conn,
    item: dict,
    *,
    status: str,
    processed_at: str | None,
    last_error: str,
) -> bool:
    """Finalize only the exact generation+lease owned by this sender."""
    changed = conn.execute(
        '''UPDATE event_outbox
           SET status=?,processed_at=?,last_error=?
           WHERE id=? AND status='Pending' AND attempts=? AND processed_at=?''',
        (
            status,
            processed_at,
            last_error,
            item['id'],
            item['attempts'],
            item['processed_at'],
        ),
    )
    if not _rowcount_one(changed):
        conn.rollback()
        return False
    conn.commit()
    return True


def process_outbox_atomic(conn) -> dict:
    """Deliver eligible committed outbox events with durable leased claims.

    Callers must enter this after the business transaction that created events
    has committed. The automation wrapper below enforces that boundary.

    Claims are committed before network I/O, so workers do not hold database
    locks for the webhook duration. Crash semantics remain intentionally
    at-least-once: after a stale lease is reclaimed, the stable X-EUAS-Event-ID
    remains the receiver-side deduplication key.
    """
    cutoff = _lease_cutoff()
    items = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM event_outbox
               WHERE attempts<? AND (
                 status='Failed'
                 OR (
                   status='Pending'
                   AND (processed_at IS NULL OR processed_at<=?)
                 )
               )
               ORDER BY id LIMIT 100''',
            (_application.OUTBOX_MAX_ATTEMPTS, cutoff),
        ).fetchall()
    ]
    delivered = failed = skipped = 0

    for snapshot in items:
        item = _claim_delivery(conn, snapshot)
        if item is None:
            continue

        if not _application.EVENT_WEBHOOK_URL:
            if _finalize_claim(
                conn,
                item,
                status='Skipped',
                processed_at=now(),
                last_error='Webhook not configured',
            ):
                skipped += 1
            continue

        try:
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

            request = _application.urllib_request.Request(
                _application.EVENT_WEBHOOK_URL,
                data=body,
                headers=headers,
                method='POST',
            )
            with _application.urllib_request.urlopen(request, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f'Webhook HTTP {response.status}')
            if _finalize_claim(
                conn,
                item,
                status='Delivered',
                processed_at=now(),
                last_error='',
            ):
                delivered += 1
        except Exception as exc:
            # A failure that spends the final attempt turns this event into a
            # poison message: nothing will retry it automatically. Alert
            # operators in the same transaction that records the failed
            # generation; notify_once keeps a single unread alert per event.
            exhausted = int(item['attempts']) >= int(_application.OUTBOX_MAX_ATTEMPTS)
            if exhausted:
                _application.notify_once(
                    conn,
                    'Outbox delivery exhausted',
                    (
                        f"{item['event_no']} ({item['event_type']}) failed after "
                        f"{item['attempts']} attempts: {str(exc)[:200]}"
                    ),
                    'Warning',
                    None,
                    'maintenance_manager',
                    'Integration Events',
                    f"outbox-exhausted:{item['event_no']}",
                )
            if _finalize_claim(
                conn,
                item,
                status='Failed',
                processed_at=None,
                last_error=str(exc)[:500],
            ):
                failed += 1

    return {'delivered': delivered, 'failed': failed, 'skipped': skipped}


def _defer_outbox(_conn) -> dict:
    """Legacy automation hook: never deliver before its payload commits."""
    return {'delivered': 0, 'failed': 0, 'skipped': 0}


def execute_automation_postcommit(
    conn,
    actor_id: int,
    trigger_source: str = 'manual',
    as_of: str | None = None,
):
    """Run business automation first, commit it, then dispatch committed events."""
    result = _legacy_execute_automation(conn, actor_id, trigger_source, as_of)
    if result.get('status') != 'Succeeded':
        return result

    # The historical executor has released its savepoint and recorded the
    # successful job result, but the outer db() context has not committed yet.
    # Commit here before any network call so every dispatched event references
    # durable business state.
    conn.commit()

    outbox = {'delivered': 0, 'failed': 0, 'skipped': 0}
    try:
        outbox = process_outbox_atomic(conn)
        summary = dict(result.get('summary') or {})
        summary['outbox_delivered'] = outbox['delivered']
        summary['outbox_failed'] = outbox['failed']
        summary['outbox_skipped'] = outbox['skipped']
        summary['outbox_delivery_phase'] = 'postcommit'
        conn.execute(
            'UPDATE job_runs SET summary_json=? WHERE id=?',
            (json.dumps(summary), result['id']),
        )
        # The historical RUN audit belongs to the committed business phase and
        # therefore records deferred zero counts. Append a second immutable audit
        # record with the actual post-commit dispatch result so job summary and
        # audit evidence remain reconcilable without rewriting prior audit rows.
        append_audit(
            conn,
            actor_id,
            'DISPATCH OUTBOX',
            'Integration Events',
            result['run_no'],
            '',
            outbox,
        )
        conn.commit()
        result = dict(result)
        result['summary'] = summary
    except Exception:
        # A processor/database failure must never retroactively mark committed
        # business automation as failed. Roll back only post-commit metadata;
        # durable outbox claims/finalizations already committed by the processor
        # remain correct and stale claims can be reclaimed by their lease.
        conn.rollback()
        _application.logger.exception('EUAS post-commit outbox delivery failed')
    return result


def retry_outbox_event_atomic(conn, event_id: int, user: dict) -> dict:
    """Requeue one stable event generation without racing an active lease."""
    initial_row = conn.execute(
        'SELECT * FROM event_outbox WHERE id=?',
        (event_id,),
    ).fetchone()
    if not initial_row:
        raise KeyError('Outbox event not found')
    initial = dict(initial_row)

    # Serialize competing operator retries and any claim that is still in its
    # short database mutation phase. Network I/O holds no row lock.
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
        or (fresh.get('processed_at') or '') != (initial.get('processed_at') or '')
    )
    if generation_changed or fresh['status'] == 'Delivered':
        return {'ok': True, 'event_no': fresh['event_no']}

    exhausted = int(fresh['attempts']) >= int(_application.OUTBOX_MAX_ATTEMPTS)

    # Pending+processed_at is an in-flight durable lease. Active leases are
    # idempotent on the retry surface; a stale lease may be explicitly broken by
    # an operator. Ordinary unleased Pending rows are already queued and remain
    # idempotent unless their attempt budget is exhausted.
    if fresh['status'] == 'Pending':
        if _lease_is_active(fresh):
            return {'ok': True, 'event_no': fresh['event_no']}
        if not fresh.get('processed_at') and not exhausted:
            return {'ok': True, 'event_no': fresh['event_no']}

    if exhausted:
        changed = conn.execute(
            '''UPDATE event_outbox
               SET status='Pending',attempts=0,processed_at=NULL,last_error=''
               WHERE id=? AND status=? AND attempts=?
                 AND COALESCE(processed_at,'')=COALESCE(?,'')''',
            (
                event_id,
                fresh['status'],
                fresh['attempts'],
                fresh.get('processed_at'),
            ),
        )
    else:
        changed = conn.execute(
            '''UPDATE event_outbox
               SET status='Pending',processed_at=NULL,last_error=''
               WHERE id=? AND status=? AND attempts=?
                 AND COALESCE(processed_at,'')=COALESCE(?,'')''',
            (
                event_id,
                fresh['status'],
                fresh['attempts'],
                fresh.get('processed_at'),
            ),
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
    """Install post-commit delivery and preserve the historical retry API."""
    app = _application.app
    marker = '_euas_outbox_delivery_atomicity'
    if getattr(app.state, marker, False):
        return

    # Legacy automation still calls this global internally. Deferring here and
    # replacing _execute_automation below guarantees no webhook occurs before
    # its business transaction commits.
    _application._process_outbox = _defer_outbox
    _application._execute_automation = execute_automation_postcommit

    # app.main copies application symbols before importing the compatibility
    # bootstrap that reaches this installer. Synchronize those two exports in
    # place so operational scripts importing app.main cannot bypass hardening.
    main_module = sys.modules.get(f'{__package__}.main')
    if main_module is not None:
        setattr(main_module, '_process_outbox', _defer_outbox)
        setattr(main_module, '_execute_automation', execute_automation_postcommit)

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