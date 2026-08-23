from __future__ import annotations

from datetime import datetime, timedelta

from apps.audit import audit
from apps.condition_monitoring import condition_matches, threshold_text
from apps.events import emit_event
from apps.maintenance import create_condition_work_order
from apps.notifications import notify_once
from core.shared import next_no


def _one(cursor):
    row = cursor.fetchone()
    return dict(row) if row else None


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def evaluate_rules(
    conn,
    channel: dict,
    value: float,
    captured_at: str,
    reading_id: int,
    actor_id: int,
    *,
    fmea_lookup,
    approval_creator,
) -> list[dict]:
    """Evaluate deterministic condition-based maintenance rules for one Good telemetry sample."""
    results = []
    rules = _rows(conn.execute('SELECT * FROM cbm_rules WHERE channel_id=? AND active=1 ORDER BY id', (channel['id'],)))
    for rule in rules:
        state = _one(conn.execute('SELECT * FROM cbm_rule_state WHERE rule_id=?', (rule['id'],))) or {
            'consecutive_hits': 0, 'last_triggered_at': None, 'active_event_id': None,
        }
        breached = condition_matches(rule, value)
        hits = int(state.get('consecutive_hits') or 0) + 1 if breached else 0
        active = _one(
            conn.execute(
                "SELECT * FROM cbm_events WHERE rule_id=? AND status IN ('Open','Acknowledged') ORDER BY id DESC LIMIT 1",
                (rule['id'],),
            )
        )
        if not breached:
            resolved_no = None
            if active:
                reason = f"Condition cleared by Good-quality telemetry reading {value:g} {channel.get('unit') or ''}".strip()
                conn.execute(
                    "UPDATE cbm_events SET status='Resolved',resolved_at=?,resolution_reason=?,last_seen_at=? WHERE id=?",
                    (captured_at, reason, captured_at, active['id']),
                )
                emit_event(
                    conn, 'maintenance.cbm.event_resolved', 'cbm_event', active['event_no'],
                    {'event_no': active['event_no'], 'rule_no': rule['rule_no'], 'value': value, 'captured_at': captured_at},
                )
                if actor_id:
                    audit(conn, actor_id, 'CBM AUTO RESOLVE', 'Condition-Based Maintenance', active['event_no'], active['status'], 'Resolved')
                resolved_no = active['event_no']
            conn.execute(
                '''INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
                   VALUES(?,0,?,'Good',?,?,NULL)
                   ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=0,last_value=excluded.last_value,last_quality='Good',
                     last_evaluated_at=excluded.last_evaluated_at,active_event_id=NULL''',
                (rule['id'], value, captured_at, state.get('last_triggered_at')),
            )
            results.append({'rule_no': rule['rule_no'], 'action': 'resolved' if resolved_no else 'normal', 'event_no': resolved_no, 'work_order': None})
            continue

        if active:
            conn.execute(
                'UPDATE cbm_events SET trigger_value=?,last_seen_at=?,occurrence_count=occurrence_count+1 WHERE id=?',
                (value, captured_at, active['id']),
            )
            conn.execute(
                '''INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
                   VALUES(?,? ,?,'Good',?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,
                     last_quality='Good',last_evaluated_at=excluded.last_evaluated_at,active_event_id=excluded.active_event_id''',
                (rule['id'], hits, value, captured_at, state.get('last_triggered_at'), active['id']),
            )
            results.append({'rule_no': rule['rule_no'], 'action': 'active', 'event_no': active['event_no'], 'work_order': None})
            continue

        required = max(1, int(rule.get('consecutive_readings') or 1))
        cooldown = False
        if state.get('last_triggered_at'):
            try:
                cooldown = _dt(captured_at) < _dt(state['last_triggered_at']) + timedelta(minutes=max(0, int(rule.get('cooldown_minutes') or 0)))
            except Exception:
                cooldown = False
        if hits < required or cooldown:
            conn.execute(
                '''INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
                   VALUES(?,? ,?,'Good',?,?,NULL)
                   ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,
                     last_quality='Good',last_evaluated_at=excluded.last_evaluated_at''',
                (rule['id'], hits, value, captured_at, state.get('last_triggered_at')),
            )
            results.append({
                'rule_no': rule['rule_no'], 'action': 'cooldown' if cooldown else 'pending', 'event_no': None,
                'work_order': None, 'hits': hits, 'required': required,
            })
            continue

        event_no = next_no(conn, 'cbm_events', 'event_no', 'CBM-', 80001)
        condition = threshold_text(rule)
        message = (
            f"{rule['name']}: {channel['channel_code']} value {value:g} {channel.get('unit') or ''} matched {condition} "
            f"after {hits} consecutive Good reading(s)."
        ).strip()
        cur = conn.execute(
            '''INSERT INTO cbm_events(
                 event_no,rule_id,channel_id,asset_id,reading_id,severity,status,trigger_value,message,asset_fmea_id,opened_at,last_seen_at,occurrence_count
               ) VALUES(?,?,?,?,?,?,'Open',?,?,?,?,?,1)''',
            (
                event_no, rule['id'], channel['id'], channel['asset_id'], reading_id, rule['severity'], value, message,
                rule.get('asset_fmea_id'), captured_at, captured_at,
            ),
        )
        work = None
        if rule['action_type'] == 'WorkOrder':
            fmea = fmea_lookup(conn, rule['asset_fmea_id'], channel['asset_id']) if rule.get('asset_fmea_id') else None
            work = create_condition_work_order(
                conn,
                rule=rule,
                channel=channel,
                event_no=event_no,
                value=value,
                actor_id=actor_id,
                fmea=fmea,
                approval_creator=approval_creator,
                condition_text=condition,
            )
            conn.execute('UPDATE cbm_events SET work_order_id=? WHERE id=?', (work['id'], cur.lastrowid))
        notify_once(
            conn,
            'Condition-based maintenance trigger',
            f"{event_no} — {message}",
            rule['severity'],
            None,
            'maintenance_manager',
            'telemetry',
            event_no,
        )
        emit_event(
            conn,
            'maintenance.cbm.event_opened',
            'cbm_event',
            event_no,
            {
                'event_no': event_no,
                'rule_no': rule['rule_no'],
                'channel_code': channel['channel_code'],
                'asset_id': channel['asset_id'],
                'value': value,
                'action_type': rule['action_type'],
                'work_order': work['wo_no'] if work else None,
            },
        )
        if actor_id:
            audit(
                conn,
                actor_id,
                'CBM TRIGGER',
                'Condition-Based Maintenance',
                event_no,
                '',
                {'rule': rule['rule_no'], 'value': value, 'work_order': work['wo_no'] if work else None},
            )
        conn.execute(
            '''INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
               VALUES(?,? ,?,'Good',?,?,?)
               ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,
                 last_quality='Good',last_evaluated_at=excluded.last_evaluated_at,last_triggered_at=excluded.last_triggered_at,
                 active_event_id=excluded.active_event_id''',
            (rule['id'], hits, value, captured_at, captured_at, cur.lastrowid),
        )
        results.append({'rule_no': rule['rule_no'], 'action': 'opened', 'event_no': event_no, 'work_order': work['wo_no'] if work else None})
    return results
