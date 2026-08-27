"""EUAS Operations Command Center kernel.

Unifies operational events (outages, critical alarms, emergency work,
dispatches, material blockers, approvals) into one decision view without
duplicating domain records: every situation references its underlying source
entities by type and number.

Design rules:

* No fabrication. Timeline entries and contributors are derived exclusively
  from persisted timestamps and relationships in the platform's own tables.
* Every severity, progress percentage and recommendation states the evidence
  that produced it.
* Read-only aggregations: no domain state is mutated here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Query

from . import application as _application
from .auth import current_user
from .database import db


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return _application.now()


# ---------------------------------------------------------------------------
# Situation aggregation
# ---------------------------------------------------------------------------
# Severity is derived from evidence, highest wins:
#   Critical - open forced outage, active critical alarm, or emergency work
#   High     - active warning alarm or High/Critical priority open work
#   Medium   - everything else that still requires coordinated recovery
_SEVERITY_RANK = {'Medium': 1, 'High': 2, 'Critical': 3}

# Restoration progress is a documented mapping from work-order execution
# state; 0 means no countermeasure work exists yet.
_RESTORATION_BY_STATUS = {
    'Submitted': 20,
    'Approved': 35,
    'Dispatched': 50,
    'In Progress': 70,
    'Completed': 90,
    'Closed': 100,
}


def _active_alarms_for_assets(conn, asset_ids: list[int]) -> dict[int, list[dict]]:
    if not asset_ids:
        return {}
    marks = ','.join('?' * len(asset_ids))
    grouped: dict[int, list[dict]] = {}
    for row in _rows(conn.execute(
        f'''SELECT oa.*,tc.channel_code,a.asset_no
            FROM operational_alarms oa
            JOIN telemetry_channels tc ON tc.id=oa.channel_id
            JOIN assets a ON a.id=oa.asset_id
            WHERE oa.asset_id IN ({marks}) AND oa.status IN ('Open','Acknowledged')
            ORDER BY oa.opened_at ASC''',
        asset_ids,
    )):
        grouped.setdefault(int(row['asset_id']), []).append(row)
    return grouped


def _open_work_for_assets(conn, asset_ids: list[int]) -> dict[int, list[dict]]:
    if not asset_ids:
        return {}
    marks = ','.join('?' * len(asset_ids))
    grouped: dict[int, list[dict]] = {}
    for row in _rows(conn.execute(
        f'''SELECT w.id,w.asset_id,w.wo_no,w.title,w.priority,w.status,w.work_type,
                   w.assigned_to,w.target_finish,w.created_at,w.actual_start,
                   u.full_name assigned_name,
                   sl.response_status,sl.resolution_status,sl.response_due,sl.resolution_due
            FROM work_orders w
            LEFT JOIN users u ON u.id=w.assigned_to
            LEFT JOIN work_order_sla sl ON sl.work_order_id=w.id
            WHERE w.asset_id IN ({marks})
              AND w.status NOT IN ('Completed','Closed','Cancelled')
            ORDER BY CASE w.priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4
                     WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC, w.id''',
        asset_ids,
    )):
        grouped.setdefault(int(row['asset_id']), []).append(row)
    return grouped


def _active_dispatches_for_work(conn, wo_ids: list[int]) -> dict[int, list[dict]]:
    """Batched active dispatch lookup: {work_order_id: [dispatch, ...]}."""
    if not wo_ids:
        return {}
    marks = ','.join('?' * len(wo_ids))
    grouped: dict[int, list[dict]] = {}
    for row in _rows(conn.execute(
        f'''SELECT d.id,d.work_order_id,d.dispatch_no,d.status,d.dispatched_at,
                   u.full_name technician_name
            FROM dispatch_assignments d LEFT JOIN users u ON u.id=d.technician_user_id
            WHERE d.work_order_id IN ({marks})
              AND d.status NOT IN ('Completed','Cancelled')
            ORDER BY d.dispatched_at ASC''',
        wo_ids,
    )):
        grouped.setdefault(int(row['work_order_id']), []).append(row)
    return grouped


def _material_blocker_counts(conn, wo_ids: list[int]) -> dict[int, int]:
    return _application._material_blocker_map(conn, wo_ids)


def _pending_approvals_for_work(conn, wo_nos: list[str]) -> dict[str, dict]:
    if not wo_nos:
        return {}
    marks = ','.join('?' * len(wo_nos))
    result: dict[str, dict] = {}
    for row in _rows(conn.execute(
        f'''SELECT record_code,approval_no,title,requested_at,assigned_role,status
            FROM approval_requests
            WHERE status='Pending' AND module='Work Management'
              AND record_code IN ({marks})
            ORDER BY requested_at ASC''',
        wo_nos,
    )):
        result.setdefault(row['record_code'], row)
    return result


def _situation_severity(outage: Optional[dict], alarms: list[dict], work: list[dict]) -> str:
    if outage and outage.get('status') == 'Open':
        return 'Critical'
    if any(a['severity'] == 'Critical' for a in alarms):
        return 'Critical'
    if any(w['priority'] == 'Emergency' for w in work):
        return 'Critical'
    if any(a['severity'] == 'Warning' for a in alarms):
        return 'High'
    if any(w['priority'] in ('High', 'Critical') for w in work):
        return 'High'
    return 'Medium'


def _restoration_progress(work: list[dict]) -> dict:
    """Documented mapping of execution state to restoration progress."""
    if not work:
        return {'progress_pct': 0, 'state': 'no_countermeasure_work',
                'basis': 'no open work order references this situation'}
    best_status = max(
        (w['status'] for w in work),
        key=lambda s: _RESTORATION_BY_STATUS.get(s, 0),
    )
    pct = _RESTORATION_BY_STATUS.get(best_status, 0)
    return {
        'progress_pct': pct,
        'state': f"work {best_status}",
        'basis': f'highest executed status among {len(work)} related work order(s)',
    }


def _situations(conn, site_id: Optional[int] = None) -> list[dict]:
    outages = _rows(conn.execute(
        '''SELECT o.*,a.asset_no,a.name asset_name,a.criticality,a.condition,a.location_id,
                  l.name location_name,s.site_code,s.name site_name
           FROM asset_outages o
           JOIN assets a ON a.id=o.asset_id
           LEFT JOIN locations l ON l.id=a.location_id
           LEFT JOIN sites s ON s.id=o.site_id
           WHERE o.status='Open'
           ORDER BY o.start_at ASC'''
    ))
    if site_id:
        outages = [o for o in outages if o.get('site_id') == site_id]

    covered_asset_ids = {int(o['asset_id']) for o in outages}
    covered_wo_ids = {int(o['work_order_id']) for o in outages if o.get('work_order_id')}

    situations: dict[str, dict] = {}
    for o in outages:
        key = f"outage:{o['outage_no']}"
        situations[key] = {
            'situation_key': key,
            'anchor_type': 'outage',
            'anchor_no': o['outage_no'],
            'outage': o,
            'extra_outage_ids': [],
        }

    # Active critical alarms on assets not already covered by an outage keep
    # their own situation so they are never silently folded away.
    critical_alarm_rows = _rows(conn.execute(
        '''SELECT DISTINCT oa.asset_id
           FROM operational_alarms oa
           WHERE oa.status IN ('Open','Acknowledged') AND oa.severity='Critical' '''
    ))
    orphan_critical_assets = [
        int(r['asset_id']) for r in critical_alarm_rows
        if int(r['asset_id']) not in covered_asset_ids
    ]

    emergency_rows = _rows(conn.execute(
        '''SELECT w.id,w.asset_id,w.wo_no FROM work_orders w
           WHERE w.priority='Emergency'
             AND w.status NOT IN ('Completed','Closed','Cancelled')'''
    ))
    orphan_emergency_assets = [
        int(r['asset_id']) for r in emergency_rows
        if r['asset_id'] and int(r['asset_id']) not in covered_asset_ids
        and int(r['id']) not in covered_wo_ids
    ]
    orphan_asset_ids = sorted(set(orphan_critical_assets) | set(orphan_emergency_assets))

    asset_filter = ''
    args: list = []
    if site_id:
        asset_filter = ' AND s.id=?'
        args.append(site_id)
    if orphan_asset_ids:
        marks = ','.join('?' * len(orphan_asset_ids))
        anchor_assets = _rows(conn.execute(
            f'''SELECT a.id,a.asset_no,a.name asset_name,a.criticality,a.condition,
                       a.status asset_status,l.name location_name,s.id site_id,
                       s.site_code,s.name site_name
                FROM assets a LEFT JOIN locations l ON l.id=a.location_id
                LEFT JOIN sites s ON s.id=l.site_id
                WHERE a.id IN ({marks}){asset_filter}''',
            orphan_asset_ids + args,
        ))
        for a in anchor_assets:
            key = f"asset:{a['asset_no']}"
            situations[key] = {
                'situation_key': key,
                'anchor_type': 'asset',
                'anchor_no': a['asset_no'],
                'anchor_asset': a,
                'extra_outage_ids': [],
            }

    if not situations:
        return []

    involved_assets = set()
    for s in situations.values():
        if s['anchor_type'] == 'outage':
            involved_assets.add(int(s['outage']['asset_id']))
        else:
            involved_assets.add(int(s['anchor_asset']['id']))

    alarms_by_asset = _active_alarms_for_assets(conn, sorted(involved_assets))
    work_by_asset = _open_work_for_assets(conn, sorted(involved_assets))
    blocker_counts = _material_blocker_counts(
        conn, [int(w['id']) for rows in work_by_asset.values() for w in rows]
    )
    approval_map = _pending_approvals_for_work(
        conn, [w['wo_no'] for rows in work_by_asset.values() for w in rows]
    )

    now_ts = datetime.now()
    result = []
    for key, sit in situations.items():
        if sit['anchor_type'] == 'outage':
            o = sit['outage']
            asset_ref = {
                'asset_id': int(o['asset_id']),
                'asset_no': o['asset_no'],
                'asset_name': o['asset_name'],
                'criticality': o.get('criticality'),
                'condition': o.get('condition'),
                'location_name': o.get('location_name'),
                'site_id': o.get('site_id'),
                'site_code': o.get('site_code'),
                'site_name': o.get('site_name'),
            }
            started_at = o['start_at']
            outage_view = {
                'outage_no': o['outage_no'],
                'outage_type': o['outage_type'],
                'cause_code': o.get('cause_code') or '',
                'impact': o.get('impact') or '',
                'start_at': o['start_at'],
                'work_order_id': o.get('work_order_id'),
            }
        else:
            a = sit['anchor_asset']
            asset_ref = {
                'asset_id': int(a['id']),
                'asset_no': a['asset_no'],
                'asset_name': a['asset_name'],
                'criticality': a.get('criticality'),
                'condition': a.get('condition'),
                'location_name': a.get('location_name'),
                'site_id': a.get('site_id'),
                'site_code': a.get('site_code'),
                'site_name': a.get('site_name'),
            }
            started_at = None
            outage_view = None

        asset_id = asset_ref['asset_id']
        alarms = alarms_by_asset.get(asset_id, [])
        work = work_by_asset.get(asset_id, [])
        severity = _situation_severity(outage_view, alarms, work)

        shortest = []
        for w in work:
            n = blocker_counts.get(int(w['id']), 0)
            if n:
                shortest.append({
                    'wo_id': int(w['id']),
                    'wo_no': w['wo_no'],
                    'shortage_items': n,
                    'approvals': approval_map.get(w['wo_no']),
                })

        timestamps = [_parse_ts(started_at)] if started_at else []
        timestamps += [_parse_ts(a['opened_at']) for a in alarms]
        timestamps += [_parse_ts(w['created_at']) for w in work]
        stamps = [t for t in timestamps if t]
        earliest = min(stamps).isoformat(timespec='seconds') if stamps else None

        duration_hours = None
        if started_at:
            st = _parse_ts(started_at)
            if st:
                duration_hours = round(max(0.0, (now_ts - st).total_seconds() / 3600.0), 1)

        wo_ids = [int(w['id']) for w in work]
        dispatches = _active_dispatches_for_work(conn, wo_ids)
        lifecycle = _derive_lifecycle(alarms, work, shortest, dispatches)
        restoration_intel = _restoration_intelligence(
            outage_view if sit['anchor_type'] == 'outage' else None,
            alarms, work, blocker_counts, now_ts,
        )

        result.append({
            'situation_key': key,
            'anchor_type': sit['anchor_type'],
            'anchor_no': sit['anchor_no'],
            **asset_ref,
            'severity': severity,
            'lifecycle': lifecycle,
            'started_at': earliest,
            'outage_duration_hours': duration_hours,
            'outage': outage_view,
            'alarms': [
                {
                    'alarm_no': a['alarm_no'],
                    'severity': a['severity'],
                    'status': a['status'],
                    'message': a['message'],
                    'channel_code': a['channel_code'],
                    'opened_at': a['opened_at'],
                }
                for a in alarms
            ],
            'work_orders': [
                {**w, 'active_dispatches': dispatches.get(int(w['id']), [])}
                for w in work
            ],
            'material_blockers': shortest,
            'restoration': _restoration_progress(work),
            'restoration_intel': restoration_intel,
        })

    result.sort(key=lambda s: (-_SEVERITY_RANK[s['severity']], s['started_at'] or '', s['anchor_no']))
    return result


# Lifecycle is derived from source-record state with documented precedence:
#   RESOLVED handled separately (see _resolved_situations); among actives:
#   RESTORING   - repair physically started (a work order has actual_start)
#   BLOCKED     - recovery work cannot execute: material shortage on open work
#   MITIGATING  - countermeasure raised or crew mobilised (open related work,
#                 active dispatch, or work In Progress)
#   ACKNOWLEDGED- every active alarm acknowledged and at least one exists
#   ACTIVE      - situation detected but no countermeasure evidence yet
_LIFECYCLE_ORDER = ('RESTORING', 'BLOCKED', 'MITIGATING', 'ACKNOWLEDGED', 'ACTIVE')


def _derive_lifecycle(alarms: list[dict], work: list[dict],
                      blockers: list[dict], dispatches_by_wo: dict[int, list[dict]]) -> str:
    signals = set()
    if blockers:
        signals.add('BLOCKED')
    if any(w.get('actual_start') for w in work):
        signals.add('RESTORING')
    mobilised = bool(work) or any(
        dispatches_by_wo.get(int(w['id'])) for w in work
    ) or any(w['status'] == 'In Progress' for w in work)
    if mobilised:
        signals.add('MITIGATING')
    if alarms and all(a['status'] == 'Acknowledged' for a in alarms):
        signals.add('ACKNOWLEDGED')
    signals.add('ACTIVE')
    for state in _LIFECYCLE_ORDER:
        if state in signals:
            return state
    return 'ACTIVE'  # pragma: no cover - defensive


def _first(values) -> Optional[str]:
    stamps = sorted(v for v in values if v)
    return stamps[0] if stamps else None


def _restoration_intelligence(outage: Optional[dict], alarms: list[dict],
                              work: list[dict], blocker_counts: dict[int, int],
                              now_ts: datetime) -> dict:
    """Every duration comes from persisted timestamps; nothing is estimated."""
    start = outage['start_at'] if outage else None
    end = outage.get('end_at') if outage else None
    ack_at = _first(a.get('acknowledged_at') for a in alarms)
    wo_created = _first(w.get('created_at') for w in work)
    dispatched = _first(
        d['dispatched_at']
        for w in work for d in (w.get('active_dispatches') or [])
    ) if work else None
    repair_start = _first(w.get('actual_start') for w in work)

    def hours_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
        ta, tb = _parse_ts(a), _parse_ts(b)
        if not ta or not tb:
            return None
        return round(abs((tb - ta).total_seconds()) / 3600.0, 1)

    elapsed_end = end or _now_iso()
    sla_resolution_status = next(
        (w['resolution_status'] for w in work if w.get('resolution_status')), None)
    sla_resolution_due = next(
        (w['resolution_due'] for w in work if w.get('resolution_due')), None)
    sla_exposed = bool(
        sla_resolution_status == 'Breached'
        or (sla_resolution_due and _parse_ts(sla_resolution_due)
            and _parse_ts(sla_resolution_due) < now_ts
            and sla_resolution_status != 'Breached' and not end)
    )
    blocked_wo = next((w for w in work if blocker_counts.get(int(w['id']))), None)
    current_blocker = None
    if blocked_wo:
        current_blocker = {
            'wo_no': blocked_wo['wo_no'],
            'shortage_items': blocker_counts[int(blocked_wo['id'])],
        }
    total_outage_hours = hours_between(start, end)
    elapsed_hours = hours_between(start, elapsed_end)
    return {
        'outage_start': start,
        'acknowledged_at': ack_at,
        'acknowledge_delay_hours': hours_between(start, ack_at),
        'work_created_at': wo_created,
        'dispatched_at': dispatched,
        'repair_started_at': repair_start,
        'restored_at': end,
        'total_outage_hours': total_outage_hours,
        'elapsed_hours': elapsed_hours,
        'sla_resolution_status': sla_resolution_status,
        'sla_resolution_due': sla_resolution_due,
        'sla_exposed': sla_exposed,
        'current_blocker': current_blocker,
    }


def _resolved_situations(conn, site_id: Optional[int] = None, window_days: int = 7) -> list[dict]:
    """Situations derivable as resolved purely from stored facts.

    A recently ended outage counts as resolved only when the asset also has
    no remaining active alarm and no remaining emergency/high-priority work.
    """
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat(timespec='seconds')
    args: list = [cutoff]
    site_clause = ''
    if site_id:
        site_clause = ' AND o.site_id=?'
        args.append(site_id)
    rows_ = _rows(conn.execute(
        f'''SELECT o.outage_no,o.asset_id,o.start_at,o.end_at,o.outage_type,
                   a.asset_no,a.name asset_name,s.site_code,s.name site_name,
                   (SELECT MAX(oa.acknowledged_at) FROM operational_alarms oa
                     WHERE oa.asset_id=o.asset_id) last_ack,
                   (SELECT COUNT(*) FROM operational_alarms oa
                     WHERE oa.asset_id=o.asset_id AND oa.status IN ('Open','Acknowledged')) active_alarms,
                   (SELECT COUNT(*) FROM work_orders w
                     WHERE w.asset_id=o.asset_id AND w.priority IN ('Emergency','Critical','High')
                       AND w.status NOT IN ('Completed','Closed','Cancelled')) open_priority_work
            FROM asset_outages o JOIN assets a ON a.id=o.asset_id
            LEFT JOIN sites s ON s.id=o.site_id
            WHERE o.status<>'Open' AND o.end_at IS NOT NULL AND o.end_at>=?{site_clause}
            ORDER BY o.end_at DESC LIMIT 10''',
        args,
    ))
    resolved = []
    for r in rows_:
        if int(r['active_alarms']) > 0 or int(r['open_priority_work']) > 0:
            continue
        ta, tb = _parse_ts(r['start_at']), _parse_ts(r['end_at'])
        resolved.append({
            'situation_key': f"outage:{r['outage_no']}",
            'asset_no': r['asset_no'],
            'asset_name': r['asset_name'],
            'site_name': r.get('site_name'),
            'started_at': r['start_at'],
            'restored_at': r['end_at'],
            'acknowledged_at': r['last_ack'],
            'total_outage_hours': (
                round((tb - ta).total_seconds() / 3600.0, 1) if ta and tb else None
            ),
        })
    return resolved


def situations_view(conn, site_id: Optional[int] = None) -> dict:
    situations = _situations(conn, site_id)
    counts: dict[str, int] = {'Critical': 0, 'High': 0, 'Medium': 0}
    lifecycle_counts: dict[str, int] = {}
    for s in situations:
        counts[s['severity']] += 1
        lifecycle_counts[s['lifecycle']] = lifecycle_counts.get(s['lifecycle'], 0) + 1
    return {
        'generated_at': _now_iso(),
        'total': len(situations),
        'severity_counts': counts,
        'lifecycle_counts': lifecycle_counts,
        'situations': situations,
        'resolved': _resolved_situations(conn, site_id),
    }


# ---------------------------------------------------------------------------
# Situation timeline (fused from persisted events only)
# ---------------------------------------------------------------------------
def situation_timeline(conn, situation_key: str) -> dict:
    anchor_type, _, anchor_no = situation_key.partition(':')
    if anchor_type == 'outage':
        outage = conn.execute(
            '''SELECT o.*,a.asset_no,a.name asset_name FROM asset_outages o
               JOIN assets a ON a.id=o.asset_id WHERE o.outage_no=?''',
            (anchor_no,),
        ).fetchone()
        if not outage:
            raise HTTPException(404, 'Situation not found')
        asset_id = int(outage['asset_id'])
        head = {
            'situation_key': situation_key,
            'asset_no': outage['asset_no'],
            'asset_name': outage['asset_name'],
        }
    elif anchor_type == 'asset':
        asset = conn.execute('SELECT id,asset_no,name FROM assets WHERE asset_no=?', (anchor_no,)).fetchone()
        if not asset:
            raise HTTPException(404, 'Situation not found')
        asset_id = int(asset['id'])
        head = {'situation_key': situation_key, 'asset_no': asset['asset_no'], 'asset_name': asset['name']}
    else:
        raise HTTPException(400, 'Situation key must start with outage: or asset:')

    events: list[dict] = []

    def add(ts, kind, label, ref_type='', ref_no=''):
        stamp = _parse_ts(ts)
        if ts and stamp:
            events.append({
                'ts': stamp.isoformat(timespec='seconds'),
                'kind': kind,
                'label': label,
                'ref_type': ref_type,
                'ref_no': ref_no,
            })

    for a in _rows(conn.execute(
        'SELECT * FROM operational_alarms WHERE asset_id=?', (asset_id,)
    )):
        add(a['opened_at'], 'alarm_opened', f"alarm {a['alarm_no']} opened ({a['severity']}): {a['message'][:80]}", 'alarm', a['alarm_no'])
        add(a['acknowledged_at'], 'alarm_acknowledged', f"alarm {a['alarm_no']} acknowledged", 'alarm', a['alarm_no'])
        add(a['cleared_at'], 'alarm_cleared', f"alarm {a['alarm_no']} cleared", 'alarm', a['alarm_no'])
        add(a['closed_at'], 'alarm_closed', f"alarm {a['alarm_no']} closed", 'alarm', a['alarm_no'])

    for o in _rows(conn.execute(
        'SELECT * FROM asset_outages WHERE asset_id=?', (asset_id,)
    )):
        add(o['start_at'], 'outage_started', f"outage {o['outage_no']} started ({o['outage_type']})", 'outage', o['outage_no'])
        add(o['end_at'], 'outage_ended', f"outage {o['outage_no']} ended", 'outage', o['outage_no'])

    work_ids: list[int] = []
    for w in _rows(conn.execute(
        '''SELECT * FROM work_orders WHERE asset_id=?
           ORDER BY created_at ASC''',
        (asset_id,),
    )):
        work_ids.append(int(w['id']))
        add(w['created_at'], 'work_created', f"work order {w['wo_no']} created ({w['priority']} {w['work_type']})", 'work_order', w['wo_no'])
        add(w['actual_start'], 'repair_started', f"repair started on {w['wo_no']}", 'work_order', w['wo_no'])
        add(w['actual_finish'], 'repair_completed', f"repair completed on {w['wo_no']}", 'work_order', w['wo_no'])

    for d in _rows(conn.execute(
        '''SELECT d.*,u.full_name technician FROM dispatch_assignments d
           LEFT JOIN users u ON u.id=d.technician_user_id
           WHERE d.work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)
           ORDER BY d.dispatched_at ASC''',
        (asset_id,),
    )):
        tech = d.get('technician') or 'unassigned technician'
        add(d['dispatched_at'], 'technician_dispatched', f"{tech} dispatched ({d['status']})", 'dispatch', d['dispatch_no'])
        add(d['arrived_at'], 'technician_arrived', f"{tech} arrived on site", 'dispatch', d['dispatch_no'])
        add(d['completed_at'], 'dispatch_completed', f"dispatch {d['dispatch_no']} completed", 'dispatch', d['dispatch_no'])

    if work_ids:
        marks = ','.join('?' * len(work_ids))
        for e in _rows(conn.execute(
            f'''SELECT we.module,we.record_code,we.event,we.from_status,we.to_status,
                       we.notes,we.created_at,u.full_name actor
                FROM workflow_events we JOIN users u ON u.id=we.actor_id
                WHERE we.module='Work Management' AND we.record_id IN ({marks})
                ORDER BY we.created_at ASC''',
            work_ids,
        )):
            transition = f"{e['from_status']}→{e['to_status']}" if e['to_status'] else e['event']
            add(e['created_at'], 'workflow_transition',
                f"{e['record_code']} {transition} by {e['actor']}",
                'work_order', e['record_code'])

        # Deterministic ordering: timestamp, then kind, then record number so
    # equal-timestamp events are stable across repeated reads.
    events.sort(key=lambda e: (e['ts'], e['kind'], e['ref_no']))
    return {
        **head,
        'generated_at': _now_iso(),
        'event_count': len(events),
        'events': events,
    }


# ---------------------------------------------------------------------------
# WHY IS THIS RED — KPI explainability over real records
# ---------------------------------------------------------------------------
def _share_block(label: str, count: int, total: int, detail: str) -> dict:
    pct = round(100.0 * count / total, 1) if total else 0.0
    return {'label': label, 'count': count, 'share_pct': pct, 'detail': detail}


def why_red(conn, key: str, site_id: Optional[int] = None) -> dict:
    generated = _now_iso()

    def site_clause(table_alias: str) -> tuple[str, list]:
        if site_id:
            return f' AND {table_alias}.site_id=?', [site_id]
        return '', []

    if key == 'open_outages':
        clause, args = site_clause('o')
        rows_ = _rows(conn.execute(
            f'''SELECT o.outage_no,o.start_at,o.outage_type,o.cause_code,a.asset_no,a.name asset_name,
                       s.name site_name
                FROM asset_outages o JOIN assets a ON a.id=o.asset_id
                LEFT JOIN sites s ON s.id=o.site_id
                WHERE o.status='Open'{clause} ORDER BY o.start_at ASC''',
            args,
        ))
        total = len(rows_)
        contributors = []
        for o in rows_:
            st = _parse_ts(o['start_at'])
            hours = round((datetime.now() - st).total_seconds() / 3600.0, 1) if st else None
            contributors.append(_share_block(
                f"{o['asset_no']} — {o['site_name'] or 'unknown site'}",
                1, total,
                f"{o['outage_type']} outage open {hours:g}h" + (f", cause {o['cause_code']}" if o['cause_code'] else ''),
            ))

    elif key == 'critical_alarms':
        clause, args = site_clause('oa')
        rows_ = _rows(conn.execute(
            f'''SELECT oa.alarm_no,oa.message,oa.opened_at,oa.status,a.asset_no,a.name asset_name
                FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id
                WHERE oa.status IN ('Open','Acknowledged') AND oa.severity='Critical'{clause}
                ORDER BY oa.opened_at ASC'''
        ))
        total = len(rows_)
        contributors = [
            _share_block(
                f"{r['asset_no']} / {r['alarm_no']}", 1, total,
                f"{r['status']} since {r['opened_at']}: {r['message'][:80]}",
            )
            for r in rows_
        ]

    elif key == 'overdue_work_orders':
        today = _application.date.today().isoformat()
        blocked = _rows(conn.execute(
            f'''SELECT w.id FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id
                WHERE w.target_finish IS NOT NULL AND w.target_finish<?
                  AND w.status NOT IN ('Completed','Closed','Cancelled')'''
        , [today]))
        blocker_map = _application._material_blocker_map(conn, [int(b['id']) for b in blocked])
        total = len(blocked)
        buckets: dict[str, dict] = {}

        def bump(bucket: str, detail: str) -> None:
            entry = buckets.setdefault(bucket, {'count': 0, 'details': []})
            entry['count'] += 1
            if len(entry['details']) < 5:
                entry['details'].append(detail)

        for wo in _rows(conn.execute(
            f'''SELECT w.id,w.wo_no,w.priority,w.target_finish,w.assigned_to,
                       sl.resolution_status
                FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id
                LEFT JOIN work_order_sla sl ON sl.work_order_id=w.id
                WHERE w.target_finish IS NOT NULL AND w.target_finish<?
                  AND w.status NOT IN ('Completed','Closed','Cancelled')
                ORDER BY w.target_finish ASC'''
        , [today])):
            wid = int(wo['id'])
            if blocker_map.get(wid):
                bump('material_blocked', f"{wo['wo_no']} — required part unavailable")
            elif not wo['assigned_to']:
                bump('unassigned', f"{wo['wo_no']} — nobody assigned")
            elif wo['resolution_status'] == 'Breached':
                bump('sla_breached', f"{wo['wo_no']} — resolution SLA breached")
            else:
                bump('execution_delay', f"{wo['wo_no']} — in execution past target")
        contributors = [
            _share_block(name, v['count'], total, '; '.join(v['details']))
            for name, v in sorted(buckets.items(), key=lambda kv: (-kv[1]['count'], kv[0]))
        ]

    elif key == 'material_blocked_work_orders':
        open_ids = [int(r['id']) for r in _rows(conn.execute(
            "SELECT id FROM work_orders WHERE status NOT IN ('Completed','Closed','Cancelled')"
        ))]
        blocker_map = _application._material_blocker_map(conn, open_ids)
        total = len(blocker_map)
        contributors = []
        if open_ids:
            marks = ','.join('?' * len(open_ids))
            info = {int(r['id']): r for r in _rows(conn.execute(
                f'SELECT id,wo_no,title,priority FROM work_orders WHERE id IN ({marks})',
                open_ids,
            ))}
            for wid, shortage in sorted(blocker_map.items(), key=lambda kv: (-kv[1], kv[0])):
                w = info.get(wid, {})
                contributors.append(_share_block(
                    w.get('wo_no', f'WO#{wid}'), shortage, sum(blocker_map.values()),
                    f"{w.get('priority', 'Unknown')} priority: {shortage} part line(s) short",
                ))

    elif key == 'emergency_work_orders':
        rows_ = _rows(conn.execute(
            '''SELECT w.wo_no,w.status,w.target_finish,a.asset_no
               FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id
               WHERE w.priority='Emergency' AND w.status NOT IN ('Closed','Cancelled')
               ORDER BY w.created_at ASC'''
        ))
        total = len(rows_)
        contributors = [
            _share_block(r['wo_no'], 1, total,
                         f"{r['asset_no'] or 'no asset'} — status {r['status']}"
                         + (f", target {r['target_finish']}" if r['target_finish'] else ''))
            for r in rows_
        ]

    elif key == 'pm_compliance':
        today = _application.date.today().isoformat()
        overdue = _rows(conn.execute(
            '''SELECT p.pm_no,p.next_due,a.asset_no,a.criticality
               FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id
               WHERE p.active=1 AND p.trigger_type='Calendar'
                 AND p.next_due IS NOT NULL AND p.next_due<?''',
            (today,),
        ))
        total_plans = int(conn.execute(
            "SELECT COUNT(*) FROM maintenance_plans WHERE active=1"
        ).fetchone()[0])
        missed = len(overdue)
        contributors = [
            _share_block(
                f"{o['pm_no']} / {o['asset_no']}", 1, max(missed, 1),
                f"criticality {o['criticality']}: due since {o['next_due']}",
            )
            for o in overdue[:10]
        ]
        return {
            'key': key,
            'title': 'PM compliance deterioration',
            'value': round(100 * (total_plans - missed) / total_plans, 1) if total_plans else 100.0,
            'unit': '%',
            'missed': missed,
            'contributors': contributors,
            'generated_at': generated,
        }

    else:
        raise HTTPException(404, f"No explainability configured for '{key}'")

    titles = {
        'open_outages': 'Open network outages',
        'critical_alarms': 'Active critical alarms',
        'overdue_work_orders': 'Overdue work orders',
        'material_blocked_work_orders': 'Material-blocked work orders',
        'emergency_work_orders': 'Emergency work load',
    }
    return {
        'key': key,
        'title': titles.get(key, key),
        'total': total,
        'contributors': contributors,
        'generated_at': generated,
    }


# ---------------------------------------------------------------------------
# WHAT SHOULD I DO — deterministic recommendation rules
# ---------------------------------------------------------------------------
# Action bridges reuse ONLY existing domain endpoints; this module never
# mutates domain state itself. Required roles mirror the owning endpoint's
# authorization so the UI can render availability, while the backend route
# remains the single enforcement point.
_ALARM_ACK_ROLES = ('admin', 'asset_manager', 'maintenance_manager',
                    'planner', 'supervisor', 'technician')
_ALARM_WO_ROLES = ('admin', 'asset_manager', 'maintenance_manager',
                   'planner', 'supervisor')
_RESERVE_ROLES = ('admin', 'maintenance_manager', 'planner', 'supervisor', 'storekeeper')
_PR_ROLES = ('admin', 'storekeeper', 'maintenance_manager', 'procurement', 'planner')


def recommendations_view(conn) -> dict:
    out: list[dict] = []
    seen: set[str] = set()
    generated = _now_iso()

    def emit(rec: dict) -> None:
        rid = rec['recommendation_id']
        if rid in seen:
            return
        seen.add(rid)
        out.append(rec)

    # R1: unacknowledged critical alarm with no linked work order.
    for a in _rows(conn.execute(
        '''SELECT oa.id,oa.alarm_no,oa.opened_at,oa.message,a.asset_no,a.id asset_id
           FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id
           WHERE oa.status='Open' AND oa.severity='Critical' AND oa.work_order_id IS NULL
           ORDER BY oa.opened_at ASC LIMIT 25'''
    )):
        opened = _parse_ts(a['opened_at'])
        age_min = int((datetime.now() - opened).total_seconds() // 60) if opened else None
        emit({
            'recommendation_id': f"critical-alarm-no-work:{a['alarm_no']}",
            'rule_id': 'critical-alarm-no-work',
            'severity': 'Critical',
            'entity_type': 'operational_alarm',
            'entity_id': int(a['id']),
            'entity_no': a['alarm_no'],
            'reason': 'Critical condition alarm has no linked work order',
            'recommended_action': 'Acknowledge and create an emergency inspection or corrective work order',
            'evidence': [
                f"alarm {a['alarm_no']} open on {a['asset_no']}" + (f" for {age_min} minutes" if age_min is not None else ''),
                f"message: {a['message'][:80]}",
            ],
            'actions': [
                {
                    'action_id': 'acknowledge_alarm',
                    'label': 'Acknowledge alarm',
                    'method': 'POST',
                    'path': f"/api/alarms/{int(a['id'])}/acknowledge",
                    'required_roles': list(_ALARM_ACK_ROLES),
                },
                {
                    'action_id': 'create_work_order',
                    'label': 'Create corrective work order',
                    'method': 'POST',
                    'path': f"/api/alarms/{int(a['id'])}/work-order",
                    'body': {'notes': f"Raised from Operations Command Center for {a['alarm_no']}"},
                    'required_roles': list(_ALARM_WO_ROLES),
                },
            ],
        })

    # R2: emergency/high work blocked on missing material.
    open_ids = [int(r['id']) for r in _rows(conn.execute(
        "SELECT id FROM work_orders WHERE status NOT IN ('Completed','Closed','Cancelled')"
    ))]
    blockers = _application._material_blocker_map(conn, open_ids) if open_ids else {}
    if blockers:
        marks = ','.join('?' * len(open_ids))
        prio = {int(r['id']): r for r in _rows(conn.execute(
            f'SELECT id,wo_no,priority,asset_id FROM work_orders WHERE id IN ({marks})',
            open_ids,
        ))}
        for wid, shortage in blockers.items():
            w = prio.get(wid)
            if not w or w['priority'] not in ('Emergency', 'Critical', 'High'):
                continue
            short_items = _rows(conn.execute(
                '''SELECT ii.id item_id,ii.item_no,ii.name,
                          rr.quantity-COALESCE((SELECT SUM(m.quantity) FROM work_order_materials m
                              WHERE m.work_order_id=rr.work_order_id AND m.inventory_item_id=rr.inventory_item_id),0)
                           AS still_required
                   FROM work_order_requirements rr JOIN inventory_items ii ON ii.id=rr.inventory_item_id
                   WHERE rr.work_order_id=? AND rr.status<>'Cancelled' ''',
                (wid,),
            ))
            pending_pr = conn.execute(
                '''SELECT pr.pr_no,pr.status FROM purchase_requisitions pr
                   JOIN purchase_requisition_items pri ON pri.pr_id=pr.id
                   JOIN work_order_requirements rr ON rr.inventory_item_id=pri.inventory_item_id
                   WHERE rr.work_order_id=? AND pr.status NOT IN ('Received','Cancelled','Rejected')
                   LIMIT 1''',
                (wid,),
            ).fetchone()
            actions = [
                {
                    'action_id': 'reserve_available_stock',
                    'label': 'Reserve any available stock',
                    'method': 'POST',
                    'path': f'/api/work-orders/{wid}/reserve-all',
                    'required_roles': list(_RESERVE_ROLES),
                },
            ]
            evidence = [
                f"{w['wo_no']} ({w['priority']}) short on {int(item['still_required'])} x {item['item_no']} {item['name']}"
                for item in short_items[:4]
            ]
            if pending_pr:
                evidence.append(f"requisition {pending_pr['pr_no']} already {pending_pr['status']}")
            pr_items = [
                {
                    'inventory_item_id': int(item['item_id']),
                    'quantity': max(0, int(item['still_required'])),
                    'description': item['name'],
                }
                for item in short_items
                if max(0, int(item['still_required'])) > 0
            ]
            if pr_items and not pending_pr:
                actions.append({
                    'action_id': 'raise_requisition',
                    'label': 'Raise spare replenishment requisition',
                    'method': 'POST',
                    'path': '/api/procurement/requisitions',
                    'body': {
                        'title': f"Spare replenishment for {w['wo_no']}",
                        'work_order_id': wid,
                        'justification': f"Auto-drafted from material blockers on {w['wo_no']}",
                        'items': pr_items,
                    },
                    'required_roles': list(_PR_ROLES),
                })
            emit({
                'recommendation_id': f"critical-work-material-blocked:{w['wo_no']}",
                'rule_id': 'critical-work-material-blocked',
                'severity': 'Critical' if w['priority'] in ('Emergency', 'Critical') else 'High',
                'entity_type': 'work_order',
                'entity_id': wid,
                'entity_no': w['wo_no'],
                'reason': f'Recovery-critical work cannot execute: {shortage} part line(s) unavailable',
                'recommended_action': (
                    'Escalate the existing requisition/PO with the vendor for expedited delivery'
                    if pending_pr else
                    'Reserve available stock or raise a spare replenishment requisition'
                ),
                'evidence': evidence,
                'actions': actions,
            })

    # R3: repeated corrective failures inside 90 days on one asset.
    cutoff = (datetime.now() - timedelta(days=90)).isoformat(timespec='seconds')
    for r in _rows(conn.execute(
        '''SELECT a.id asset_id,a.asset_no,COUNT(*) failures FROM work_orders w
           JOIN assets a ON a.id=w.asset_id
           WHERE w.status IN ('Completed','Closed')
             AND w.work_type IN ('Corrective Maintenance','Breakdown')
             AND COALESCE(w.actual_finish,w.updated_at)>=?
           GROUP BY a.id HAVING COUNT(*)>=2 ORDER BY failures DESC LIMIT 10''',
        (cutoff,),
    )):
        emit({
            'recommendation_id': f"repeat-failure-investigation:{r['asset_no']}",
            'rule_id': 'repeat-failure-investigation',
            'severity': 'High',
            'entity_type': 'asset',
            'entity_id': int(r['asset_id']),
            'entity_no': r['asset_no'],
            'reason': f"{r['failures']} corrective completions in 90 days indicate a recurring failure mode",
            'recommended_action': 'Open a reliability investigation (FMEA review / root cause analysis)',
            'evidence': [f"{r['failures']} corrective/breakdown completions within the 90-day window"],
            # No mutation bridge: FMEA authoring needs analyst input, so this
            # rule only navigates to the affected record.
            'actions': [],
        })

    # R4: PM overdue on high-consequence assets.
    today = _application.date.today().isoformat()
    for p in _rows(conn.execute(
        '''SELECT p.pm_no,p.next_due,a.asset_no,a.criticality FROM maintenance_plans p
           JOIN assets a ON a.id=p.asset_id
           WHERE p.active=1 AND p.trigger_type='Calendar'
             AND p.next_due IS NOT NULL AND p.next_due<?
             AND a.criticality IN ('Critical','High')
           ORDER BY p.next_due ASC LIMIT 15''',
        (today,),
    )):
        emit({
            'recommendation_id': f"critical-pm-overdue:{p['pm_no']}",
            'rule_id': 'critical-pm-overdue',
            'severity': 'High' if p['criticality'] == 'Critical' else 'Medium',
            'entity_type': 'maintenance_plan',
            'entity_id': 0,
            'entity_no': p['pm_no'],
            'reason': f"Preventive maintenance on {p['criticality']} asset {p['asset_no']} is past due",
            'recommended_action': 'Escalate scheduling priority for the overdue PM cycle',
            'evidence': [f"plan {p['pm_no']} due since {p['next_due']} on {p['asset_no']}"],
            'actions': [],
        })

    severity_rank = {'Critical': 3, 'High': 2, 'Medium': 1}
    out.sort(key=lambda r: (-severity_rank[r['severity']], r['rule_id'], r['entity_no']))
    return {
        'generated_at': generated,
        'total': len(out),
        'recommendations': out,
        'identity_note': 'recommendation_id = rule + entity number; stable across reads',
    }


# ---------------------------------------------------------------------------
# Operational dependency / blocker graph
# ---------------------------------------------------------------------------
def blocker_chain(conn, wo_id: int) -> dict:
    wo = conn.execute(
        '''SELECT w.*,a.asset_no,a.name asset_name FROM work_orders w
           LEFT JOIN assets a ON a.id=w.asset_id WHERE w.id=?''',
        (wo_id,),
    ).fetchone()
    if not wo:
        raise HTTPException(404, 'Work order not found')
    wo = dict(wo)
    stages: list[dict] = []

    upstream = []
    for o in _rows(conn.execute(
        'SELECT outage_no,status,start_at,end_at,outage_type FROM asset_outages WHERE work_order_id=? OR (asset_id=? AND status=\'Open\')',
        (wo_id, wo['asset_id']),
    )):
        upstream.append({
            'node': f"outage:{o['outage_no']}",
            'state': 'active' if o['status'] == 'Open' else 'resolved',
            'detail': f"{o['outage_type']} outage started {o['start_at']}",
        })
    stages.append({'stage': 'source_events', 'nodes': upstream or []})

    assignment = []
    if wo.get('assigned_to'):
        tech = conn.execute('SELECT full_name FROM users WHERE id=?', (wo['assigned_to'],)).fetchone()
        assignment.append({
            'node': f"user:{wo['assigned_to']}",
            'state': 'ok',
            'detail': f"assigned to {tech['full_name'] if tech else 'unknown'}",
        })
    for d in _rows(conn.execute(
        'SELECT dispatch_no,status,technician_user_id FROM dispatch_assignments WHERE work_order_id=?',
        (wo_id,),
    )):
        assignment.append({
            'node': f"dispatch:{d['dispatch_no']}",
            'state': 'active' if d['status'] not in ('Completed', 'Cancelled') else 'done',
            'detail': f"dispatch {d['status']}",
        })
    stages.append({'stage': 'assignment', 'nodes': assignment})

    requirements = _rows(conn.execute(
        '''SELECT rr.id,rr.quantity,ii.id item_id,ii.item_no,ii.name,ii.current_stock,
                  ii.reserved_stock
           FROM work_order_requirements rr JOIN inventory_items ii ON ii.id=rr.inventory_item_id
           WHERE rr.work_order_id=? AND rr.status<>'Cancelled' ''',
        (wo_id,),
    ))
    material_nodes = []
    all_short = True if requirements else False
    for rr in requirements:
        issued = float(conn.execute(
            'SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',
            (wo_id, rr['item_id']),
        ).fetchone()[0])
        reserved = float(conn.execute(
            '''SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
               WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')''',
            (wo_id, rr['item_id']),
        ).fetchone()[0])
        remaining = float(rr['quantity']) - issued
        secured = reserved + max(0.0, float(rr['current_stock']) - float(rr['reserved_stock']))
        short = remaining > secured + 1e-9
        if not short:
            all_short = False
        node = {
            'node': f"item:{rr['item_no']}",
            'state': 'blocked' if short else 'ok',
            'detail': (
                f"need {remaining:g}, secured {min(secured, remaining):g} "
                f"(stock {rr['current_stock']:g}, reserved elsewhere {rr['reserved_stock']:g})"
            ),
        }
        if short:
            pr = conn.execute(
                '''SELECT pr.pr_no,pr.status,po.po_no,po.status po_status,v.name vendor,
                          po.expected_delivery
                   FROM purchase_requisitions pr
                   JOIN purchase_requisition_items pri ON pri.pr_id=pr.id
                   LEFT JOIN purchase_orders po ON po.pr_id=pr.id
                   LEFT JOIN vendors v ON v.id=po.vendor_id
                   WHERE pri.inventory_item_id=? AND pr.status NOT IN ('Received','Cancelled','Rejected')
                   ORDER BY pr.id DESC LIMIT 1''',
                (rr['item_id'],),
            ).fetchone()
            if pr:
                node['downstream'] = {
                    'node': f"requisition:{pr['pr_no']}",
                    'state': 'pending',
                    'detail': (
                        f"requisition {pr['status']}"
                        + (f" → PO {pr['po_no']} ({pr['po_status']})" if pr['po_no'] else '')
                        + (f", vendor {pr['vendor']}" if pr['vendor'] else '')
                        + (f", ETA {pr['expected_delivery']}" if pr['expected_delivery'] else '')
                    ),
                }
            else:
                node['downstream'] = {
                    'node': 'requisition:none',
                    'state': 'missing',
                    'detail': 'no open requisition covers this part',
                }
        material_nodes.append(node)
    stages.append({
        'stage': 'materials',
        'nodes': material_nodes,
        'state': 'blocked' if (requirements and all_short) else ('partial' if any(n['state'] == 'blocked' for n in material_nodes) else 'ok'),
    })

    approval = conn.execute(
        "SELECT approval_no,status FROM approval_requests WHERE module='Work Management' AND record_code=? AND status='Pending'",
        (wo['wo_no'],),
    ).fetchone()
    gate = []
    if approval:
        gate.append({
            'node': f"approval:{approval['approval_no']}",
            'state': 'pending',
            'detail': 'awaiting approval decision',
        })
    stages.append({'stage': 'governance', 'nodes': gate})

    return {
        'work_order': {
            'id': wo_id,
            'wo_no': wo['wo_no'],
            'title': wo['title'],
            'priority': wo['priority'],
            'status': wo['status'],
            'asset_no': wo.get('asset_no'),
        },
        'stages': stages,
        'generated_at': _now_iso(),
    }


# ---------------------------------------------------------------------------
# Operations inbox (role-aware, flood-safe)
# ---------------------------------------------------------------------------
_INBOX_CAP = 20
_ALARM_OWNER_ROLES = ('admin', 'maintenance_manager', 'asset_manager')
_PLANNER_ROLES = ('admin', 'maintenance_manager', 'planner')
_STORE_ROLES = ('admin', 'storekeeper', 'procurement', 'maintenance_manager')


def _dedupe(entries: list[dict]) -> list[dict]:
    """Keep one entry per (entity_type, entity_id); first occurrence wins."""
    seen: set[tuple] = set()
    out = []
    for e in entries:
        key = (e.get('entity_type'), int(e.get('entity_id') or 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _age_days(stamp) -> Optional[float]:
    ts = _parse_ts(stamp)
    if not ts:
        return None
    return round(max(0.0, (datetime.now() - ts).total_seconds() / 86400.0), 1)


def _operations_inbox(conn, user: dict) -> dict:
    uid = int(user['id'])
    role = str(user.get('role') or '')
    generated = _now_iso()
    today = _application.date.today().isoformat()

    my_actions: dict[str, list] = {}
    my_risks: dict[str, list] = {}
    system_events: list[dict] = []

    # Approvals waiting on me (by user or by role).
    approvals = _rows(conn.execute(
        '''SELECT a.id,a.approval_no,a.module,a.record_code,a.title,a.requested_at,
                  a.assigned_role,a.assigned_user_id
           FROM approval_requests a
           WHERE a.status='Pending'
             AND (a.assigned_user_id=? OR (a.assigned_role=? AND a.assigned_user_id IS NULL))
           ORDER BY a.requested_at ASC LIMIT ?''',
        (uid, role, _INBOX_CAP),
    ))
    if approvals:
        my_actions['approvals'] = _dedupe([
            {
                **a,
                'reason': f"pending since {a['requested_at']}",
                'age_days': _age_days(a['requested_at']),
                'owner': a.get('assigned_role') or (
                    f"user:{a['assigned_user_id']}" if a.get('assigned_user_id') else '-'),
                'severity': 'High' if (_age_days(a['requested_at']) or 0) > 3 else 'Medium',
                'primary_action': 'decide',
                'entity_type': 'approval_request',
                'entity_id': int(a['id']),
            }
            for a in approvals
        ])

    # Work assigned to me and still open.
    my_work = _rows(conn.execute(
        '''SELECT w.id,w.wo_no,w.title,w.priority,w.status,w.target_finish,
                  sl.resolution_status,sl.response_due,sl.resolution_due
           FROM work_orders w LEFT JOIN work_order_sla sl ON sl.work_order_id=w.id
           WHERE w.assigned_to=? AND w.status NOT IN ('Completed','Closed','Cancelled')
           ORDER BY CASE w.priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4
                    WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC, w.id
           LIMIT ?''',
        (uid, _INBOX_CAP),
    ))
    if my_work:
        now_ts = datetime.now()
        items = []
        for w in my_work:
            overdue = bool(w['target_finish'] and str(w['target_finish'])[:10] < today)
            sla_risk = bool(
                w['resolution_due'] and not overdue
                and _parse_ts(w['resolution_due'])
                and _parse_ts(w['resolution_due']) < now_ts + timedelta(hours=24)
                and w['resolution_status'] != 'Breached'
            )
            items.append({
                **w,
                'overdue': overdue,
                'sla_24h_risk': sla_risk,
                'severity': ('Critical' if (overdue and w['priority'] in ('Emergency', 'Critical'))
                             else ('High' if overdue or sla_risk else 'Medium')),
                'age_days': _age_days(w['target_finish']),
                'reason': (
                    'past target finish'
                    + (', resolution SLA at risk within 24h' if sla_risk else '')
                ) if (overdue or sla_risk) else 'open assigned work',
                'owner': 'me',
                'primary_action': 'execute',
                'entity_type': 'work_order',
                'entity_id': int(w['id']),
            })
        my_actions['assigned_work'] = _dedupe(items)

    # Condition alarms awaiting acknowledgment for operating roles.
    if role in _ALARM_OWNER_ROLES:
        alarms = _rows(conn.execute(
            '''SELECT oa.id,oa.alarm_no,oa.severity,oa.message,oa.opened_at,a.asset_no
               FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id
               WHERE oa.status='Open' AND oa.severity IN ('Critical','Warning')
               ORDER BY CASE oa.severity WHEN 'Critical' THEN 1 ELSE 2 END,
                        oa.opened_at ASC LIMIT ?''',
            (_INBOX_CAP,),
        ))
        if alarms:
            my_actions['alarms_to_acknowledge'] = _dedupe([
                {
                    **a,
                    'age_hours': round((
                        (datetime.now() - _parse_ts(a['opened_at'])).total_seconds() / 3600.0
                    ) if _parse_ts(a['opened_at']) else 0, 1),
                    'reason': 'unacknowledged condition alarm',
                    'owner': 'alarm-owner roles',
                    'primary_action': 'acknowledge',
                    'entity_type': 'operational_alarm',
                    'entity_id': int(a['id']),
                }
                for a in alarms
            ])

    # SLA escalations touching my work or my span of control.
    if role in _ALARM_OWNER_ROLES + ('supervisor', 'technician'):
        breaches = _rows(conn.execute(
            '''SELECT sl.work_order_id,w.wo_no,w.title,w.assigned_to,
                      sl.response_status,sl.resolution_status,sl.escalated_level
               FROM work_order_sla sl JOIN work_orders w ON w.id=sl.work_order_id
               WHERE sl.response_status='Breached' OR sl.resolution_status='Breached'
                  OR sl.escalated_level>0
               ORDER BY sl.updated_at DESC LIMIT ?''',
            (_INBOX_CAP,),
        ))
        mine = [b for b in breaches if b['assigned_to'] == uid] or breaches
        if mine:
            my_risks['sla_escalations'] = _dedupe([
                {
                    **b,
                    'severity': 'Critical' if b['resolution_status'] == 'Breached' else 'High',
                    'reason': f"SLA state: {b['response_status']}/{b['resolution_status']}"
                              + (f", escalated level {b['escalated_level']}" if b['escalated_level'] else ''),
                    'owner': f"user:{b['assigned_to']}" if b['assigned_to'] else 'unassigned',
                    'due_state': b['resolution_status'] or b['response_status'],
                    'primary_action': 'expedite',
                    'entity_type': 'work_order',
                    'entity_id': int(b['work_order_id']),
                }
                for b in mine[:_INBOX_CAP]
            ])

    # Deteriorating critical assets for asset-owner roles.
    if role in _ALARM_OWNER_ROLES:
        deteriorating = _rows(conn.execute(
            '''SELECT s.asset_id,s.score,s.risk_band,s.calculated_at,a.asset_no,a.name asset_name
               FROM asset_health_snapshots s JOIN assets a ON a.id=s.asset_id
               WHERE s.id IN (SELECT MAX(id) FROM asset_health_snapshots GROUP BY asset_id)
                 AND s.score<=50 AND a.criticality IN ('Critical','High')
               ORDER BY s.score ASC LIMIT ?''',
            (_INBOX_CAP,),
        ))
        if deteriorating:
            my_risks['deterioration_watchlist'] = _dedupe([
                {
                    **d,
                    'severity': 'Critical' if float(d['score']) <= 35 else 'High',
                    'reason': f"health score {float(d['score']):g} ({d['risk_band']}) on high-consequence asset",
                    'owner': 'asset management',
                    'primary_action': 'inspect',
                    'entity_type': 'asset',
                    'entity_id': int(d['asset_id']),
                }
                for d in deteriorating
            ])

    # Overdue jobs I own (already in assigned_work but surfaced as risk too).
    if my_work:
        overdue = [
            {
                'wo_no': w['wo_no'],
                'target_finish': w['target_finish'],
                'priority': w['priority'],
                'title': w['title'],
                'severity': 'Critical' if w['priority'] in ('Emergency', 'Critical') else 'High',
                'reason': 'past target finish while assigned to me',
                'owner': 'me',
                'primary_action': 're-plan or execute',
                'entity_type': 'work_order',
                'entity_id': int(w['id']),
            }
            for w in my_work
            if w['target_finish'] and str(w['target_finish'])[:10] < today
        ]
        if overdue:
            my_risks['my_overdue_jobs'] = _dedupe(overdue)

    # Material shortages for store/procurement roles.
    if role in _STORE_ROLES:
        shortages = _rows(conn.execute(
            '''SELECT id,item_no,name,current_stock,reserved_stock,reorder_point,min_level
               FROM inventory_items WHERE current_stock-reserved_stock<=reorder_point
               ORDER BY current_stock-reserved_stock ASC LIMIT ?''',
            (_INBOX_CAP,),
        ))
        if shortages:
            my_risks['stock_shortages'] = _dedupe([
                {
                    **s,
                    'available': float(s['current_stock']) - float(s['reserved_stock']),
                    'severity': ('High' if float(s['current_stock']) - float(s['reserved_stock'])
                                 <= float(s['min_level']) else 'Medium'),
                    'reason': f"available {float(s['current_stock']) - float(s['reserved_stock']):g} at/below reorder point {float(s['reorder_point']):g}",
                    'owner': 'storekeeping/procurement',
                    'primary_action': 'replenish',
                    'entity_type': 'inventory_item',
                    'entity_id': int(s['id']),
                }
                for s in shortages
            ])

    # System events: outages opened/restored in the last 24 hours.
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec='seconds')
    outages = _rows(conn.execute(
        '''SELECT o.outage_no,o.status,o.start_at,o.end_at,a.asset_no
           FROM asset_outages o JOIN assets a ON a.id=o.asset_id
           WHERE o.start_at>=? OR (o.end_at IS NOT NULL AND o.end_at>=?)
           ORDER BY COALESCE(o.end_at,o.start_at) DESC LIMIT 10''',
        (cutoff, cutoff),
    ))
    for o in outages:
        restored = o['end_at'] is not None and o['status'] != 'Open'
        system_events.append({
            'kind': 'outage_restored' if restored else 'outage_open',
            'label': f"{o['outage_no']} on {o['asset_no']} "
                     + ('restored' if restored else 'opened'),
            'ts': o['end_at'] or o['start_at'],
            'entity_type': 'outage',
            'entity_no': o['outage_no'],
        })
    system_events.sort(key=lambda e: e['ts'], reverse=True)

    action_count = sum(len(v) for v in my_actions.values())
    risk_count = sum(len(v) for v in my_risks.values())
    return {
        'generated_at': generated,
        'role': role,
        'counts': {'actions': action_count, 'risks': risk_count, 'events': len(system_events)},
        'my_actions': my_actions,
        'my_risks': my_risks,
        'system_events': system_events,
    }


# ---------------------------------------------------------------------------
# Command search across operational entities
# ---------------------------------------------------------------------------
_SEARCH_SPECS = (
    # kind, source, columns, match predicate, site-scope predicate or None
    ('asset', 'assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id',
     'a.id,a.asset_no no,a.name title,a.status detail',
     '(a.asset_no LIKE ? OR a.name LIKE ?)', 's.id=?'),
    ('site', 'sites s',
     's.id,s.site_code no,s.name title,s.region detail',
     '(s.site_code LIKE ? OR s.name LIKE ?)', None),
    ('work_order', 'work_orders w LEFT JOIN locations l ON l.id=w.location_id',
     "w.id,w.wo_no no,w.title title,w.status detail",
     '(w.wo_no LIKE ? OR w.title LIKE ?)', 'l.site_id=?'),
    ('purchase_order', 'purchase_orders p',
     'p.id,p.po_no no,p.status title,p.order_date detail',
     'p.po_no LIKE ?', None),
    ('requisition', 'purchase_requisitions p',
     'p.id,p.pr_no no,p.title title,p.status detail',
     '(p.pr_no LIKE ? OR p.title LIKE ?)', None),
    ('alarm', 'operational_alarms oa JOIN assets a ON a.id=oa.asset_id '
              'LEFT JOIN locations l ON l.id=a.location_id',
     'oa.id,oa.alarm_no no,oa.message title,oa.status detail',
     '(oa.alarm_no LIKE ? OR oa.message LIKE ?)', 'l.site_id=?'),
    ('employee', 'users u LEFT JOIN roles r ON r.id=u.role_id',
     'u.id,u.username no,u.full_name title,r.code detail',
     '(u.full_name LIKE ? OR u.username LIKE ?) AND u.active=1', None),
    ('location', 'locations l',
     'l.id,l.location_code no,l.name title,l.location_type detail',
     '(l.location_code LIKE ? OR l.name LIKE ?)', 'l.site_id=?'),
)


def _search_score(term: str, record: dict) -> int:
    """0 exact identifier, 1 identifier prefix, 2 weak textual match."""
    no_field = str(record.get('no') or '')
    if no_field.lower() == term.lower():
        return 0
    if no_field.lower().startswith(term.lower()):
        return 1
    return 2


def command_search(conn, q: str, limit_per_type: int = 5,
                   site_id: Optional[int] = None) -> dict:
    term = (q or '').strip()
    if not term:
        return {'query': '', 'results': {}, 'total': 0}
    like = f'%{term}%'
    results: dict[str, list] = {}
    total = 0
    for kind, source, columns, predicate, scope in _SEARCH_SPECS:
        sql = f'SELECT {columns} FROM {source} WHERE {predicate}'
        args: list = [like] * predicate.count('?')
        if site_id is not None and scope:
            sql += f' AND {scope}'
            args.append(site_id)
        # Fetch a bounded candidate window, then rank deterministically.
        sql += ' ORDER BY 1 ASC LIMIT 50'
        rows_ = _rows(conn.execute(sql, tuple(args)))
        if not rows_:
            continue
        for e in rows_:
            e['score'] = _search_score(term, e)
        rows_.sort(key=lambda e: (e['score'], int(e['id'])))
        entries = [
            {
                **{k: v for k, v in e.items() if k != 'score'},
                'entity_type': kind,
                'entity_id': int(e['id']),
            }
            for e in rows_[:limit_per_type]
        ]
        results[kind] = entries
        total += len(entries)
    ordered_results = {kind: results[kind] for kind in sorted(results)}
    return {'query': term, 'results': ordered_results, 'total': total}


def install_operations_routes() -> None:
    app = _application.app
    marker = '_euas_operations_routes'
    if getattr(app.state, marker, False):
        return

    @app.get('/api/operations/situations')
    def operations_situations_route(
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return situations_view(conn, site_id)

    @app.get('/api/operations/situations/{key:path}/timeline')
    def operations_timeline_route(key: str, user=Depends(current_user)):
        with db() as conn:
            return situation_timeline(conn, key)

    @app.get('/api/operations/why-red')
    def operations_why_red_route(
        key: str,
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return why_red(conn, key, site_id)

    @app.get('/api/operations/recommendations')
    def operations_recommendations_route(user=Depends(current_user)):
        with db() as conn:
            return recommendations_view(conn)

    @app.get('/api/operations/blocker-chain/{wo_id}')
    def operations_blocker_chain_route(wo_id: int, user=Depends(current_user)):
        with db() as conn:
            return blocker_chain(conn, wo_id)

    @app.get('/api/operations/inbox')
    def operations_inbox_route(user=Depends(current_user)):
        with db() as conn:
            return _operations_inbox(conn, user)

    @app.get('/api/command-search')
    def command_search_route(
        q: str = '',
        limit: int = Query(5, ge=1, le=20),
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return command_search(conn, q, limit, site_id=site_id)

    app.openapi_schema = None
    setattr(app.state, marker, True)
