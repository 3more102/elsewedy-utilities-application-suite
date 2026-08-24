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

from fastapi import Depends, HTTPException

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
                   w.assigned_to,w.target_finish,w.created_at,u.full_name assigned_name
            FROM work_orders w LEFT JOIN users u ON u.id=w.assigned_to
            WHERE w.asset_id IN ({marks})
              AND w.status NOT IN ('Completed','Closed','Cancelled')
            ORDER BY CASE w.priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4
                     WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC, w.id''',
        asset_ids,
    )):
        grouped.setdefault(int(row['asset_id']), []).append(row)
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

        result.append({
            'situation_key': key,
            'anchor_type': sit['anchor_type'],
            'anchor_no': sit['anchor_no'],
            **asset_ref,
            'severity': severity,
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
            'work_orders': work,
            'material_blockers': shortest,
            'restoration': _restoration_progress(work),
        })

    result.sort(key=lambda s: (-_SEVERITY_RANK[s['severity']], s['started_at'] or '', s['anchor_no']))
    return result


def situations_view(conn, site_id: Optional[int] = None) -> dict:
    situations = _situations(conn, site_id)
    counts: dict[str, int] = {'Critical': 0, 'High': 0, 'Medium': 0}
    for s in situations:
        counts[s['severity']] += 1
    return {
        'generated_at': _now_iso(),
        'total': len(situations),
        'severity_counts': counts,
        'situations': situations,
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

    events.sort(key=lambda e: e['ts'])
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
            for name, v in sorted(buckets.items(), key=lambda kv: -kv[1]['count'])
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
            for wid, shortage in sorted(blocker_map.items(), key=lambda kv: -kv[1]):
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
def recommendations_view(conn) -> dict:
    out: list[dict] = []
    generated = _now_iso()

    # R1: unacknowledged critical alarm with no linked work order.
    for a in _rows(conn.execute(
        '''SELECT oa.alarm_no,oa.opened_at,oa.message,a.asset_no,a.id asset_id
           FROM operational_alarms oa JOIN assets a ON a.id=oa.asset_id
           WHERE oa.status='Open' AND oa.severity='Critical' AND oa.work_order_id IS NULL
           ORDER BY oa.opened_at ASC LIMIT 25'''
    )):
        opened = _parse_ts(a['opened_at'])
        age_min = int((datetime.now() - opened).total_seconds() // 60) if opened else None
        out.append({
            'rule_id': 'critical-alarm-no-work',
            'severity': 'Critical',
            'entity_type': 'operational_alarm',
            'entity_no': a['alarm_no'],
            'reason': 'Critical condition alarm has no linked work order',
            'recommended_action': 'Acknowledge and create an emergency inspection or corrective work order',
            'evidence': [
                f"alarm {a['alarm_no']} open on {a['asset_no']}" + (f" for {age_min} minutes" if age_min is not None else ''),
                f"message: {a['message'][:80]}",
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
                '''SELECT ii.item_no,ii.name,
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
            evidence = [
                f"{w['wo_no']} ({w['priority']}) short on {int(item['still_required'])} x {item['item_no']} {item['name']}"
                for item in short_items[:4]
            ]
            action = 'Escalate procurement: expedite requisition or transfer stock'
            if pending_pr:
                evidence.append(f"requisition {pending_pr['pr_no']} already {pending_pr['status']}")
                action = 'Escalate the existing requisition/PO with the vendor for expedited delivery'
            out.append({
                'rule_id': 'critical-work-material-blocked',
                'severity': 'Critical' if w['priority'] in ('Emergency', 'Critical') else 'High',
                'entity_type': 'work_order',
                'entity_no': w['wo_no'],
                'reason': f'Recovery-critical work cannot execute: {shortage} part line(s) unavailable',
                'recommended_action': action,
                'evidence': evidence,
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
        out.append({
            'rule_id': 'repeat-failure-investigation',
            'severity': 'High',
            'entity_type': 'asset',
            'entity_no': r['asset_no'],
            'reason': f"{r['failures']} corrective completions in 90 days indicate a recurring failure mode",
            'recommended_action': 'Open a reliability investigation (FMEA review / root cause analysis)',
            'evidence': [f"{r['failures']} corrective/breakdown completions within the 90-day window"],
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
        out.append({
            'rule_id': 'critical-pm-overdue',
            'severity': 'High' if p['criticality'] == 'Critical' else 'Medium',
            'entity_type': 'maintenance_plan',
            'entity_no': p['pm_no'],
            'reason': f"Preventive maintenance on {p['criticality']} asset {p['asset_no']} is past due",
            'recommended_action': 'Escalate scheduling priority for the overdue PM cycle',
            'evidence': [f"plan {p['pm_no']} due since {p['next_due']} on {p['asset_no']}"],
        })

    severity_rank = {'Critical': 3, 'High': 2, 'Medium': 1}
    out.sort(key=lambda r: (-severity_rank[r['severity']], r['rule_id'], r['entity_no']))
    return {'generated_at': generated, 'total': len(out), 'recommendations': out}


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
        short = remaining > secured - 1e-9
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

    app.openapi_schema = None
    setattr(app.state, marker, True)
