from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from apps.audit import audit
from apps.events import emit_event
from core.database import now
from core.shared import next_no

SEVERITY_RANK = {'Info': 0, 'Warning': 1, 'Critical': 2}


class AlarmNotFound(LookupError):
    pass


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def _one(cursor):
    row = cursor.fetchone()
    return dict(row) if row else None


def _dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def topology_graph(conn):
    links = _rows(conn.execute("SELECT * FROM asset_topology_links WHERE active=1 ORDER BY id"))
    directed: dict[int, list[int]] = {}
    undirected: dict[int, list[int]] = {}
    for link in links:
        upstream = int(link['upstream_asset_id'])
        downstream = int(link['downstream_asset_id'])
        directed.setdefault(upstream, []).append(downstream)
        undirected.setdefault(upstream, []).append(downstream)
        undirected.setdefault(downstream, []).append(upstream)
    return links, directed, undirected


def graph_distance(graph: dict[int, list[int]], start: int, target: int, max_hops: int = 3):
    if start == target:
        return 0
    seen = {start}
    queue = deque([(start, 0)])
    while queue:
        node, hops = queue.popleft()
        if hops >= max_hops:
            continue
        for nxt in graph.get(node, []):
            if nxt == target:
                return hops + 1
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, hops + 1))
    return None


def incident_root_cause(conn, members: list[dict]):
    asset_ids = sorted({int(item['asset_id']) for item in members})
    if not asset_ids:
        return {'asset_id': None, 'asset_no': '', 'mode': 'Asset', 'score': 0, 'reason': 'No alarm members', 'hops': 0}
    placeholders = ','.join('?' for _ in asset_ids)
    meta = {
        int(row['id']): dict(row)
        for row in conn.execute(
            f'SELECT id,asset_no,name FROM assets WHERE id IN ({placeholders})', asset_ids
        ).fetchall()
    }
    if len(asset_ids) == 1:
        asset = meta[asset_ids[0]]
        return {
            'asset_id': asset_ids[0],
            'asset_no': asset['asset_no'],
            'mode': 'Asset',
            'score': 100.0,
            'reason': f"All correlated alarms originate from {asset['asset_no']}.",
            'hops': 0,
        }

    _, directed, undirected = topology_graph(conn)
    first_seen = {
        asset_id: min(_dt(item['opened_at']) for item in members if int(item['asset_id']) == asset_id)
        for asset_id in asset_ids
    }
    severity = {
        asset_id: max(
            (SEVERITY_RANK.get(item['severity'], 0) for item in members if int(item['asset_id']) == asset_id),
            default=0,
        )
        for asset_id in asset_ids
    }
    candidates = []
    for asset_id in asset_ids:
        directed_distances = [graph_distance(directed, asset_id, other, 3) for other in asset_ids if other != asset_id]
        downstream_count = sum(distance is not None for distance in directed_distances)
        connected_distances = [graph_distance(undirected, asset_id, other, 3) for other in asset_ids if other != asset_id]
        connected_count = sum(distance is not None for distance in connected_distances)
        candidates.append(
            (
                asset_id,
                downstream_count,
                connected_count,
                first_seen[asset_id],
                severity[asset_id],
                directed_distances,
                connected_distances,
            )
        )
    candidates.sort(key=lambda item: (-item[1], -item[2], item[3], -item[4], item[0]))
    asset_id, downstream_count, _, opened, _, directed_distances, connected_distances = candidates[0]
    other_count = max(len(asset_ids) - 1, 1)
    earliest = min(first_seen.values()) == opened
    score = min(95.0, 60.0 + 25.0 * (downstream_count / other_count) + (10.0 if earliest else 0.0))
    reachable = [distance for distance in directed_distances if distance is not None] or [
        distance for distance in connected_distances if distance is not None
    ]
    hops = max(reachable, default=0)
    asset = meta[asset_id]
    if downstream_count:
        reason = f"{asset['asset_no']} is upstream of {downstream_count} of {other_count} other alarmed asset(s) within the configured topology"
        if earliest:
            reason += ' and its alarm evidence appeared earliest'
        reason += '.'
    else:
        reason = f"{asset['asset_no']} is the earliest alarmed asset in the connected topology; no alarmed asset is upstream of another alarmed member."
    return {
        'asset_id': asset_id,
        'asset_no': asset['asset_no'],
        'mode': 'Topology',
        'score': round(score, 1),
        'reason': reason,
        'hops': hops,
    }


