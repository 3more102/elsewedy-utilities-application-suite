from __future__ import annotations

from collections.abc import Callable

from apps.audit import audit
from apps.events import emit_event
from core.database import now
from core.shared import next_no

ACTIVE_ALARM_STATES = ('Open', 'Acknowledged')
TERMINAL_ALARM_STATES = ('Cleared', 'Closed')


class AlarmNotFound(LookupError):
    pass


class InvalidAlarmTransition(RuntimeError):
    pass


def channel_site(conn, asset_id: int) -> dict:
    row = conn.execute(
        'SELECT s.id site_id,s.site_code,s.name site_name FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?',
        (asset_id,),
    ).fetchone()
    return dict(row) if row else {'site_id': None, 'site_code': None, 'site_name': None}


def active_suppression(conn, channel: dict, captured_at: str):
    site = channel_site(conn, channel['asset_id'])
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT s.*,u.full_name created_by_name FROM alarm_suppressions s
               LEFT JOIN users u ON u.id=s.created_by
               WHERE s.active=1 AND s.start_at<=? AND s.end_at>=?
               AND ((s.channel_id=? ) OR (s.channel_id IS NULL AND s.asset_id=? )
                    OR (s.channel_id IS NULL AND s.asset_id IS NULL AND s.site_id=?))""",
            (captured_at, captured_at, channel['id'], channel['asset_id'], site.get('site_id')),
        ).fetchall()
    ]
    if not rows:
        return None
    rows.sort(
        key=lambda item: (
            1 if item.get('channel_id') else 0,
            1 if item.get('asset_id') else 0,
            1 if item.get('site_id') else 0,
        ),
        reverse=True,
    )
    return rows[0]


def telemetry_alarm_level(channel: dict, value: float):
    checks = [
        ('Critical', 'critical_high', 'high'),
        ('Critical', 'critical_low', 'low'),
        ('Warning', 'warning_high', 'high'),
        ('Warning', 'warning_low', 'low'),
    ]
    for severity, key, direction in checks:
        threshold = channel.get(key)
        if threshold is None:
            continue
        threshold = float(threshold)
        if direction == 'high' and value >= threshold:
            return severity, threshold
        if direction == 'low' and value <= threshold:
            return severity, threshold
    return None, None


def evaluate_telemetry_alarm(
    conn,
    channel: dict,
    value: float,
    captured_at: str,
    actor_id: int | None,
    *,
    correlate: Callable[[object, int, int | None], dict | None],
    refresh_incidents: Callable[[object, int, int | None], list[dict]],
    notify: Callable[..., object] | None = None,
):
    severity, threshold = telemetry_alarm_level(channel, value)
    active = conn.execute(
        "SELECT * FROM operational_alarms WHERE channel_id=? AND status IN ('Open','Acknowledged') ORDER BY id DESC LIMIT 1",
        (channel['id'],),
    ).fetchone()
    site = channel_site(conn, channel['asset_id'])
    unit = channel.get('unit') or ''
    if severity:
        suppression = active_suppression(conn, channel, captured_at)
        if suppression:
            return {
                'action': 'suppressed',
                'alarm_id': None,
                'alarm_no': None,
                'severity': severity,
                'threshold': threshold,
                'suppression_id': suppression['id'],
                'suppression_no': suppression['suppression_no'],
                'suppression_reason': suppression['reason'],
            }
        message = f"{channel['name']} {severity.lower()}: {value:g} {unit}".strip()
        if active:
            conn.execute(
                'UPDATE operational_alarms SET severity=?,message=?,trigger_value=?,threshold_value=?,last_seen_at=?,occurrence_count=occurrence_count+1 WHERE id=?',
                (severity, message, value, threshold, captured_at, active['id']),
            )
            incident = correlate(conn, active['id'], actor_id)
            return {
                'action': 'updated',
                'alarm_id': active['id'],
                'alarm_no': active['alarm_no'],
                'severity': severity,
                'incident_id': incident['id'] if incident else None,
                'incident_no': incident['incident_no'] if incident else None,
            }
        alarm_no = next_no(conn, 'operational_alarms', 'alarm_no', 'ALM-', 50001)
        cur = conn.execute(
            "INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)",
            (
                alarm_no,
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
        if notify is not None:
            notify(conn, 'Operational alarm', f"{alarm_no} — {message}", severity, None, 'maintenance_manager', 'operations', alarm_no)
            notify(conn, 'Operational alarm', f"{alarm_no} — {message}", severity, None, 'asset_manager', 'operations', alarm_no)
        emit_event(
            conn,
            'operations.alarm.opened',
            'alarm',
            alarm_no,
            {
                'alarm_no': alarm_no,
                'channel_code': channel['channel_code'],
                'asset_id': channel['asset_id'],
                'severity': severity,
                'value': value,
                'threshold': threshold,
                'captured_at': captured_at,
            },
        )
        if actor_id:
            audit(
                conn,
                actor_id,
                'ALARM OPEN',
                'Utilities Operations',
                alarm_no,
                '',
                {'channel': channel['channel_code'], 'severity': severity, 'value': value, 'threshold': threshold},
            )
        incident = correlate(conn, cur.lastrowid, actor_id)
        return {
            'action': 'opened',
            'alarm_id': cur.lastrowid,
            'alarm_no': alarm_no,
            'severity': severity,
            'incident_id': incident['id'] if incident else None,
            'incident_no': incident['incident_no'] if incident else None,
        }
    if active:
        conn.execute(
            "UPDATE operational_alarms SET status='Cleared',cleared_at=?,last_seen_at=?,trigger_value=? WHERE id=?",
            (captured_at, captured_at, value, active['id']),
        )
        emit_event(
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
            audit(conn, actor_id, 'ALARM CLEAR', 'Utilities Operations', active['alarm_no'], active['status'], 'Cleared')
        incidents = refresh_incidents(conn, active['id'], actor_id)
        return {
            'action': 'cleared',
            'alarm_id': active['id'],
            'alarm_no': active['alarm_no'],
            'severity': active['severity'],
            'incidents_resolved': [item['incident_no'] for item in incidents if item.get('status') == 'Resolved'],
        }
    return {'action': 'normal', 'alarm_id': None, 'alarm_no': None, 'severity': None}


def acknowledge_alarm(conn, alarm_id: int, actor_id: int, *, correlate: Callable[[object, int, int | None], dict | None]):
    row = conn.execute('SELECT * FROM operational_alarms WHERE id=?', (alarm_id,)).fetchone()
    if not row:
        raise AlarmNotFound('Alarm not found')
    alarm = dict(row)
    if alarm['status'] == 'Acknowledged':
        return {'ok': True, 'status': 'Acknowledged', 'idempotent': True}
    if alarm['status'] != 'Open':
        raise InvalidAlarmTransition(f"Alarm is {alarm['status']}")
    conn.execute(
        "UPDATE operational_alarms SET status='Acknowledged',acknowledged_at=?,acknowledged_by=? WHERE id=?",
        (now(), actor_id, alarm_id),
    )
    correlate(conn, alarm_id, actor_id)
    emit_event(conn, 'operations.alarm.acknowledged', 'alarm', alarm['alarm_no'], {'alarm_no': alarm['alarm_no'], 'asset_id': alarm['asset_id']})
    audit(conn, actor_id, 'ACKNOWLEDGE ALARM', 'Utilities Operations', alarm['alarm_no'], alarm['status'], 'Acknowledged')
    return {'ok': True, 'status': 'Acknowledged', 'idempotent': False}


def close_alarm(conn, alarm_id: int, actor_id: int, *, refresh_incidents: Callable[[object, int, int | None], list[dict]]):
    row = conn.execute('SELECT * FROM operational_alarms WHERE id=?', (alarm_id,)).fetchone()
    if not row:
        raise AlarmNotFound('Alarm not found')
    alarm = dict(row)
    if alarm['status'] == 'Closed':
        return {'ok': True, 'status': 'Closed', 'idempotent': True}
    conn.execute(
        "UPDATE operational_alarms SET status='Closed',closed_at=?,closed_by=? WHERE id=?",
        (now(), actor_id, alarm_id),
    )
    refresh_incidents(conn, alarm_id, actor_id)
    emit_event(conn, 'operations.alarm.closed', 'alarm', alarm['alarm_no'], {'alarm_no': alarm['alarm_no'], 'asset_id': alarm['asset_id']})
    audit(conn, actor_id, 'CLOSE ALARM', 'Utilities Operations', alarm['alarm_no'], alarm['status'], 'Closed')
    return {'ok': True, 'status': 'Closed', 'idempotent': False}
