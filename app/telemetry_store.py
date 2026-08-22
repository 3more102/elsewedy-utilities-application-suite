from __future__ import annotations

import sys
from datetime import datetime, timezone

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


TELEMETRY_INGEST_ROLES = (
    'admin',
    'asset_manager',
    'maintenance_manager',
    'planner',
    'supervisor',
    'technician',
)


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _event_instant(value: str | datetime) -> datetime:
    """Normalize an ISO-8601 capture value to a comparable UTC-naive instant."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_temporally_current(
    captured_at: str,
    last_reading_at: str | None,
    *,
    allow_equal: bool = False,
) -> bool:
    """Decide whether a reading may advance the live channel generation.

    Explicit event timestamps use strict ordering: an equal instant is treated
    as replay/historical evidence and cannot duplicate alarm side effects.
    Readings that omit captured_at inherit server arrival time; because the
    historical ``now()`` format has second resolution, equal generated values
    retain legacy arrival-order behavior via ``allow_equal``.
    """
    if not last_reading_at:
        return True
    try:
        captured = _event_instant(captured_at)
        current = _event_instant(last_reading_at)
        return captured > current or (allow_equal and captured == current)
    except (TypeError, ValueError):
        # Incoming capture values are validated before this helper. If legacy
        # state contains an invalid timestamp, allow the valid incoming reading
        # to repair the channel's latest-state marker instead of freezing it.
        return True


def _lock_and_load_channel(conn, channel_code: str) -> dict:
    initial = conn.execute(
        'SELECT id FROM telemetry_channels WHERE channel_code=? AND active=1',
        (channel_code,),
    ).fetchone()
    if not initial:
        raise KeyError(f'Telemetry channel {channel_code} not found or inactive')

    # A no-op row update is portable across the EUAS adapters and serializes all
    # current-state mutations for one telemetry channel under PostgreSQL. This
    # gives event-time comparison a stable last_reading_at snapshot.
    locked = conn.execute(
        '''UPDATE telemetry_channels
           SET updated_at=updated_at
           WHERE id=? AND active=1''',
        (initial['id'],),
    )
    if not _rowcount_one(locked):
        raise KeyError(f'Telemetry channel {channel_code} not found or inactive')

    row = conn.execute(
        'SELECT * FROM telemetry_channels WHERE id=? AND active=1',
        (initial['id'],),
    ).fetchone()
    if not row:
        raise KeyError(f'Telemetry channel {channel_code} not found or inactive')
    return dict(row)


def ingest_telemetry_atomic(conn, body, user: dict) -> dict:
    """Persist every reading while advancing live state only by event time.

    Delayed or explicit equal-time readings remain queryable in
    telemetry_readings but cannot regress telemetry_channels.last_* or mutate
    active alarm state. Untimestamped readings preserve legacy arrival-order
    behavior. The per-channel row lock makes the invariant deterministic under
    concurrent PostgreSQL ingestion.
    """
    summary = {
        'accepted': 0,
        'historical': 0,
        'alarms_opened': 0,
        'alarms_updated': 0,
        'alarms_cleared': 0,
        'normal': 0,
        'results': [],
    }

    for reading in body.readings:
        channel_code = reading.channel_code.strip().upper()
        explicit_capture = reading.captured_at is not None
        captured = reading.captured_at or now()
        try:
            _event_instant(captured)
        except (TypeError, ValueError):
            raise HTTPException(
                400,
                f'Invalid captured_at for telemetry channel {channel_code}',
            )

        channel = _lock_and_load_channel(conn, channel_code)
        source = reading.source or channel['source_system'] or 'Manual'
        conn.execute(
            '''INSERT INTO telemetry_readings(
                 channel_id,value,quality,source,captured_at,ingested_at,ingested_by
               ) VALUES(?,?,?,?,?,?,?)''',
            (
                channel['id'],
                reading.value,
                reading.quality,
                source,
                captured,
                now(),
                user['id'],
            ),
        )
        summary['accepted'] += 1

        if not _is_temporally_current(
            captured,
            channel.get('last_reading_at'),
            allow_equal=not explicit_capture,
        ):
            summary['historical'] += 1
            summary['results'].append(
                {
                    'channel_code': channel['channel_code'],
                    'value': reading.value,
                    'action': 'historical',
                    'alarm_id': None,
                    'alarm_no': None,
                    'severity': None,
                    'current_at': channel.get('last_reading_at'),
                }
            )
            continue

        conn.execute(
            '''UPDATE telemetry_channels
               SET last_value=?,last_quality=?,last_reading_at=?,updated_at=?
               WHERE id=?''',
            (
                reading.value,
                reading.quality,
                captured,
                now(),
                channel['id'],
            ),
        )
        result = _application._evaluate_telemetry_alarm(
            conn,
            channel,
            float(reading.value),
            captured,
            user['id'],
        )
        summary['results'].append(
            {
                'channel_code': channel['channel_code'],
                'value': reading.value,
                **result,
            }
        )
        if result['action'] in ('opened', 'updated', 'cleared'):
            summary['alarms_' + result['action']] += 1
        else:
            summary['normal'] += 1

    _application.emit_event(
        conn,
        'operations.telemetry.ingested',
        'telemetry',
        'batch',
        {
            'accepted': summary['accepted'],
            'historical': summary['historical'],
            'alarms_opened': summary['alarms_opened'],
            'alarms_updated': summary['alarms_updated'],
            'alarms_cleared': summary['alarms_cleared'],
        },
    )
    append_audit(
        conn,
        user['id'],
        'INGEST TELEMETRY',
        'Utilities Operations',
        'batch',
        '',
        {
            'accepted': summary['accepted'],
            'historical': summary['historical'],
            'alarms_opened': summary['alarms_opened'],
            'alarms_cleared': summary['alarms_cleared'],
        },
    )
    return summary


def install_telemetry_temporal_ordering() -> None:
    app = _application.app
    marker = '_euas_telemetry_temporal_ordering'
    if getattr(app.state, marker, False):
        return

    path = '/api/telemetry/ingest'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def ingest_telemetry_route(
        body: _application.TelemetryIngestIn,
        user=Depends(require_roles(*TELEMETRY_INGEST_ROLES)),
    ):
        try:
            with db() as conn:
                return ingest_telemetry_atomic(conn, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))

    _application.ingest_telemetry = ingest_telemetry_route
    main_module = sys.modules.get(f'{__package__}.main')
    if main_module is not None:
        setattr(main_module, 'ingest_telemetry', ingest_telemetry_route)
    app.openapi_schema = None
    setattr(app.state, marker, True)