def incident_candidate_distance(conn, incident_id: int, asset_id: int, max_hops: int = 2):
    member_assets = [
        int(row['asset_id'])
        for row in conn.execute(
            'SELECT DISTINCT oa.asset_id FROM alarm_incident_members m JOIN operational_alarms oa ON oa.id=m.alarm_id WHERE m.incident_id=?',
            (incident_id,),
        ).fetchall()
    ]
    if asset_id in member_assets:
        return 0
    _, _, undirected = topology_graph(conn)
    distances = [graph_distance(undirected, asset_id, member, max_hops) for member in member_assets]
    distances = [distance for distance in distances if distance is not None]
    return min(distances) if distances else None


def incident_member_summary(conn, incident_id: int):
    members = _rows(
        conn.execute(
            """SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name
               FROM alarm_incident_members m JOIN operational_alarms oa ON oa.id=m.alarm_id
               JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id
               WHERE m.incident_id=? ORDER BY oa.opened_at""",
            (incident_id,),
        )
    )
    active = [item for item in members if item['status'] in ('Open', 'Acknowledged')]
    severity = max(
        (item['severity'] for item in members),
        key=lambda value: SEVERITY_RANK.get(value, 0),
        default='Warning',
    )
    return members, active, severity


def refresh_incident(conn, incident_id: int, actor_id: int | None = None):
    row = conn.execute('SELECT * FROM alarm_incidents WHERE id=?', (incident_id,)).fetchone()
    if not row:
        return None
    incident = dict(row)
    members, active, severity = incident_member_summary(conn, incident_id)
    last_seen = max((item['last_seen_at'] for item in members), default=incident['last_seen_at'])
    root = incident_root_cause(conn, members)
    title = incident['title']
    key = incident['correlation_key']
    if root['asset_id']:
        if root['mode'] == 'Topology':
            title = f"{root['asset_no']} topology-correlated operational incident"
            key = f"topology:{root['asset_id']}"
        else:
            title = f"{root['asset_no']} operational alarm incident"
            key = f"asset:{root['asset_id']}"
    updates = {
        'severity': severity,
        'alarm_count': len(members),
        'last_seen_at': last_seen,
        'root_cause_asset_id': root['asset_id'],
        'correlation_mode': root['mode'],
        'root_cause_score': root['score'],
        'root_cause_reason': root['reason'],
        'topology_hops': root['hops'],
        'title': title,
        'correlation_key': key,
        'updated_at': now(),
    }
    if not active and incident['status'] in ('Open', 'Acknowledged'):
        updates['status'] = 'Resolved'
        updates['resolved_at'] = now()
        updates['resolved_by'] = actor_id
    conn.execute(
        'UPDATE alarm_incidents SET ' + ','.join(f'{column}=?' for column in updates) + ' WHERE id=?',
        (*updates.values(), incident_id),
    )
    updated = _one(conn.execute('SELECT * FROM alarm_incidents WHERE id=?', (incident_id,)))
    if root['mode'] == 'Topology' and incident.get('root_cause_asset_id') not in (None, root['asset_id']):
        emit_event(
            conn,
            'operations.incident.root_cause_updated',
            'alarm_incident',
            incident['incident_no'],
            {
                'incident_no': incident['incident_no'],
                'root_cause_asset_id': root['asset_id'],
                'score': root['score'],
                'reason': root['reason'],
            },
        )
        if actor_id:
            audit(
                conn,
                actor_id,
                'UPDATE INCIDENT ROOT CAUSE',
                'Utilities Operations',
                incident['incident_no'],
                incident.get('root_cause_asset_id'),
                root,
            )
    if incident['status'] in ('Open', 'Acknowledged') and updated and updated['status'] == 'Resolved':
        emit_event(
            conn,
            'operations.incident.auto_resolved',
            'alarm_incident',
            incident['incident_no'],
            {'incident_no': incident['incident_no'], 'alarm_count': len(members)},
        )
        if actor_id:
            audit(
                conn,
                actor_id,
                'AUTO RESOLVE INCIDENT',
                'Utilities Operations',
                incident['incident_no'],
                incident['status'],
                'Resolved',
            )
    return updated


