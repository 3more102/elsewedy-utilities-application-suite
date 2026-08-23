from __future__ import annotations

from collections.abc import Callable

from apps.audit import audit
from apps.events import emit_event
from core.database import now
from core.shared import next_no

from .validation import (
    TelemetryChannelNotFound,
    normalize_channel_code,
    normalize_measurement,
    normalize_quality,
    normalize_source,
    normalize_timestamp,
)

AlarmEvaluator = Callable[[object, dict, float, str, int | None], dict]
CbmEvaluator = Callable[[object, dict, float, str, int, int], list[dict]]


def ingest_batch(conn, body, *, actor_id: int, evaluate_alarm: AlarmEvaluator, evaluate_cbm: CbmEvaluator) -> dict:
    summary = {
        'accepted': 0,
        'duplicates': 0,
        'bad_quality': 0,
        'quality_ignored': 0,
        'suppressed': 0,
        'alarms_opened': 0,
        'alarms_updated': 0,
        'alarms_cleared': 0,
        'normal': 0,
        'cbm_events_opened': 0,
        'cbm_events_resolved': 0,
        'cbm_work_orders_created': 0,
        'results': [],
    }
    if body.idempotency_key:
        previous = conn.execute(
            'SELECT * FROM telemetry_ingest_batches WHERE idempotency_key=?',
            (body.idempotency_key,),
        ).fetchone()
        if previous:
            return {
                'batch_no': previous['batch_no'],
                'idempotent_replay': True,
                'accepted': previous['accepted_count'],
                'duplicates': previous['duplicate_count'],
                'bad_quality': previous['bad_quality_count'],
                'quality_ignored': 0,
                'suppressed': previous['suppressed_count'],
                'alarms_opened': previous['alarms_opened'],
                'alarms_updated': previous['alarms_updated'],
                'alarms_cleared': previous['alarms_cleared'],
                'cbm_events_opened': previous['cbm_events_opened'],
                'cbm_events_resolved': previous['cbm_events_resolved'],
                'cbm_work_orders_created': previous['cbm_work_orders_created'],
                'normal': 0,
                'results': [],
            }

    batch_no = next_no(conn, 'telemetry_ingest_batches', 'batch_no', 'TIB-', 70001)
    started_at = now()
    cur = conn.execute(
        """INSERT INTO telemetry_ingest_batches(batch_no,source_system,idempotency_key,received_count,ingested_by,started_at)
           VALUES(?,?,?,?,?,?)""",
        (batch_no, body.source_system or 'API', body.idempotency_key, len(body.readings), actor_id, started_at),
    )
    batch_id = cur.lastrowid

    for reading in body.readings:
        code = normalize_channel_code(reading.channel_code)
        channel_row = conn.execute(
            'SELECT * FROM telemetry_channels WHERE channel_code=? AND active=1',
            (code,),
        ).fetchone()
        if not channel_row:
            raise TelemetryChannelNotFound(f'Telemetry channel {reading.channel_code} not found or inactive')
        channel = dict(channel_row)
        captured = normalize_timestamp(reading.captured_at, fallback=now())
        source = normalize_source(reading.source, fallback=body.source_system or channel['source_system'] or 'Manual')
        quality = normalize_quality(reading.quality)
        value = normalize_measurement(reading.value)

        if reading.external_id:
            duplicate = conn.execute(
                'SELECT id FROM telemetry_readings WHERE channel_id=? AND external_id=?',
                (channel['id'], reading.external_id),
            ).fetchone()
            if duplicate:
                summary['duplicates'] += 1
                summary['results'].append(
                    {
                        'channel_code': code,
                        'value': value,
                        'action': 'duplicate',
                        'external_id': reading.external_id,
                    }
                )
                continue

        reading_cur = conn.execute(
            'INSERT INTO telemetry_readings(channel_id,value,quality,source,captured_at,ingested_at,ingested_by,external_id,batch_id) VALUES(?,?,?,?,?,?,?,?,?)',
            (channel['id'], value, quality, source, captured, now(), actor_id, reading.external_id, batch_id),
        )
        conn.execute(
            'UPDATE telemetry_channels SET last_value=?,last_quality=?,last_reading_at=?,updated_at=? WHERE id=?',
            (value, quality, captured, now(), channel['id']),
        )
        summary['accepted'] += 1
        cbm_results: list[dict] = []
        if quality != 'Good':
            if quality == 'Bad':
                summary['bad_quality'] += 1
            summary['quality_ignored'] += 1
            result = {
                'action': 'quality_ignored',
                'alarm_id': None,
                'alarm_no': None,
                'severity': None,
                'quality': quality,
            }
        else:
            result = evaluate_alarm(conn, channel, value, captured, actor_id)
            cbm_results = evaluate_cbm(conn, channel, value, captured, reading_cur.lastrowid, actor_id)
            summary['cbm_events_opened'] += sum(1 for item in cbm_results if item['action'] == 'opened')
            summary['cbm_events_resolved'] += sum(1 for item in cbm_results if item['action'] == 'resolved')
            summary['cbm_work_orders_created'] += sum(1 for item in cbm_results if item.get('work_order'))

        summary['results'].append(
            {
                'channel_code': channel['channel_code'],
                'value': value,
                'quality': quality,
                'external_id': reading.external_id,
                'cbm': cbm_results,
                **result,
            }
        )
        if result['action'] in ('opened', 'updated', 'cleared'):
            summary['alarms_' + result['action']] += 1
        elif result['action'] == 'suppressed':
            summary['suppressed'] += 1
        elif result['action'] == 'normal':
            summary['normal'] += 1

    conn.execute(
        """UPDATE telemetry_ingest_batches SET accepted_count=?,duplicate_count=?,bad_quality_count=?,alarms_opened=?,alarms_updated=?,alarms_cleared=?,suppressed_count=?,cbm_events_opened=?,cbm_events_resolved=?,cbm_work_orders_created=?,completed_at=? WHERE id=?""",
        (
            summary['accepted'],
            summary['duplicates'],
            summary['bad_quality'],
            summary['alarms_opened'],
            summary['alarms_updated'],
            summary['alarms_cleared'],
            summary['suppressed'],
            summary['cbm_events_opened'],
            summary['cbm_events_resolved'],
            summary['cbm_work_orders_created'],
            now(),
            batch_id,
        ),
    )
    emit_event(
        conn,
        'operations.telemetry.ingested',
        'telemetry',
        batch_no,
        {
            'batch_no': batch_no,
            'accepted': summary['accepted'],
            'duplicates': summary['duplicates'],
            'bad_quality': summary['bad_quality'],
            'suppressed': summary['suppressed'],
            'alarms_opened': summary['alarms_opened'],
            'alarms_updated': summary['alarms_updated'],
            'alarms_cleared': summary['alarms_cleared'],
            'cbm_events_opened': summary['cbm_events_opened'],
            'cbm_work_orders_created': summary['cbm_work_orders_created'],
        },
    )
    audit(
        conn,
        actor_id,
        'INGEST TELEMETRY',
        'Utilities Operations',
        batch_no,
        '',
        {
            'accepted': summary['accepted'],
            'duplicates': summary['duplicates'],
            'bad_quality': summary['bad_quality'],
            'suppressed': summary['suppressed'],
            'alarms_opened': summary['alarms_opened'],
            'alarms_cleared': summary['alarms_cleared'],
        },
    )
    return {'batch_no': batch_no, 'idempotent_replay': False, **summary}
