"""EUAS Asset Performance Management domain store.

Database-backed APM services built on the deterministic kernel in
``reliability.py``:

* explainable asset health (extends the historical penalty score)
* likelihood x consequence risk, separate from health
* deterioration watchlist over real telemetry
* alarm correlation / recurrence / burst analysis
* condition-based maintenance recommendation lifecycle
* reliability bad-actor ranking with drill-down
* FMEA records with observed-evidence comparison
* post-maintenance effectiveness verdicts

All analysis is read-only against operational tables; recommendations and
FMEA records are first-class persisted entities with their own workflow.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException

from . import application as _application
from . import reliability as rl
from .auth import current_user, require_roles
from .database import db, now

RELIABILITY_MANAGE_ROLES = ('admin', 'asset_manager', 'maintenance_manager')

_FAILURE_WORK_TYPES = ('Corrective', 'Emergency')
_SEVERITY_PRIORITY = {'Critical': 'Critical', 'High': 'High', 'Medium': 'Medium', 'Low': 'Low'}


def _rows(cur):
    return _application.rows(cur)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Health & risk evidence
# ---------------------------------------------------------------------------
def _channel_condition_level(conn, channel: dict, *, window_days: int = 30) -> tuple[str, list[str]]:
    cutoff = _iso(datetime.now() - timedelta(days=window_days))
    readings = [
        float(r['value'])
        for r in _rows(conn.execute(
            '''SELECT value FROM telemetry_readings
               WHERE channel_id=? AND captured_at>=?
               ORDER BY captured_at ASC, id ASC''',
            (channel['id'], cutoff),
        ))
    ]
    thresholds = {
        'warning_low': channel.get('warning_low'),
        'critical_low': channel.get('critical_low'),
        'warning_high': channel.get('warning_high'),
        'critical_high': channel.get('critical_high'),
    }
    verdict = rl.evaluate_channel_condition(readings, thresholds)
    return verdict['level'], verdict['signals']


def _asset_trend_level(conn, asset_id: int) -> tuple[str, list[dict]]:
    worst = 'none'
    rank = {'none': 0, 'adverse': 1, 'severe': 2}
    findings: list[dict] = []
    for channel in _rows(conn.execute(
        'SELECT * FROM telemetry_channels WHERE asset_id=? AND active=1 ORDER BY channel_code',
        (asset_id,),
    )):
        level, signals = _channel_condition_level(conn, channel)
        if signals:
            findings.append({
                'channel_id': channel['id'],
                'channel_code': channel['channel_code'],
                'name': channel['name'],
                'level': level,
                'signals': signals,
            })
        if rank[level] > rank[worst]:
            worst = level
    return worst, findings


def gather_health_evidence(conn, asset: dict, *, trend_level: Optional[str] = None) -> dict:
    """Collect every health/risk input for one asset with plain queries."""
    open_work = _rows(conn.execute(
        "SELECT priority,target_finish,status FROM work_orders "
        "WHERE asset_id=? AND status NOT IN ('Completed','Closed','Cancelled')",
        (asset['id'],),
    ))
    failed_inspections = int(conn.execute(
        "SELECT COUNT(*) FROM inspections WHERE asset_id=? AND result='Fail'",
        (asset['id'],),
    ).fetchone()[0])
    sla_breaches = int(conn.execute(
        "SELECT COUNT(*) FROM work_order_sla s JOIN work_orders w ON w.id=s.work_order_id "
        "WHERE w.asset_id=? AND (s.response_status='Breached' OR s.resolution_status='Breached')",
        (asset['id'],),
    ).fetchone()[0])
    alarm_rows = _rows(conn.execute(
        "SELECT severity FROM operational_alarms "
        "WHERE asset_id=? AND status IN ('Open','Acknowledged')",
        (asset['id'],),
    ))
    window_start = _iso(datetime.now() - timedelta(days=90))
    failures_window = int(conn.execute(
        "SELECT COUNT(*) FROM work_orders WHERE asset_id=? AND work_type IN (?,?) "
        "AND status IN ('Completed','Closed') "
        "AND COALESCE(actual_finish,updated_at)>=?",
        (asset['id'], *_FAILURE_WORK_TYPES, window_start),
    ).fetchone()[0])

    downtime_hours = 0.0
    ref = datetime.now()
    for outage in _rows(conn.execute(
        "SELECT start_at,end_at,status FROM asset_outages "
        "WHERE asset_id=? AND outage_type='Forced' AND start_at>=?",
        (asset['id'], window_start),
    )):
        started = _parse_ts(outage['start_at'])
        ended = _parse_ts(outage['end_at'])
        if not started:
            continue
        if outage['status'] == 'Open' or ended is None:
            ended = ref
        downtime_hours += max(0.0, (ended - started).total_seconds() / 3600.0)

    if trend_level is None:
        trend_level, _findings = _asset_trend_level(conn, asset['id'])

    return {
        'condition': asset.get('condition'),
        'criticality': asset.get('criticality'),
        'status': asset.get('status'),
        'open_work': [dict(w) for w in open_work],
        'failed_inspections': failed_inspections,
        'sla_breaches': sla_breaches,
        'active_alarms': len(alarm_rows),
        'active_critical_alarms': sum(1 for a in alarm_rows if a['severity'] == 'Critical'),
        'failures_window': failures_window,
        'trend_level': trend_level,
        'downtime_hours': round(downtime_hours, 1),
        'today': _application.date.today().isoformat(),
    }


def explain_asset_health_view(conn, asset_id: int) -> dict:
    asset = _application.get_or_404(
        conn,
        "SELECT a.*,l.name location_name,s.name site_name FROM assets a "
        "LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id "
        "WHERE a.id=?",
        (asset_id,),
        'Asset not found',
    )
    trend_level, channel_findings = _asset_trend_level(conn, asset_id)
    evidence = gather_health_evidence(conn, asset, trend_level=trend_level)
    health = rl.compute_asset_health(evidence)

    detail_map = {
        'condition': f"recorded asset condition {asset.get('condition')}",
        'criticality': f"asset criticality {asset.get('criticality')}",
        'priority_work': f"{sum(1 for w in evidence['open_work'] if w.get('priority') in ('Emergency','Critical','High'))} high-priority open work order(s)",
        'overdue_work': f"{sum(1 for w in evidence['open_work'] if str(w.get('target_finish') or '') < evidence['today'])} overdue work order(s)",
        'failed_inspections': f"{evidence['failed_inspections']} failed inspection(s)",
        'sla_breaches': f"{evidence['sla_breaches']} breached SLA row(s)",
        'operational_alarms': (
            f"{evidence['active_alarms']} active alarm(s), "
            f"{evidence['active_critical_alarms']} critical"
        ),
        'repeat_failures': (
            f"{evidence['failures_window']} corrective/emergency completion(s) in 90 days"
        ),
        'deterioration': f"telemetry deterioration level {trend_level}",
        'downtime_90d': f"{evidence['downtime_hours']}h forced downtime in 90 days",
    }
    contributors = []
    for entry in health['contributors']:
        contributors.append({
            **entry,
            'detail': detail_map.get(entry['factor'], ''),
        })

    risk = rl.compute_asset_risk({**evidence, 'health_band': health['band']})
    return {
        'asset_id': asset_id,
        'asset_no': asset['asset_no'],
        'name': asset['name'],
        'site_name': asset.get('site_name'),
        'location_name': asset.get('location_name'),
        'health': {'score': health['score'], 'state': health['band']},
        'risk': risk,
        'contributors': contributors,
        'channel_findings': channel_findings,
        'evidence': {k: v for k, v in evidence.items() if k != 'open_work'},
        'formula': 'docs/APM_ANALYTICS.md#asset-health-score',
    }


def portfolio_risk_view(conn, site_id=None) -> dict:
    sql = (
        "SELECT a.id,a.asset_no,a.name,a.criticality,s.name site_name "
        "FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id "
        "WHERE 1=1"
    )
    args: list = []
    if site_id is not None:
        sql += ' AND s.id=?'
        args.append(site_id)
    sql += ' ORDER BY a.asset_no'

    matrix: dict[str, dict[str, int]] = {}
    entries = []
    for asset in _rows(conn.execute(sql, args)):
        trend_level, _f = _asset_trend_level(conn, asset['id'])
        evidence = gather_health_evidence(conn, asset, trend_level=trend_level)
        health = rl.compute_asset_health(evidence)
        risk = rl.compute_asset_risk({**evidence, 'health_band': health['band']})
        matrix.setdefault(str(risk['consequence']), {})
        matrix[str(risk['consequence'])][str(risk['likelihood'])] = (
            matrix[str(risk['consequence'])].get(str(risk['likelihood']), 0) + 1
        )
        entries.append({
            'asset_id': asset['id'],
            'asset_no': asset['asset_no'],
            'name': asset['name'],
            'site_name': asset.get('site_name'),
            'health_score': health['score'],
            'health_state': health['band'],
            'likelihood': risk['likelihood'],
            'consequence': risk['consequence'],
            'risk_score': risk['risk_score'],
            'risk_level': risk['risk_level'],
        })
    entries.sort(key=lambda e: (-e['risk_score'], -e['health_score'], e['asset_no']))
    counts: dict[str, int] = {'Extreme': 0, 'High': 0, 'Medium': 0, 'Low': 0}
    for e in entries:
        counts[e['risk_level']] += 1
    return {'matrix': matrix, 'counts': counts, 'assets': entries[:100], 'total': len(entries)}


# ---------------------------------------------------------------------------
# Deterioration watchlist
# ---------------------------------------------------------------------------
def deterioration_watchlist_view(
    conn, *, window_days: int = 30, min_points: int = 4, site_id: Optional[int] = None
) -> list[dict]:
    cutoff = _iso(datetime.now() - timedelta(days=window_days))
    watchlist = []
    channels_sql = '''
       SELECT tc.*,a.asset_no,a.name asset_name,s.name site_name
       FROM telemetry_channels tc
       JOIN assets a ON a.id=tc.asset_id
       LEFT JOIN locations l ON l.id=a.location_id
       LEFT JOIN sites s ON s.id=l.site_id
       WHERE tc.active=1'''
    channel_args: list = []
    if site_id is not None:
        channels_sql += ' AND s.id=?'
        channel_args.append(site_id)
    channels_sql += ' ORDER BY a.asset_no,tc.channel_code'
    channels = _rows(conn.execute(channels_sql, channel_args))
    for channel in channels:
        values = [
            float(r['value']) for r in _rows(conn.execute(
                '''SELECT value FROM telemetry_readings
                   WHERE channel_id=? AND captured_at>=?
                   ORDER BY captured_at ASC, id ASC''',
                (channel['id'], cutoff),
            ))
        ]
        verdict = rl.evaluate_channel_condition(values, {
            'warning_low': channel.get('warning_low'),
            'critical_low': channel.get('critical_low'),
            'warning_high': channel.get('warning_high'),
            'critical_high': channel.get('critical_high'),
        }, min_points=min_points)
        if verdict['level'] == 'none':
            continue
        watchlist.append({
            'asset_id': channel['asset_id'],
            'asset_no': channel['asset_no'],
            'asset_name': channel['asset_name'],
            'site_name': channel.get('site_name'),
            'channel_id': channel['id'],
            'channel_code': channel['channel_code'],
            'channel_name': channel['name'],
            'unit': channel['unit'],
            'level': verdict['level'],
            'signals': verdict['signals'],
            'trend_state': verdict['trend']['state'],
            'readings': verdict['excursions']['readings'],
        })
    order = {'severe': 0, 'adverse': 1}
    watchlist.sort(key=lambda item: (order[item['level']], item['asset_no'], item['channel_code']))
    return watchlist


# ---------------------------------------------------------------------------
# Alarm correlation
# ---------------------------------------------------------------------------
def _correlation_id(kind: str, *parts) -> str:
    """Stable content-derived identifier for one correlation cluster.

    The id is a digest of the cluster's defining evidence (kind, asset,
    channel and window bounds), so identical data always yields the same id
    while any change to the underlying evidence yields a new one.
    """
    digest = hashlib.sha256(
        '|'.join([kind, *(str(p) for p in parts)]).encode('utf-8')
    ).hexdigest()[:16]
    return f'COR-{digest}'


def _cluster_attribution(alarms: list[dict]) -> dict:
    ordered = sorted(alarms, key=lambda a: (str(a['opened_at']), int(a['id'])))
    return {
        'primary_alarm_id': int(ordered[0]['id']),
        'related_alarm_ids': [int(a['id']) for a in ordered[1:]],
        'alarm_nos': [a['alarm_no'] for a in ordered],
    }


def _sliding_time_clusters(
    timed_pairs: list[tuple[datetime, dict]], window_seconds: int
):
    """Yield ``(i, j)`` index windows whose span fits ``window_seconds``.

    ``timed_pairs`` must be sorted ascending by timestamp; each yielded window
    is maximal, so consecutive windows never share an alarm.
    """
    times = [t for t, _alarm in timed_pairs]
    i = 0
    while i < len(times):
        j = i
        while (
            j + 1 < len(times)
            and (times[j + 1] - times[i]).total_seconds() <= window_seconds
        ):
            j += 1
        yield i, j
        i = j + 1


def alarm_correlation_view(
    conn,
    *,
    hours: int = 24,
    burst_window_minutes: int = 15,
    burst_threshold: int = 5,
    site_id: Optional[int] = None,
    site_burst_threshold: int = 8,
) -> dict:
    cutoff = _iso(datetime.now() - timedelta(hours=hours))
    alarm_sql = '''
        SELECT oa.*,tc.channel_code,tc.name channel_name,a.asset_no,a.name asset_name,
                  s.site_code,s.name site_name
           FROM operational_alarms oa
           JOIN telemetry_channels tc ON tc.id=oa.channel_id
           JOIN assets a ON a.id=oa.asset_id
           LEFT JOIN sites s ON s.id=oa.site_id
           WHERE (oa.opened_at>=? OR oa.last_seen_at>=?)'''
    alarm_args: list = [cutoff, cutoff]
    if site_id is not None:
        alarm_sql += ' AND oa.site_id=?'
        alarm_args.append(site_id)
    alarm_sql += ' ORDER BY oa.opened_at ASC'
    alarms = _rows(conn.execute(alarm_sql, alarm_args))

    by_channel: dict[int, list[dict]] = {}
    by_asset: dict[int, list[dict]] = {}
    for alarm in alarms:
        by_channel.setdefault(alarm['channel_id'], []).append(alarm)
        by_asset.setdefault(alarm['asset_id'], []).append(alarm)

    recurrence = []
    for channel_id, group in by_channel.items():
        if len(group) < 2:
            continue
        sample = group[-1]
        first_opened = min(str(a['opened_at']) for a in group)
        last_seen = max(str(a['last_seen_at']) for a in group)
        recurrence.append({
            'correlation_id': _correlation_id(
                'recurrence', channel_id, sample['asset_id'], first_opened, last_seen
            ),
            **_cluster_attribution(group),
            'asset_id': sample['asset_id'],
            'asset_no': sample['asset_no'],
            'channel_id': channel_id,
            'channel_code': sample['channel_code'],
            'message_pattern': sample['message'].split(':')[0][:80],
            'occurrences': len(group),
            'total_occurrence_count': sum(int(a['occurrence_count'] or 1) for a in group),
            'first_opened_at': group[0]['opened_at'],
            'last_seen_at': max(a['last_seen_at'] for a in group),
            'rationale': (
                f"{len(group)} alarm event(s) on the same asset/channel pair "
                f"in the last {hours}h"
            ),
        })
    recurrence.sort(key=lambda item: (-item['occurrences'], item['asset_no']))

    bursts = []
    window_seconds = burst_window_minutes * 60
    for asset_id, group in by_asset.items():
        timed = sorted(
            (
                (parsed, alarm)
                for parsed, alarm in (
                    (_parse_ts(entry['opened_at']), entry) for entry in group
                )
                if parsed
            ),
            key=lambda pair: (pair[0], int(pair[1]['id'])),
        )
        for i, j in _sliding_time_clusters(timed, window_seconds):
            count = j - i + 1
            if count >= burst_threshold:
                window_alarms = [a for _t, a in timed[i:j + 1]]
                sample = group[0]
                channels = sorted({a['channel_code'] for a in window_alarms})
                bursts.append({
                    'correlation_id': _correlation_id(
                        'burst', asset_id, sample['site_id'], _iso(timed[i][0]), _iso(timed[j][0])
                    ),
                    **_cluster_attribution(window_alarms),
                    'asset_id': asset_id,
                    'asset_no': sample['asset_no'],
                    'site_name': sample.get('site_name'),
                    'alarms': count,
                    'started_at': _iso(timed[i][0]),
                    'ended_at': _iso(timed[j][0]),
                    'channels': channels,
                    'probable_common_source': sample['asset_name'],
                    'rationale': (
                        f"{count} alarms on asset {sample['asset_no']} within "
                        f"{burst_window_minutes} minutes across {len(channels)} channel(s); "
                        f"probable common source is the shared asset/process"
                    ),
                })
    bursts.sort(key=lambda item: (-item['alarms'], item['asset_no']))

    groups = []
    for asset_id, group in by_asset.items():
        if len(group) < 3:
            continue
        sample = group[0]
        first_opened = min(str(a['opened_at']) for a in group)
        last_seen = max(str(a['last_seen_at']) for a in group)
        groups.append({
            'correlation_id': _correlation_id(
                'group', asset_id, sample['site_id'], first_opened, last_seen
            ),
            **_cluster_attribution(group),
            'asset_id': asset_id,
            'asset_no': sample['asset_no'],
            'asset_name': sample['asset_name'],
            'site_name': sample.get('site_name'),
            'alarm_count': len(group),
            'distinct_channels': len({a['channel_id'] for a in group}),
            'first_opened_at': group[0]['opened_at'],
            'last_seen_at': max(a['last_seen_at'] for a in group),
            'rationale': (
                f"{len(group)} correlated alarms share one asset/source; raw alarms preserved"
            ),
        })
    groups.sort(key=lambda item: (-item['alarm_count'], item['asset_no']))

    # Site-level bursts: many alarms across DIFFERENT assets at one site in a
    # tight window indicate a probable shared upstream condition (power feed,
    # process header, comms). Single-asset clusters are already reported as
    # asset bursts, so a site burst requires at least two distinct assets.
    by_site: dict[int, list[dict]] = {}
    for alarm in alarms:
        if alarm['site_id'] is not None:
            by_site.setdefault(int(alarm['site_id']), []).append(alarm)

    site_bursts = []
    for cluster_site_id, group in by_site.items():
        timed = sorted(
            (
                (parsed, alarm)
                for parsed, alarm in (
                    (_parse_ts(entry['opened_at']), entry) for entry in group
                )
                if parsed
            ),
            key=lambda pair: (pair[0], int(pair[1]['id'])),
        )
        sample = group[0]
        site_label = sample.get('site_name') or f"site {cluster_site_id}"
        for i, j in _sliding_time_clusters(timed, window_seconds):
            window_alarms = [a for _t, a in timed[i:j + 1]]
            distinct_assets = {int(a['asset_id']) for a in window_alarms}
            count = len(window_alarms)
            if count < site_burst_threshold or len(distinct_assets) < 2:
                continue
            channels = sorted({a['channel_code'] for a in window_alarms})
            assets = sorted({a['asset_no'] for a in window_alarms})
            site_bursts.append({
                'correlation_id': _correlation_id(
                    'site_burst', cluster_site_id, _iso(timed[i][0]), _iso(timed[j][0])
                ),
                **_cluster_attribution(window_alarms),
                'site_id': cluster_site_id,
                'site_code': sample.get('site_code'),
                'site_name': sample.get('site_name'),
                'alarms': count,
                'distinct_assets': len(distinct_assets),
                'assets': assets,
                'started_at': _iso(timed[i][0]),
                'ended_at': _iso(timed[j][0]),
                'channels': channels,
                'rationale': (
                    f"{count} alarms across {len(distinct_assets)} asset(s) at "
                    f"{site_label} within {burst_window_minutes} minutes; "
                    f"probable common upstream condition at the site"
                ),
            })
    site_bursts.sort(key=lambda item: (-item['alarms'], item.get('site_code') or ''))

    return {
        'window_hours': hours,
        'total_alarms': len(alarms),
        'recurrence': recurrence[:50],
        'bursts': bursts[:50],
        'groups': groups[:50],
        'site_bursts': site_bursts[:50],
        'burst_settings': {
            'window_minutes': burst_window_minutes,
            'threshold': burst_threshold,
            'site_threshold': site_burst_threshold,
        },
    }


# ---------------------------------------------------------------------------
# Condition-based maintenance recommendations
# ---------------------------------------------------------------------------
def run_cbm_evaluation(conn, actor: Optional[dict], *, window_days: int = 30) -> dict:
    created: list[dict] = []
    evaluated_channels = 0
    for finding in deterioration_watchlist_view(conn, window_days=window_days):
        evaluated_channels += 1
        if finding['level'] == 'adverse' and not any(
            s.startswith(('warning excursion', 'repeated abnormal', 'latest reading'))
            for s in finding['signals']
        ):
            # A bare stable-direction trend without abnormal evidence stays on
            # the watchlist but does not generate maintenance workload yet.
            continue
        condition_type = (
            'persistent_abnormal'
            if any(s.startswith('abnormal state persisted') for s in finding['signals'])
            else 'critical_excursion'
            if any('critical threshold' in s for s in finding['signals'])
            else 'trend_deterioration'
        )
        severity = 'Critical' if finding['level'] == 'severe' else 'High'
        open_row = conn.execute(
            '''SELECT id FROM cbm_recommendations
               WHERE asset_id=? AND COALESCE(channel_id,-1)=COALESCE(?,-1)
               AND condition_type=? AND status IN ('Open','Reviewed','Approved')''',
            (finding['asset_id'], finding['channel_id'], condition_type),
        ).fetchone()
        if open_row:
            continue
        no = _application.next_no(conn, 'cbm_recommendations', 'recommendation_no', 'CBM-', 30001)
        evidence = {
            'signals': finding['signals'],
            'level': finding['level'],
            'readings': finding['readings'],
            'window_days': window_days,
        }
        suggested = (
            'Perform condition-based inspection and verify against acceptable limits'
        )
        cur = conn.execute(
            '''INSERT INTO cbm_recommendations(
                 recommendation_no,asset_id,channel_id,condition_type,severity,
                 evidence_json,suggested_action,confidence,status,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (
                no,
                finding['asset_id'],
                finding['channel_id'],
                condition_type,
                severity,
                json.dumps(evidence, sort_keys=True),
                suggested,
                'deterministic',
                'Open',
                actor['id'] if actor else None,
                now(),
            ),
        )
        del cur
        created.append({'recommendation_no': no, **finding})

    if created and actor:
        _application.audit(
            conn,
            actor['id'],
            'GENERATE CBM',
            'Reliability',
            ','.join(c['recommendation_no'] for c in created)[:120],
            '',
            {'created': len(created)},
        )
    return {'evaluated_channels': evaluated_channels, 'created': created}


def _cbm_or_404(conn, recommendation_id: int) -> dict:
    row = conn.execute(
        'SELECT r.*,a.asset_no FROM cbm_recommendations r '
        'JOIN assets a ON a.id=r.asset_id WHERE r.id=?',
        (recommendation_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, 'Recommendation not found')
    return dict(row)


def decide_cbm_recommendation(
    conn,
    recommendation_id: int,
    action: str,
    user: dict,
    suggested_action: Optional[str] = None,
) -> dict:
    rec = _cbm_or_404(conn, recommendation_id)
    transitions = {
        'review': ({'Open'}, 'Reviewed'),
        'approve': ({'Open', 'Reviewed'}, 'Approved'),
        'dismiss': ({'Open', 'Reviewed'}, 'Dismissed'),
    }
    if action not in transitions:
        raise HTTPException(400, 'Action must be review, approve or dismiss')
    allowed_from, target = transitions[action]
    if rec['status'] not in allowed_from:
        raise HTTPException(
            409, f"Recommendation is {rec['status']}; cannot move to {target}"
        )
    updates = 'status=?,decided_at=?,decided_by=?'
    args: list = [target, now(), user['id']]
    if action == 'review' and suggested_action:
        updates += ',suggested_action=?'
        args.append(suggested_action)
    args.append(recommendation_id)
    conn.execute(f'UPDATE cbm_recommendations SET {updates} WHERE id=?', args)
    _application.audit(
        conn,
        user['id'],
        f'CBM {action.upper()}',
        'Reliability',
        rec['recommendation_no'],
        rec['status'],
        target,
    )
    return {'ok': True, 'status': target, 'recommendation_no': rec['recommendation_no']}


def convert_cbm_to_work_order(conn, recommendation_id: int, user: dict) -> dict:
    rec = _cbm_or_404(conn, recommendation_id)
    if rec['status'] != 'Approved':
        raise HTTPException(409, 'Only approved recommendations can become work orders')
    if rec['work_order_id']:
        wo = conn.execute('SELECT wo_no FROM work_orders WHERE id=?', (rec['work_order_id'],)).fetchone()
        if wo:
            return {'ok': True, 'work_order_id': rec['work_order_id'], 'wo_no': wo['wo_no'], 'existing': True}
    no = _application.next_no(conn, 'work_orders', 'wo_no', 'WO-', 10026)
    evidence = json.loads(rec['evidence_json'] or '{}')
    signals = '; '.join(evidence.get('signals', []))[:400]
    title = f"CBM {rec['recommendation_no']}: {rec['condition_type']} on {rec['asset_no']}"
    description = f"Deterministic condition evidence: {signals}"
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,description,asset_id,priority,status,work_type,failure_code,
             requested_by,target_start,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            no,
            title,
            description,
            rec['asset_id'],
            _SEVERITY_PRIORITY.get(rec['severity'], 'Medium'),
            'Submitted',
            'Corrective',
            '',
            user['id'],
            _iso(datetime.now()),
            now(),
            now(),
        ),
    )
    conn.execute(
        'UPDATE cbm_recommendations SET work_order_id=?,decided_at=?,decided_by=? WHERE id=?',
        (cur.lastrowid, now(), user['id'], recommendation_id),
    )
    _application.audit(
        conn,
        user['id'],
        'CONVERT CBM TO WO',
        'Reliability',
        rec['recommendation_no'],
        '',
        {'work_order': no},
    )
    _application.emit_event(
        conn,
        'reliability.cbm.converted',
        'cbm_recommendation',
        rec['recommendation_no'],
        {'work_order': no, 'asset_id': rec['asset_id']},
    )
    _application._ensure_work_sla(conn, cur.lastrowid)
    return {'ok': True, 'work_order_id': cur.lastrowid, 'wo_no': no, 'existing': False}


# ---------------------------------------------------------------------------
# Bad actors
# ---------------------------------------------------------------------------
def bad_actors_view(conn, *, window_days: int = 365, limit: int = 20, site_id: Optional[int] = None) -> list[dict]:
    window_start = _iso(datetime.now() - timedelta(days=window_days))
    metrics_by_asset: dict[str, dict] = {}
    drilldown: dict[str, dict] = {}

    asset_sql = (
        "SELECT a.id,a.asset_no,a.name,a.criticality,s.name site_name "
        "FROM assets a LEFT JOIN locations l ON l.id=a.location_id "
        "LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1"
    )
    asset_args: list = []
    if site_id is not None:
        asset_sql += ' AND s.id=?'
        asset_args.append(site_id)
    asset_sql += ' ORDER BY a.asset_no'
    asset_rows = _rows(conn.execute(asset_sql, asset_args))
    operating_hours = max(float(window_days) * 24.0, 1.0)

    for asset in asset_rows:
        completions = _rows(conn.execute(
            "SELECT id,wo_no,title,status,priority,work_type,failure_code,actual_finish,actual_cost "
            "FROM work_orders WHERE asset_id=? AND work_type IN (?,?) "
            "AND status IN ('Completed','Closed') AND COALESCE(actual_finish,updated_at)>=?",
            (asset['id'], *_FAILURE_WORK_TYPES, window_start),
        ))
        emergency = sum(1 for w in completions if w['priority'] in ('Emergency', 'Critical'))
        downtime = 0.0
        for outage in _rows(conn.execute(
            "SELECT start_at,end_at,status FROM asset_outages "
            "WHERE asset_id=? AND outage_type='Forced' AND start_at>=?",
            (asset['id'], window_start),
        )):
            started = _parse_ts(outage['start_at'])
            ended = _parse_ts(outage['end_at'])
            if not started:
                continue
            if outage['status'] == 'Open' or ended is None:
                ended = datetime.now()
            downtime += max(0.0, (ended - started).total_seconds() / 3600.0)
        alarm_count = int(conn.execute(
            "SELECT COUNT(*) FROM operational_alarms WHERE asset_id=? AND opened_at>=?",
            (asset['id'], window_start),
        ).fetchone()[0])
        cost = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM maintenance_cost_ledger "
            "WHERE asset_id=? AND posted_at>=?",
            (asset['id'], window_start),
        ).fetchone()[0])

        m = rl.bad_actor_metrics(
            corrective_completions=len(completions),
            emergency_count=emergency,
            downtime_hours=downtime,
            alarm_count=alarm_count,
            maintenance_cost=cost,
            operating_hours=operating_hours,
        )
        metrics_by_asset[asset['asset_no']] = m
        drilldown[asset['asset_no']] = {
            'asset_id': asset['id'],
            'name': asset['name'],
            'criticality': asset['criticality'],
            'failures': [dict(w) for w in completions],
            'failure_modes': _mode_counts(completions),
        }

    ranked = rl.rank_bad_actors(metrics_by_asset)
    result = []
    for entry in ranked[:limit]:
        detail = drilldown[entry['asset_no']]
        result.append({**entry, **detail})
    return result


def _mode_counts(completions: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for w in completions:
        code = (w.get('failure_code') or '').strip() or 'Unclassified'
        counts[code] = counts.get(code, 0) + 1
    return sorted(
        ({'failure_mode': k, 'count': v} for k, v in counts.items()),
        key=lambda item: (-item['count'], item['failure_mode']),
    )


# ---------------------------------------------------------------------------
# Post-maintenance effectiveness
# ---------------------------------------------------------------------------
_EFFECTIVENESS_WINDOW_DAYS = 30


def _window_metrics(
    conn,
    asset_id: int,
    started: datetime,
    ended: datetime,
) -> dict:
    alarms = _rows(conn.execute(
        "SELECT channel_id,severity FROM operational_alarms "
        "WHERE asset_id=? AND opened_at>? AND opened_at<=?",
        (asset_id, _iso(started), _iso(ended)),
    ))
    excursions = 0
    for reading in _rows(conn.execute(
        '''SELECT tr.value,tc.warning_low,tc.critical_low,tc.warning_high,tc.critical_high
           FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id
           WHERE tc.asset_id=? AND tr.captured_at>? AND tr.captured_at<=?''',
        (asset_id, _iso(started), _iso(ended)),
    )):
        severity, _bound = rl.threshold_level(float(reading['value']), {
            'warning_low': reading['warning_low'],
            'critical_low': reading['critical_low'],
            'warning_high': reading['warning_high'],
            'critical_high': reading['critical_high'],
        })
        if severity:
            excursions += 1
    failures = int(conn.execute(
        "SELECT COUNT(*) FROM work_orders WHERE asset_id=? AND work_type IN (?,?) "
        "AND status IN ('Completed','Closed') "
        "AND COALESCE(actual_finish,updated_at)>? AND COALESCE(actual_finish,updated_at)<=?",
        (asset_id, *_FAILURE_WORK_TYPES, _iso(started), _iso(ended)),
    ).fetchone()[0])
    downtime = 0.0
    for outage in _rows(conn.execute(
        "SELECT start_at,end_at,status FROM asset_outages WHERE asset_id=? AND outage_type='Forced'",
        (asset_id,),
    )):
        o_start = _parse_ts(outage['start_at'])
        o_end = _parse_ts(outage['end_at']) if outage['status'] != 'Open' else None
        if not o_start:
            continue
        o_end = o_end or datetime.now()
        latest_start = max(o_start, started)
        earliest_end = min(o_end, ended)
        if earliest_end > latest_start:
            downtime += (earliest_end - latest_start).total_seconds() / 3600.0

    snapshots = _rows(conn.execute(
        "SELECT score FROM asset_health_snapshots WHERE asset_id=? AND calculated_at>? AND calculated_at<=?",
        (asset_id, _iso(started), _iso(ended)),
    ))
    avg_health = round(sum(float(s['score']) for s in snapshots) / len(snapshots), 1) if snapshots else None

    return {
        'alarms': len(alarms),
        'critical_alarms': sum(1 for a in alarms if a['severity'] == 'Critical'),
        'alarm_channels': sorted({int(a['channel_id']) for a in alarms}),
        'failures': failures,
        'downtime_hours': round(downtime, 1),
        'excursions': excursions,
        'avg_health': avg_health,
    }


def work_order_effectiveness(conn, wo_id: int, *, window_days: int = _EFFECTIVENESS_WINDOW_DAYS) -> dict:
    wo = _application.get_or_404(
        conn, 'SELECT * FROM work_orders WHERE id=?', (wo_id,), 'Work order not found'
    )
    finish = _parse_ts(wo.get('actual_finish'))
    if not finish or wo['status'] not in ('Completed', 'Closed'):
        raise HTTPException(
            409,
            'Effectiveness is measured after the work order reaches Completed/Closed',
        )
    span = timedelta(days=window_days)
    pre = _window_metrics(conn, wo['asset_id'], finish - span, finish)
    post = _window_metrics(conn, wo['asset_id'], finish, finish + span)

    recurring = []
    pre_channels = set(pre.pop('alarm_channels'))
    post_channels = set(post.pop('alarm_channels'))
    for channel_id in sorted(pre_channels & post_channels):
        row = conn.execute(
            'SELECT channel_code,name FROM telemetry_channels WHERE id=?', (channel_id,)
        ).fetchone()
        if row:
            recurring.append({
                'channel_id': channel_id,
                'channel_code': row['channel_code'],
                'channel_name': row['name'],
                'rationale': 'same channel alarmed both before and after the repair',
            })
    post['recurring_issues'] = recurring
    verdict = rl.maintenance_effectiveness(pre, post)
    return {
        'work_order_id': wo_id,
        'wo_no': wo['wo_no'],
        'asset_id': wo['asset_id'],
        'actual_finish': wo['actual_finish'],
        'window_days': window_days,
        'pre': pre,
        'post': post,
        **verdict,
    }


def maintenance_effectiveness_list(
    conn, *, limit: int = 25, window_days: int = _EFFECTIVENESS_WINDOW_DAYS,
    site_id: Optional[int] = None,
) -> list[dict]:
    completed_sql = (
        "SELECT w.id,w.wo_no,w.asset_id,w.actual_finish,w.work_type "
        "FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id "
        "LEFT JOIN locations l ON l.id=a.location_id "
        "WHERE w.status IN ('Completed','Closed') AND w.actual_finish IS NOT NULL"
    )
    completed_args: list = []
    if site_id is not None:
        completed_sql += ' AND l.site_id=?'
        completed_args.append(site_id)
    completed_sql += ' ORDER BY w.actual_finish DESC LIMIT ?'
    completed_args.append(limit)
    completed = _rows(conn.execute(completed_sql, completed_args))
    results = []
    for wo in completed:
        try:
            results.append(work_order_effectiveness(conn, wo['id'], window_days=window_days))
        except HTTPException:
            continue
    return results


# ---------------------------------------------------------------------------
# FMEA catalog
# ---------------------------------------------------------------------------
def fmea_list_view(
    conn, *, asset_id: Optional[int] = None, status: str = '',
    limit: int = 100, offset: int = 0,
) -> dict:
    where = ' WHERE 1=1'
    args: list = []
    if asset_id is not None:
        where += ' AND f.asset_id=?'
        args.append(asset_id)
    if status:
        where += ' AND f.status=?'
        args.append(status)
    total = int(conn.execute(
        'SELECT COUNT(*) FROM fmea_records f' + where, args
    ).fetchone()[0])
    records = _rows(conn.execute(
        '''SELECT f.*,a.asset_no,a.name asset_name,u.full_name created_by_name
           FROM fmea_records f
           LEFT JOIN assets a ON a.id=f.asset_id
           LEFT JOIN users u ON u.id=f.created_by''' + where +
        ' ORDER BY f.id DESC LIMIT ? OFFSET ?',
        args + [limit, offset],
    ))
    return {'total': total, 'limit': limit, 'offset': offset, 'records': records}


# ---------------------------------------------------------------------------
# FMEA / RCM evidence
# ---------------------------------------------------------------------------
_FMEA_STATUSES = ('Draft', 'Approved')


def create_fmea_record(conn, body, user: dict) -> dict:
    rpn = int(body.severity) * int(body.occurrence) * int(body.detection)
    no = _application.next_no(conn, 'fmea_records', 'fmea_no', 'FMEA-', 70001)
    cur = conn.execute(
        '''INSERT INTO fmea_records(
             fmea_no,asset_id,function_text,failure_mode,failure_cause,failure_effect,
             severity,occurrence,detection,rpn,status,created_by,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,'Draft',?,?,?)''',
        (
            no,
            body.asset_id,
            body.function_text,
            body.failure_mode,
            body.failure_cause or '',
            body.failure_effect or '',
            int(body.severity),
            int(body.occurrence),
            int(body.detection),
            rpn,
            user['id'],
            now(),
            now(),
        ),
    )
    _application.audit(conn, user['id'], 'CREATE FMEA', 'Reliability', no, '', body.model_dump())
    return {'id': cur.lastrowid, 'fmea_no': no, 'rpn': rpn, 'status': 'Draft'}


def update_fmea_record(conn, fmea_id: int, body, user: dict) -> dict:
    record = _application.get_or_404(
        conn, 'SELECT * FROM fmea_records WHERE id=?', (fmea_id,), 'FMEA record not found'
    )
    if record['status'] == 'Approved':
        raise HTTPException(
            409,
            'Approved FMEA analysis is immutable; create a revised draft instead',
        )
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = {**dict(record), **changes}
    rpn = int(merged['severity']) * int(merged['occurrence']) * int(merged['detection'])
    sets = list(changes.keys()) + ['rpn', 'updated_at']
    values = list(changes.values()) + [rpn, now(), fmea_id]
    conn.execute(
        f'UPDATE fmea_records SET {",".join(f"{c}=?" for c in sets)} WHERE id=?',
        values,
    )
    _application.audit(
        conn, user['id'], 'UPDATE FMEA', 'Reliability', record['fmea_no'], record, changes
    )
    return {'ok': True, 'rpn': rpn}


def approve_fmea_record(conn, fmea_id: int, user: dict) -> dict:
    record = _application.get_or_404(
        conn, 'SELECT * FROM fmea_records WHERE id=?', (fmea_id,), 'FMEA record not found'
    )
    if record['status'] == 'Approved':
        raise HTTPException(409, 'FMEA record is already approved')
    conn.execute(
        'UPDATE fmea_records SET status=?,approved_by=?,approved_at=?,updated_at=? WHERE id=?',
        ('Approved', user['id'], now(), now(), fmea_id),
    )
    _application.audit(
        conn, user['id'], 'APPROVE FMEA', 'Reliability', record['fmea_no'], 'Draft', 'Approved'
    )
    return {'ok': True, 'status': 'Approved'}


def fmea_observed_evidence(conn, fmea_id: int, *, window_days: int = 365) -> dict:
    record = _application.get_or_404(
        conn, 'SELECT * FROM fmea_records WHERE id=?', (fmea_id,), 'FMEA record not found'
    )
    if not record['asset_id']:
        raise HTTPException(409, 'FMEA record has no linked asset to observe')
    window_start = _iso(datetime.now() - timedelta(days=window_days))
    completions = _rows(conn.execute(
        "SELECT wo_no,title,actual_finish,failure_code,actual_cost,completion_notes "
        "FROM work_orders WHERE asset_id=? AND work_type IN (?,?) "
        "AND status IN ('Completed','Closed') AND COALESCE(actual_finish,updated_at)>=?",
        (record['asset_id'], *_FAILURE_WORK_TYPES, window_start),
    ))
    matches = [
        w for w in completions
        if (w['failure_code'] or '').strip().casefold()
        == str(record['failure_mode']).strip().casefold()
    ]
    expected_occurrence = int(record['occurrence'])
    observed_scale = min(5, max(1, len(matches)))
    gaps = []
    finishes = sorted(_parse_ts(w['actual_finish']) for w in matches if w['actual_finish'])
    for earlier, later in zip(finishes, finishes[1:]):
        gaps.append(round((later - earlier).total_seconds() / 86400.0, 1))
    alignment = 'consistent' if abs(observed_scale - expected_occurrence) <= 1 else 'divergent'
    return {
        'fmea_no': record['fmea_no'],
        'failure_mode': record['failure_mode'],
        'expected_occurrence': expected_occurrence,
        'observed_occurrences': len(matches),
        'observed_occurrence_scale': observed_scale,
        'alignment': alignment,
        'mean_interval_days': round(sum(gaps) / len(gaps), 1) if gaps else None,
        'matching_completions': matches,
        'note': (
            'Observed evidence maps completed corrective/emergency work orders to this '
            'failure mode by exact failure_code match only; unmapped codes are never inferred.'
        ),
    }


def install_apm_routes() -> None:
    app = _application.app
    marker = '_euas_apm_routes'
    if getattr(app.state, marker, False):
        return

    @app.get('/api/reliability/health/{asset_id}')
    def reliability_health_route(asset_id: int, user=Depends(current_user)):
        with db() as conn:
            return explain_asset_health_view(conn, asset_id)

    @app.get('/api/reliability/risk-matrix')
    def reliability_risk_matrix_route(site_id: Optional[int] = None, user=Depends(current_user)):
        with db() as conn:
            return portfolio_risk_view(conn, site_id=site_id)

    @app.get('/api/reliability/deterioration-watchlist')
    def deterioration_watchlist_route(
        window_days: int = _application.Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return deterioration_watchlist_view(
                conn, window_days=window_days, site_id=site_id
            )

    @app.get('/api/reliability/alarm-correlation')
    def alarm_correlation_route(
        hours: int = _application.Query(24, ge=1, le=720),
        burst_window_minutes: int = _application.Query(15, ge=1, le=240),
        burst_threshold: int = _application.Query(5, ge=2, le=100),
        site_id: Optional[int] = None,
        site_burst_threshold: int = _application.Query(8, ge=2, le=500),
        user=Depends(current_user),
    ):
        with db() as conn:
            return alarm_correlation_view(
                conn,
                hours=hours,
                burst_window_minutes=burst_window_minutes,
                burst_threshold=burst_threshold,
                site_id=site_id,
                site_burst_threshold=site_burst_threshold,
            )

    @app.post('/api/reliability/cbm-evaluation')
    def cbm_evaluation_route(
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES, 'planner')),
    ):
        with db() as conn:
            return run_cbm_evaluation(conn, user)

    @app.get('/api/reliability/cbm-recommendations')
    def cbm_list_route(
        status: str = '',
        asset_id: Optional[int] = None,
        site_id: Optional[int] = None,
        limit: int = _application.Query(200, ge=1, le=1000),
        offset: int = _application.Query(0, ge=0),
        user=Depends(current_user),
    ):
        with db() as conn:
            sql = (
                'SELECT r.*,a.asset_no,a.name asset_name,tc.channel_code '
                'FROM cbm_recommendations r JOIN assets a ON a.id=r.asset_id '
                'LEFT JOIN telemetry_channels tc ON tc.id=r.channel_id '
                'LEFT JOIN locations l ON l.id=a.location_id WHERE 1=1'
            )
            args: list = []
            if status:
                sql += ' AND r.status=?'
                args.append(status)
            if asset_id is not None:
                sql += ' AND r.asset_id=?'
                args.append(asset_id)
            if site_id is not None:
                sql += ' AND l.site_id=?'
                args.append(site_id)
            sql += (
                ' ORDER BY CASE r.status WHEN \'Open\' THEN 0 WHEN \'Reviewed\' THEN 1'
                ' ELSE 2 END, r.id DESC LIMIT ? OFFSET ?'
            )
            args.extend([limit, offset])
            return _rows(conn.execute(sql, args))

    @app.post('/api/reliability/cbm-recommendations/{recommendation_id}/convert-to-work-order')
    def cbm_convert_route(
        recommendation_id: int,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES, 'planner')),
    ):
        with db() as conn:
            return convert_cbm_to_work_order(conn, recommendation_id, user)

    # Registered AFTER the literal convert-to-work-order path so FastAPI never
    # captures it as an {action} decision (whose role list excludes planners).
    @app.post('/api/reliability/cbm-recommendations/{recommendation_id}/{action}')
    def cbm_decide_route(
        recommendation_id: int,
        action: str,
        body: _application.CBMDecisionIn | None = None,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES)),
    ):
        with db() as conn:
            return decide_cbm_recommendation(
                conn,
                recommendation_id,
                action,
                user,
                suggested_action=body.suggested_action if body else None,
            )

    @app.get('/api/reliability/bad-actors')
    def bad_actors_route(
        window_days: int = _application.Query(365, ge=30, le=1095),
        limit: int = _application.Query(20, ge=1, le=100),
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return bad_actors_view(conn, window_days=window_days, limit=limit, site_id=site_id)

    @app.get('/api/work-orders/{wo_id}/effectiveness')
    def wo_effectiveness_route(
        wo_id: int,
        window_days: int = _application.Query(30, ge=7, le=180),
        user=Depends(current_user),
    ):
        with db() as conn:
            return work_order_effectiveness(conn, wo_id, window_days=window_days)

    @app.get('/api/reliability/maintenance-effectiveness')
    def effectiveness_list_route(
        window_days: int = _application.Query(_EFFECTIVENESS_WINDOW_DAYS, ge=7, le=180),
        limit: int = _application.Query(25, ge=1, le=200),
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            return maintenance_effectiveness_list(
                conn, window_days=window_days, limit=limit, site_id=site_id
            )

    @app.get('/api/reliability/fmea')
    def fmea_list_route(
        asset_id: Optional[int] = None,
        status: str = '',
        limit: int = _application.Query(100, ge=1, le=500),
        offset: int = _application.Query(0, ge=0),
        user=Depends(current_user),
    ):
        with db() as conn:
            return fmea_list_view(
                conn, asset_id=asset_id, status=status, limit=limit, offset=offset
            )

    # ------------------------------------------------------------------
    # CSV exports for the APM intelligence views
    # ------------------------------------------------------------------
    @app.get('/api/exports/reliability/bad-actors.csv')
    def export_bad_actors_csv(
        window_days: int = _application.Query(365, ge=30, le=1095),
        site_id: Optional[int] = None,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES, 'planner', 'supervisor', 'executive')),
    ):
        with db() as conn:
            ranked = bad_actors_view(conn, window_days=window_days, limit=100, site_id=site_id)
        return _application.csv_response(
            'EUAS_reliability_bad_actors.csv',
            ['Asset', 'Name', 'Site', 'Bad Actor Points', 'Failures', 'Emergency Work',
             'Downtime Hours', 'Alarms', 'Maintenance Cost', 'MTBF Hours', 'MTTR Hours',
             'Drivers'],
            [[
                e['asset_no'], e['name'], e.get('site_name') or '', e['bad_actor_points'],
                e['corrective_completions'], e['emergency_count'], e['downtime_hours'],
                e['alarm_count'], e['maintenance_cost'],
                e['mtbf_hours'] if e['mtbf_hours'] is not None else '',
                e['mttr_hours'] if e['mttr_hours'] is not None else '',
                '; '.join(e['drivers']),
            ] for e in ranked],
        )

    @app.get('/api/exports/reliability/deterioration-watchlist.csv')
    def export_watchlist_csv(
        window_days: int = _application.Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        user=Depends(current_user),
    ):
        with db() as conn:
            watchlist = deterioration_watchlist_view(
                conn, window_days=window_days, site_id=site_id
            )
        return _application.csv_response(
            'EUAS_deterioration_watchlist.csv',
            ['Asset', 'Asset Name', 'Channel', 'Channel Name', 'Unit', 'Level',
             'Signals', 'Readings', 'Site'],
            [[
                w['asset_no'], w['asset_name'], w['channel_code'], w['channel_name'],
                w.get('unit') or '', w['level'], '; '.join(w['signals']),
                w['readings'], w.get('site_name') or '',
            ] for w in watchlist],
        )

    @app.get('/api/exports/reliability/fmea.csv')
    def export_fmea_csv(user=Depends(current_user)):
        with db() as conn:
            catalog = fmea_list_view(conn, limit=500)
        return _application.csv_response(
            'EUAS_fmea_catalog.csv',
            ['FMEA No', 'Asset', 'Function', 'Failure Mode', 'Failure Cause',
             'Failure Effect', 'Severity', 'Occurrence', 'Detection', 'RPN', 'Status',
             'Created At'],
            [[
                r['fmea_no'], r.get('asset_no') or '', r['function_text'],
                r['failure_mode'], r['failure_cause'], r['failure_effect'],
                r['severity'], r['occurrence'], r['detection'], r['rpn'], r['status'],
                r['created_at'],
            ] for r in catalog['records']],
        )

    @app.post('/api/reliability/fmea')
    def fmea_create_route(
        body: _application.FMEAIn,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES)),
    ):
        with db() as conn:
            return create_fmea_record(conn, body, user)

    @app.patch('/api/reliability/fmea/{fmea_id}')
    def fmea_update_route(
        fmea_id: int,
        body: _application.FMEAPatch,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES)),
    ):
        with db() as conn:
            return update_fmea_record(conn, fmea_id, body, user)

    @app.post('/api/reliability/fmea/{fmea_id}/approve')
    def fmea_approve_route(
        fmea_id: int,
        user=Depends(require_roles(*RELIABILITY_MANAGE_ROLES)),
    ):
        with db() as conn:
            return approve_fmea_record(conn, fmea_id, user)

    @app.get('/api/reliability/fmea/{fmea_id}/observed-evidence')
    def fmea_observed_route(
        fmea_id: int,
        window_days: int = _application.Query(365, ge=30, le=1825),
        user=Depends(current_user),
    ):
        with db() as conn:
            return fmea_observed_evidence(conn, fmea_id, window_days=window_days)

    app.openapi_schema = None
    # Bind the evidence gatherer so the shared _asset_health implementation
    # upgrades to the documented APM formula for every existing caller
    # (dashboards, automation, CSV export) without import cycles.
    setattr(_application, '_APM_EVIDENCE_GATHERER', gather_health_evidence)
    setattr(app.state, marker, True)
