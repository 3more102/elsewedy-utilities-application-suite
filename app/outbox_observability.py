from __future__ import annotations

from datetime import datetime

from fastapi import Depends

from . import application as _application
from .auth import require_permissions, require_roles
from .config import OUTBOX_LEASE_SECONDS
from .database import db, now
from .outbox_store import _lease_cutoff


OUTBOX_STATUS_ROLES = ('admin', 'maintenance_manager', 'executive')
OUTBOX_STATUS_PERMISSION = 'observability.metrics.read'
_legacy_metrics = _application.metrics
_OUTBOX_METRIC_PREFIXES = (
    'euas_outbox_retryable ',
    'euas_outbox_queued ',
    'euas_outbox_failed_retryable ',
    'euas_outbox_active_leases ',
    'euas_outbox_stale_leases ',
    'euas_outbox_unresolved ',
    'euas_outbox_oldest_retryable_age_seconds ',
)


def _age_seconds(created_at: str | None, as_of: str) -> int | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at))
        current = datetime.fromisoformat(str(as_of))
        if created.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=created.tzinfo)
        elif created.tzinfo is None and current.tzinfo is not None:
            created = created.replace(tzinfo=current.tzinfo)
        return max(0, int((current - created).total_seconds()))
    except (TypeError, ValueError):
        return None


def outbox_operational_snapshot(conn) -> dict:
    """Return payload-free operator health for the durable event backlog."""
    cutoff = _lease_cutoff()
    max_attempts = int(_application.OUTBOX_MAX_ATTEMPTS)
    as_of = now()

    row = conn.execute(
        '''SELECT
             COUNT(*) AS total,
             SUM(CASE WHEN status='Pending' AND processed_at IS NULL
                       AND attempts<? THEN 1 ELSE 0 END) AS queued,
             SUM(CASE WHEN status='Failed' AND attempts<?
                      THEN 1 ELSE 0 END) AS failed_retryable,
             SUM(CASE WHEN status='Pending' AND processed_at>?
                      THEN 1 ELSE 0 END) AS active_leases,
             SUM(CASE WHEN status='Pending' AND processed_at IS NOT NULL
                       AND processed_at<=? AND attempts<?
                      THEN 1 ELSE 0 END) AS stale_leases,
             SUM(CASE WHEN status IN ('Pending','Failed') AND attempts>=?
                      THEN 1 ELSE 0 END) AS exhausted,
             SUM(CASE WHEN status IN ('Pending','Failed')
                      THEN 1 ELSE 0 END) AS unresolved,
             SUM(CASE WHEN status='Delivered' THEN 1 ELSE 0 END) AS delivered,
             SUM(CASE WHEN status='Skipped' THEN 1 ELSE 0 END) AS skipped
           FROM event_outbox''',
        (max_attempts, max_attempts, cutoff, cutoff, max_attempts, max_attempts),
    ).fetchone()
    counts = dict(row) if row else {}

    oldest_retryable = conn.execute(
        '''SELECT MIN(created_at) AS created_at
           FROM event_outbox
           WHERE attempts<? AND (
             status='Failed'
             OR (status='Pending' AND (processed_at IS NULL OR processed_at<=?))
           )''',
        (max_attempts, cutoff),
    ).fetchone()
    oldest_unresolved = conn.execute(
        '''SELECT MIN(created_at) AS created_at
           FROM event_outbox WHERE status IN ('Pending','Failed')'''
    ).fetchone()

    retryable = (
        int(counts.get('queued') or 0)
        + int(counts.get('failed_retryable') or 0)
        + int(counts.get('stale_leases') or 0)
    )
    oldest_retryable_at = (
        oldest_retryable['created_at'] if oldest_retryable else None
    )
    oldest_unresolved_at = (
        oldest_unresolved['created_at'] if oldest_unresolved else None
    )

    return {
        'as_of': as_of,
        'config': {
            'max_attempts': max_attempts,
            'lease_seconds': int(OUTBOX_LEASE_SECONDS),
            'webhook_configured': bool(_application.EVENT_WEBHOOK_URL),
        },
        'queue': {
            'retryable': retryable,
            'queued': int(counts.get('queued') or 0),
            'failed_retryable': int(counts.get('failed_retryable') or 0),
            'active_leases': int(counts.get('active_leases') or 0),
            'stale_leases': int(counts.get('stale_leases') or 0),
            'exhausted': int(counts.get('exhausted') or 0),
            'unresolved': int(counts.get('unresolved') or 0),
        },
        'terminal': {
            'delivered': int(counts.get('delivered') or 0),
            'skipped': int(counts.get('skipped') or 0),
        },
        'total_events': int(counts.get('total') or 0),
        'oldest_retryable_created_at': oldest_retryable_at,
        'oldest_retryable_age_seconds': _age_seconds(oldest_retryable_at, as_of),
        'oldest_unresolved_created_at': oldest_unresolved_at,
        'oldest_unresolved_age_seconds': _age_seconds(oldest_unresolved_at, as_of),
    }


def metrics_with_outbox_observability(user) -> str:
    """Append lease/backlog gauges to the established Prometheus text surface."""
    base = str(_legacy_metrics(user))
    with db() as conn:
        snapshot = outbox_operational_snapshot(conn)

    lines = [
        line
        for line in base.splitlines()
        if not line.startswith(_OUTBOX_METRIC_PREFIXES)
    ]
    queue = snapshot['queue']
    oldest_age = int(snapshot['oldest_retryable_age_seconds'] or 0)
    lines.extend(
        [
            f"euas_outbox_retryable {queue['retryable']}",
            f"euas_outbox_queued {queue['queued']}",
            f"euas_outbox_failed_retryable {queue['failed_retryable']}",
            f"euas_outbox_active_leases {queue['active_leases']}",
            f"euas_outbox_stale_leases {queue['stale_leases']}",
            f"euas_outbox_unresolved {queue['unresolved']}",
            f'euas_outbox_oldest_retryable_age_seconds {oldest_age}',
        ]
    )
    return '\n'.join(lines) + '\n'


def install_outbox_observability() -> None:
    """Install payload-free operator status and metrics augmentation."""
    app = _application.app
    marker = '_euas_outbox_observability'
    if getattr(app.state, marker, False):
        return

    # app.main imports the production extension composition before capturing its
    # legacy metrics callable. Replacing this application global here makes the
    # hardened /api/metrics route wrap this augmentation without duplicating or
    # rewriting the security/session hardening in app.main.
    _application.metrics = metrics_with_outbox_observability

    path = '/api/events/outbox/status'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'GET' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.get(path)
    def outbox_status_route(
        _role_user=Depends(require_roles(*OUTBOX_STATUS_ROLES)),
        _capability_user=Depends(require_permissions(OUTBOX_STATUS_PERMISSION)),
    ):
        with db() as conn:
            return outbox_operational_snapshot(conn)

    _application.outbox_status = outbox_status_route
    setattr(app.state, marker, True)
