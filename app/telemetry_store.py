from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone

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

ACTIVE_ALARM_STATUSES = ('Open', 'Acknowledged')


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _event_instant(value: str | datetime) -> datetime:
    """Normalize ISO-8601 input to a comparable UTC-naive instant.

    EUAS historically stores ``now()`` values without an offset. For backwards
    compatibility those naive values are interpreted in the server's local
    timezone; offset-aware SCADA/client timestamps are converted directly to
    UTC. Timezone-equivalent instants therefore compare equal regardless of the
    offset they were written with.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = datetime.fromisoformat(text)

    if parsed.tzinfo is None:
        # ``astimezone()`` on a naive datetime applies the host's local timezone
        # for that event date, including the correct historical DST offset.
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


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


def _implicit_capture_after(last_reading_at: str | None) -> str:
    """Return the server arrival marker for one untimestamped reading.

    Called only while the channel row lock is held so concurrent legacy callers
    are serialized by the database rather than by request-start timing. Equal
    second-level markers stay live generations (historical arrival-order
    behavior). When the stored marker is ahead of the host clock — for example
    after a future-dated explicit sample — synthesize a marker one microsecond
    past it instead of silently classifying every untimestamped reading as
    historical until the clock catches up.
    """
    candidate = now()
    if not last_reading_at:
        return candidate
    try:
        if _event_instant(candidate) >= _event_instant(last_reading_at):
            return candidate
        target_utc = (
            _event_instant(last_reading_at).replace(tzinfo=timezone.utc)
            + timedelta(microseconds=1)
        )
        return (
            target_utc.astimezone()
            .replace(tzinfo=None)
            .isoformat(timespec='microseconds')
        )
    except (TypeError, ValueError):
        return candidate


def _resolve_and_lock_channels(conn, channel_codes: list[str]) -> dict[str, int]:
    """Lock all channels for one batch in canonical ID order.

    Transactions retain PostgreSQL row locks until commit. Pre-locking every
    distinct channel by stable numeric ID prevents A/B versus B/A batch-order
    deadlocks while preserving the caller's original result order.
    """
    ids: dict[str, int] = {}
    for code in sorted(set(channel_codes)):
        code = code.strip().upper()
        row = conn.execute(
            'SELECT id FROM telemetry_channels WHERE channel_code=? AND active=1',
            (code,),
        ).fetchone()
        if not row:
            raise KeyError(f'Telemetry channel {code} not found or inactive')
        ids[code] = int(row['id'])

    for channel_id in sorted(set(ids.values())):
        locked = conn.execute(
            '''UPDATE telemetry_channels
               SET updated_at=updated_at
               WHERE id=? AND active=1''',
            (channel_id,),
        )
        if not _rowcount_one(locked):
            raise KeyError(f'Telemetry channel id {channel_id} not found or inactive')
    return ids


def _load_channel(conn, channel_id: int) -> dict:
    row = conn.execute(
        'SELECT * FROM telemetry_channels WHERE id=? AND active=1',
        (channel_id,),
    ).fetchone()
    if not row:
        raise KeyError(f'Telemetry channel id {channel_id} not found or inactive')
    return dict(row)


def _active_alarm(conn, channel_id: int) -> dict | None:
    row = conn.execute(
        """SELECT * FROM operational_alarms
           WHERE channel_id=? AND status IN ('Open','Acknowledged')
           ORDER BY id DESC LIMIT 1""",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None


def _lock_and_reload_alarm(conn, alarm_id: int) -> dict | None:
    """Serialize against manual acknowledge/close and reload fresh status."""
    locked = conn.execute(
        'UPDATE operational_alarms SET status=status WHERE id=?',
        (alarm_id,),
    )
    if not _rowcount_one(locked):
        return None
    row = conn.execute(
        'SELECT * FROM operational_alarms WHERE id=?',
        (alarm_id,),
    ).fetchone()
    return dict(row) if row else None


def evaluate_telemetry_alarm_atomic(
    conn,
    channel: dict,
    value: float,
    captured_at: str,
    actor_id: int | None,
) -> dict:
    """Apply threshold state without regressing manual alarm lifecycle state.

    The channel row is already locked by the caller. If an active alarm exists,
    lock and reload it before mutation. A manual close that commits first is
    therefore observed as terminal; a normal sample cannot write Closed->Cleared.
    A fresh violating sample after a committed close opens a new alarm generation.
    """
    severity, threshold = _application._telemetry_alarm_level(channel, value)
    active = _active_alarm(conn, int(channel['id']))
    if active is not None:
        active = _lock_and_reload_alarm(conn, int(active['id']))
        if active is not None and active['status'] not in ACTIVE_ALARM_STATUSES:
            active = None

    site = _application._channel_site(conn, channel['asset_id'])
    unit = channel.get('unit') or ''

    if severity:
        message = f"{channel['name']} {severity.lower()}: {value:g} {unit}".strip()
        if active is not None:
            updated = conn.execute(
                '''UPDATE operational_alarms
                   SET severity=?,message=?,trigger_value=?,threshold_value=?,
                       last_seen_at=?,occurrence_count=occurrence_count+1
                   WHERE id=? AND status IN ('Open','Acknowledged')''',
                (
                    severity,
                    message,
                    value,
                    threshold,
                    captured_at,
                    active['id'],
                ),
            )
            if _rowcount_one(updated):
                return {
                    'action': 'updated',
                    'alarm_id': active['id'],
                    'alarm_no': active['alarm_no'],
                    'severity': severity,
                }
            active = None

        number = _application.next_no(
            conn,
            'operational_alarms',
            'alarm_no',
            'ALM-',
            50001,
        )
        created = conn.execute(
            """INSERT INTO operational_alarms(
                 alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,
                 message,trigger_value,threshold_value,opened_at,last_seen_at,
                 occurrence_count
               ) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)""",
            (
                number,
                channel['id'],
                channel['asset_id'],
                site.get('site_id'),
                severity,
                message,
                value,
                threshold,
                captured_at,
                captured_at,
            ),
        )
        _application.notify_once(
            conn,
            'Operational alarm',
            f'{number} — {message}',
            severity,
            None,
            'maintenance_manager',
            'operations',
            number,
        )
        _application.notify_once(
            conn,
            'Operational alarm',
            f'{number} — {message}',
            severity,
            None,
            'asset_manager',
            'operations',
            number,
        )
        _application.emit_event(
            conn,
            'operations.alarm.opened',
            'alarm',
            number,
            {
                'alarm_no': number,
                'channel_code': channel['channel_code'],
                'asset_id': channel['asset_id'],
                'severity': severity,
                'value': value,
                'threshold': threshold,
                'captured_at': captured_at,
            },
        )
        if actor_id:
            append_audit(
                conn,
                actor_id,
                'ALARM OPEN',
                'Utilities Operations',
                number,
                '',
                {
                    'channel': channel['channel_code'],
                    'severity': severity,
                    'value': value,
                    'threshold': threshold,
                },
            )
        return {
            'action': 'opened',
            'alarm_id': int(created.lastrowid),
            'alarm_no': number,
            'severity': severity,
        }

    if active is not None:
        cleared = conn.execute(
            """UPDATE operational_alarms
               SET status='Cleared',cleared_at=?,last_seen_at=?,trigger_value=?
               WHERE id=? AND status IN ('Open','Acknowledged')""",
            (captured_at, captured_at, value, active['id']),
        )
        if _rowcount_one(cleared):
            _application.emit_event(
                conn,
                'operations.alarm.cleared',
                'alarm',
                active['alarm_no'],
                {
                    'alarm_no': active['alarm_no'],
                    'channel_code': channel['channel_code'],
                    'asset_id': channel['asset_id'],
                    'value': value,
                    'captured_at': captured_at,
                },
            )
            if actor_id:
                append_audit(
                    conn,
                    actor_id,
                    'ALARM CLEAR',
                    'Utilities Operations',
                    active['alarm_no'],
                    active['status'],
                    'Cleared',
                )
            return {
                'action': 'cleared',
                'alarm_id': active['id'],
                'alarm_no': active['alarm_no'],
                'severity': active['severity'],
            }

    return {
        'action': 'normal',
        'alarm_id': None,
        'alarm_no': None,
        'severity': None,
    }


def ingest_telemetry_atomic(conn, body, user: dict) -> dict:
    """Persist every reading; advance live state only by event time.

    Delayed or explicit equal-time readings remain queryable in
    telemetry_readings but cannot regress telemetry_channels.last_* or mutate
    active alarm state. Untimestamped readings preserve legacy arrival-order
    behavior. All participating channels are locked in canonical ID order
    before any mutation, and telemetry alarm mutation coordinates with the
    manual lifecycle through locked reloads, keeping the invariants
    deterministic under concurrent PostgreSQL ingestion.
    """
    normalized: list[tuple[object, str, str | None]] = []
    for reading in body.readings:
        code = reading.channel_code.strip().upper()
        if not math.isfinite(float(reading.value)):
            # NaN compares false against every threshold, so a single garbage
            # sample would silently CLEAR an active alarm; +/-inf would open
            # alarms with unusable trigger values. Reject before any write.
            raise HTTPException(
                400,
                f'Non-finite value for telemetry channel {code}',
            )
        explicit_capture = reading.captured_at
        if explicit_capture is not None:
            try:
                _event_instant(explicit_capture)
            except (TypeError, ValueError):
                raise HTTPException(
                    400,
                    f'Invalid captured_at for telemetry channel {code}',
                )
        normalized.append((reading, code, explicit_capture))

    channel_ids = _resolve_and_lock_channels(
        conn,
        [code for _reading, code, _captured in normalized],
    )
    summary = {
        'accepted': 0,
        'historical': 0,
        'alarms_opened': 0,
        'alarms_updated': 0,
        'alarms_cleared': 0,
        'normal': 0,
        'results': [],
    }

    for reading, code, explicit_capture in normalized:
        channel = _load_channel(conn, channel_ids[code])
        captured = (
            explicit_capture
            if explicit_capture is not None
            else _implicit_capture_after(channel.get('last_reading_at'))
        )
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
        result = evaluate_telemetry_alarm_atomic(
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


def install_telemetry_temporal_integrity() -> None:
    app = _application.app
    marker = '_euas_telemetry_temporal_integrity'
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
    _application._evaluate_telemetry_alarm = evaluate_telemetry_alarm_atomic
    main_module = sys.modules.get(f'{__package__}.main')
    if main_module is not None:
        setattr(main_module, 'ingest_telemetry', ingest_telemetry_route)
        setattr(main_module, '_evaluate_telemetry_alarm', evaluate_telemetry_alarm_atomic)
    app.openapi_schema = None
    setattr(app.state, marker, True)