def correlate_alarm(
    conn,
    alarm_id: int,
    actor_id: int | None = None,
    *,
    notify: Callable[..., object] | None = None,
):
    row = conn.execute(
        """SELECT oa.*,a.asset_no,a.name asset_name FROM operational_alarms oa
           JOIN assets a ON a.id=oa.asset_id WHERE oa.id=?""",
        (alarm_id,),
    ).fetchone()
    if not row:
        raise AlarmNotFound('Alarm not found')
    alarm = dict(row)
    existing = conn.execute(
        'SELECT incident_id FROM alarm_incident_members WHERE alarm_id=? ORDER BY incident_id DESC LIMIT 1',
        (alarm_id,),
    ).fetchone()
    if existing:
        return refresh_incident(conn, existing['incident_id'], actor_id)

    cutoff = (_dt(alarm['last_seen_at']) - timedelta(minutes=30)).isoformat(timespec='seconds')
    sql = "SELECT * FROM alarm_incidents WHERE status IN ('Open','Acknowledged') AND last_seen_at>=?"
    args: list[object] = [cutoff]
    if alarm.get('site_id') is None:
        sql += ' AND site_id IS NULL'
    else:
        sql += ' AND site_id=?'
        args.append(alarm['site_id'])
    candidates = []
    for incident in _rows(conn.execute(sql, args)):
        distance = incident_candidate_distance(conn, incident['id'], alarm['asset_id'], 1)
        if distance is not None:
            candidates.append((distance, -_dt(incident['last_seen_at']).timestamp(), incident))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]['id']))
    incident = candidates[0][2] if candidates else None
    if not incident:
        incident_no = next_no(conn, 'alarm_incidents', 'incident_no', 'INC-', 60001)
        cur = conn.execute(
            """INSERT INTO alarm_incidents(incident_no,correlation_key,site_id,asset_id,title,severity,status,opened_at,last_seen_at,alarm_count,root_cause_asset_id,correlation_mode,root_cause_score,root_cause_reason,topology_hops,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'Open',?,?,0,?,'Asset',100,?,0,?,?)""",
            (
                incident_no,
                f"asset:{alarm['asset_id']}",
                alarm.get('site_id'),
                alarm['asset_id'],
                f"{alarm['asset_no']} operational alarm incident",
                alarm['severity'],
                alarm['opened_at'],
                alarm['last_seen_at'],
                alarm['asset_id'],
                f"Initial alarm evidence originates from {alarm['asset_no']}.",
                now(),
                now(),
            ),
        )
        incident_id = cur.lastrowid
        emit_event(
            conn,
            'operations.incident.opened',
            'alarm_incident',
            incident_no,
            {
                'incident_no': incident_no,
                'asset_id': alarm['asset_id'],
                'alarm_no': alarm['alarm_no'],
                'severity': alarm['severity'],
            },
        )
        if notify is not None:
            notify(
                conn,
                'Operational incident',
                f"{incident_no} — {alarm['asset_no']} correlated alarm incident",
                alarm['severity'],
                None,
                'maintenance_manager',
                'commandcenter',
                incident_no,
            )
        if actor_id:
            audit(
                conn,
                actor_id,
                'INCIDENT OPEN',
                'Utilities Operations',
                incident_no,
                '',
                {'alarm': alarm['alarm_no'], 'asset': alarm['asset_no']},
            )
    else:
        incident_id = incident['id']
        incident_no = incident['incident_no']

    conn.execute(
        'INSERT OR IGNORE INTO alarm_incident_members(incident_id,alarm_id,added_at) VALUES(?,?,?)',
        (incident_id, alarm_id, now()),
    )
    updated = refresh_incident(conn, incident_id, actor_id)
    if updated and updated.get('correlation_mode') == 'Topology':
        emit_event(
            conn,
            'operations.incident.topology_correlated',
            'alarm_incident',
            incident_no,
            {
                'incident_no': incident_no,
                'alarm_no': alarm['alarm_no'],
                'asset_id': alarm['asset_id'],
                'root_cause_asset_id': updated.get('root_cause_asset_id'),
                'score': updated.get('root_cause_score'),
            },
        )
    return updated


def refresh_incidents_for_alarm(conn, alarm_id: int, actor_id: int | None = None):
    result = []
    for row in conn.execute(
        'SELECT incident_id FROM alarm_incident_members WHERE alarm_id=?', (alarm_id,)
    ).fetchall():
        updated = refresh_incident(conn, row['incident_id'], actor_id)
        if updated:
            result.append(updated)
    return result
