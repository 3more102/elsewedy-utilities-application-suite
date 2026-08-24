from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _login(username: str, password: str) -> dict[str, str]:
    with TestClient(app) as client:
        response = client.post('/api/auth/login', json={'username': username, 'password': password})
        assert response.status_code == 200, response.text
        return _bearer(response.json()['token'])


def _iso(dt) -> str:
    return dt.isoformat(timespec='seconds')


def _make_asset(conn, asset_no: str, *, condition='Good', criticality='Medium') -> int:
    conn.execute('DELETE FROM assets WHERE asset_no=?', (asset_no,))
    cur = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)''',
        (asset_no, f'APM Test Asset {asset_no}', 'PUMP', criticality, condition, 'Operating', now(), now()),
    )
    return int(cur.lastrowid)


def _make_channel(
    conn,
    code: str,
    asset_id: int,
    *,
    unit='°C',
    warning_high=None,
    critical_high=None,
    warning_low=None,
    critical_low=None,
) -> int:
    conn.execute('DELETE FROM telemetry_channels WHERE channel_code=?', (code,))
    cur = conn.execute(
        '''INSERT INTO telemetry_channels(
             channel_code,asset_id,name,metric_type,unit,source_system,
             warning_low,critical_low,warning_high,critical_high,active,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)''',
        (
            code, asset_id, f'Channel {code}', 'Temperature', unit, 'Test',
            warning_low, critical_low, warning_high, critical_high, now(), now(),
        ),
    )
    return int(cur.lastrowid)


def _add_readings(conn, channel_id: int, values: list[float], *, step_minutes=5):
    from datetime import datetime, timedelta

    base = datetime.now() - timedelta(minutes=step_minutes * len(values))
    for index, value in enumerate(values):
        captured = _iso(base + timedelta(minutes=step_minutes * index))
        conn.execute(
            '''INSERT INTO telemetry_readings(channel_id,value,quality,source,captured_at,ingested_at)
               VALUES(?,?,?,?,?,?)''',
            (channel_id, value, 'Good', 'Test', captured, captured),
        )


def _add_alarm(conn, code: str, asset_id: int, channel_id: int, opened_at: str, *, severity='Warning'):
    conn.execute('DELETE FROM operational_alarms WHERE alarm_no=?', (code,))
    conn.execute(
        '''INSERT INTO operational_alarms(
             alarm_no,channel_id,asset_id,severity,status,alarm_type,message,
             trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count)
           VALUES(?,?,?,?, 'Open','Threshold',?,?,?,?,?,1)''',
        (code, channel_id, asset_id, severity, f'{code} message', 95.0, 80.0, opened_at, opened_at),
    )


# ---------------------------------------------------------------------------
# Authentication scoping
# ---------------------------------------------------------------------------
RELIABILITY_GETS = [
    '/api/reliability/risk-matrix',
    '/api/reliability/deterioration-watchlist',
    '/api/reliability/alarm-correlation',
    '/api/reliability/cbm-recommendations',
    '/api/reliability/bad-actors',
    '/api/reliability/maintenance-effectiveness',
    '/api/reliability/fmea',
]


@pytest.mark.parametrize('path', RELIABILITY_GETS)
def test_reliability_reads_require_authentication(path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    'method,path',
    [
        ('POST', '/api/reliability/cbm-evaluation'),
        ('POST', '/api/reliability/fmea'),
    ],
)
def test_reliability_writes_require_authentication(method, path):
    with TestClient(app) as client:
        response = getattr(client, method.lower())(path, json={})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Health explainability
# ---------------------------------------------------------------------------
def test_health_endpoint_is_explainable():
    headers = _login('exec', 'Viewer@2026')
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-H1', condition='Warning', criticality='High')
        channel_id = _make_channel(conn, 'TEL-APM-H1', asset_id, warning_high=80, critical_high=90)
        # Rising trend that crosses the warning bound repeatedly.
        _add_readings(conn, channel_id, [60, 65, 71, 78, 84, 88])
        conn.commit()
    try:
        with TestClient(app) as client:
            result = client.get(f'/api/reliability/health/{asset_id}', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()

            assert payload['asset_no'] == 'AST-APM-H1'
            health = payload['health']
            assert 0 <= health['score'] <= 100
            assert health['state'] in ('Healthy', 'Monitor', 'Warning', 'Critical')

            factors = [
                c for c in payload['contributors']
                if c['factor'] == 'operational_alarms'
            ]
            # No live alarms were ingested through the alarm path, so the
            # alarm penalty must be absent rather than invented.
            assert factors == []

            # The deterioration evidence must surface the real channel signal.
            assert payload['evidence']['trend_level'] in ('none', 'adverse', 'severe')
            assert any(f['channel_code'] == 'TEL-APM-H1' for f in payload['channel_findings'])
            finding = next(f for f in payload['channel_findings'] if f['channel_code'] == 'TEL-APM-H1')
            assert finding['signals'], 'every reported finding must state its rationale'

            risk = payload['risk']
            for key in ('likelihood', 'consequence', 'risk_score'):
                assert isinstance(risk[key], int)

            # Every nonzero contributor explains itself in words.
            for contributor in payload['contributors']:
                assert contributor['points'] > 0
                assert contributor['detail']
    finally:
        with db() as conn:
            conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel_id,))
            conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


def test_risk_matrix_counts_and_orders_assets():
    headers = _login('exec', 'Viewer@2026')
    with TestClient(app) as client:
        matrix = client.get('/api/reliability/risk-matrix', headers=headers)
        assert matrix.status_code == 200, matrix.text
        payload = matrix.json()
        assert set(payload['counts'].keys()) == {'Extreme', 'High', 'Medium', 'Low'}
        assert sum(payload['counts'].values()) == payload['total']
        scores = [a['risk_score'] for a in payload['assets']]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Deterioration watchlist
# ---------------------------------------------------------------------------
def test_watchlist_flags_adverse_channel_and_ignores_stable():
    headers = _login('planner', 'Planner@2026')
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-W1')
        hot = _make_channel(conn, 'TEL-APM-W1-HOT', asset_id, warning_high=80)
        calm = _make_channel(conn, 'TEL-APM-W1-CALM', asset_id, warning_high=80)
        _add_readings(conn, hot, [55, 62, 68, 75, 82, 86])
        _add_readings(conn, calm, [50, 50.4, 49.8, 50.2, 50.0, 49.9])
        conn.commit()
    try:
        with TestClient(app) as client:
            watchlist = client.get('/api/reliability/deterioration-watchlist', headers=headers)
            assert watchlist.status_code == 200, watchlist.text
            codes = {item['channel_code']: item for item in watchlist.json()}
            assert codes.get('TEL-APM-W1-CALM') is None
            flagged = codes['TEL-APM-W1-HOT']
            assert flagged['level'] in ('adverse', 'severe')
            assert flagged['readings'] == 6
    finally:
        with db() as conn:
            for channel in (hot, calm):
                conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel,))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# Alarm correlation
# ---------------------------------------------------------------------------
def test_alarm_correlation_finds_recurrence_bursts_and_groups():
    headers = _login('exec', 'Viewer@2026')
    from datetime import datetime, timedelta

    base = datetime.now() - timedelta(minutes=30)
    with db() as conn:
        # Seeded demo alarms must not leak into this correlation window.
        conn.execute('DELETE FROM operational_alarms')
        asset_id = _make_asset(conn, 'AST-APM-A1')
        ch1 = _make_channel(conn, 'TEL-APM-A1-C1', asset_id)
        ch2 = _make_channel(conn, 'TEL-APM-A1-C2', asset_id)
        # Storm: five alarms within ten minutes across two channels.
        for index in range(3):
            _add_alarm(conn, f'ALM-APM-STORM{index}', asset_id, ch1, _iso(base + timedelta(minutes=index)), severity='Critical')
        for index in range(2):
            _add_alarm(conn, f'ALM-APM-STORM-B{index}', asset_id, ch2, _iso(base + timedelta(minutes=3 + index)))
        # Quiet history elsewhere: one old alarm long before the window.
        _add_alarm(conn, 'ALM-APM-OLD', asset_id, ch1, _iso(datetime.now() - timedelta(days=5)))
        conn.commit()
    try:
        with TestClient(app) as client:
            result = client.get(
                '/api/reliability/alarm-correlation?hours=2&burst_threshold=5&burst_window_minutes=15',
                headers=headers,
            )
            assert result.status_code == 200, result.text
            payload = result.json()
            assert payload['total_alarms'] == 5  # the 5-day-old alarm stays outside the window

            storm = payload['bursts'][0]
            assert storm['alarms'] >= 5
            assert storm['asset_no'] == 'AST-APM-A1'
            assert 'probable common source' in storm['rationale']
            assert set(storm['channels']) == {'TEL-APM-A1-C1', 'TEL-APM-A1-C2'}

            recurrence_codes = {r['channel_code'] for r in payload['recurrence']}
            assert {'TEL-APM-A1-C1', 'TEL-APM-A1-C2'} <= recurrence_codes

            groups = {g['asset_no']: g for g in payload['groups']}
            assert 'AST-APM-A1' in groups
            assert groups['AST-APM-A1']['alarm_count'] == 5
    finally:
        with db() as conn:
            for code in [f'ALM-APM-STORM{i}' for i in range(3)] + \
                        [f'ALM-APM-STORM-B{i}' for i in range(2)] + ['ALM-APM-OLD']:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no=?', (code,))
            for channel in (ch1, ch2):
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# CBM lifecycle
# ---------------------------------------------------------------------------
def test_cbm_recommendation_lifecycle_and_authorization():
    manager = _login('seif', 'EUAS@2026')
    planner = _login('planner', 'Planner@2026')
    technician = _login('tech1', 'Tech@2026')

    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-CBM')
        channel_id = _make_channel(conn, 'TEL-APM-CBM', asset_id, warning_high=80, critical_high=90)
        # Severe: critical excursions persisting across consecutive readings.
        _add_readings(conn, channel_id, [85, 92, 94, 96])
        conn.commit()

    recommendation_id = None
    try:
        with TestClient(app) as client:
            denied = client.post('/api/reliability/cbm-evaluation', headers=technician)
            assert denied.status_code == 403, denied.text

            first_run = client.post('/api/reliability/cbm-evaluation', headers=manager)
            assert first_run.status_code == 200, first_run.text
            created = first_run.json()['created']
            mine = [c for c in created if c['channel_code'] == 'TEL-APM-CBM']
            assert len(mine) == 1
            recommendation_id = mine[0]['recommendation_no']

            # Idempotent: an open recommendation suppresses duplicates.
            second_run = client.post('/api/reliability/cbm-evaluation', headers=manager)
            assert second_run.status_code == 200
            still_open = [
                c for c in second_run.json()['created']
                if c['channel_code'] == 'TEL-APM-CBM'
            ]
            assert still_open == []

            listing = client.get('/api/reliability/cbm-recommendations?status=Open', headers=technician)
            assert listing.status_code == 200
            entry = next(r for r in listing.json() if r['recommendation_no'] == recommendation_id)
            assert entry['confidence'] == 'deterministic'
            assert entry['severity'] in ('High', 'Critical')

            base = f'/api/reliability/cbm-recommendations/{entry["id"]}'
            forbidden = client.post(f'{base}/review', headers=technician)
            assert forbidden.status_code == 403

            reviewed = client.post(
                f'{base}/review',
                headers=manager,
                json={'suggested_action': 'Inspect cooling circuit and verify oil quality'},
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()['status'] == 'Reviewed'

            approved = client.post(f'{base}/approve', headers=manager)
            assert approved.status_code == 200
            assert approved.json()['status'] == 'Approved'

            converted = client.post(f'{base}/convert-to-work-order', headers=planner)
            assert converted.status_code == 200, converted.text
            assert converted.json()['existing'] is False
            wo_no = converted.json()['wo_no']

            with db() as conn:
                wo = conn.execute(
                    'SELECT * FROM work_orders WHERE wo_no=?', (wo_no,)
                ).fetchone()
                assert wo['status'] == 'Submitted'
                assert wo['work_type'] == 'Corrective'
                assert wo['asset_id'] == asset_id

            replayed = client.post(f'{base}/convert-to-work-order', headers=planner)
            assert replayed.status_code == 200
            assert replayed.json()['existing'] is True
            assert replayed.json()['work_order_id'] == converted.json()['work_order_id']

            decided = client.post(f'{base}/dismiss', headers=manager)
            assert decided.status_code == 409  # Approved is terminal here
    finally:
        with db() as conn:
            if recommendation_id:
                conn.execute(
                    'DELETE FROM cbm_recommendations WHERE recommendation_no=?',
                    (recommendation_id,),
                )
            conn.execute(
                'DELETE FROM work_order_sla WHERE work_order_id IN '
                '(SELECT id FROM work_orders WHERE asset_id=?)',
                (asset_id,),
            )
            conn.execute('DELETE FROM work_orders WHERE asset_id=?', (asset_id,))
            conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel_id,))
            conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


def test_cbm_dismiss_path():
    manager = _login('seif', 'EUAS@2026')
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-DIS')
        channel_id = _make_channel(conn, 'TEL-APM-DIS', asset_id, warning_high=80)
        _add_readings(conn, channel_id, [60, 70, 83, 85])
        conn.commit()
    try:
        with TestClient(app) as client:
            run = client.post('/api/reliability/cbm-evaluation', headers=manager)
            assert run.status_code == 200
            mine = [
                c for c in run.json()['created']
                if c['channel_code'] == 'TEL-APM-DIS'
            ]
            assert len(mine) == 1
            internal_id = None
            listing = client.get(
                '/api/reliability/cbm-recommendations?status=Open', headers=manager
            ).json()
            entry = next(r for r in listing if r['recommendation_no'] == mine[0]['recommendation_no'])
            internal_id = entry['id']

            dismissed = client.post(
                f'/api/reliability/cbm-recommendations/{internal_id}/dismiss',
                headers=manager,
            )
            assert dismissed.status_code == 200
            assert dismissed.json()['status'] == 'Dismissed'

            again = client.post(
                f'/api/reliability/cbm-recommendations/{internal_id}/review',
                headers=manager,
            )
            assert again.status_code == 409
    finally:
        with db() as conn:
            conn.execute('DELETE FROM cbm_recommendations WHERE asset_id=?', (asset_id,))
            conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel_id,))
            conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# Bad actors
# ---------------------------------------------------------------------------
def test_bad_actors_ranking_and_anonymous_denial():
    with TestClient(app) as client:
        assert client.get('/api/reliability/bad-actors').status_code == 401

    headers = _login('exec', 'Viewer@2026')
    with TestClient(app) as client:
        ranking = client.get('/api/reliability/bad-actors?limit=5', headers=headers)
        assert ranking.status_code == 200, ranking.text
        entries = ranking.json()
        assert isinstance(entries, list)
        points = [e['bad_actor_points'] for e in entries]
        assert points == sorted(points, reverse=True)
        for entry in entries:
            assert 'mtbf_hours' in entry and 'drivers' in entry


# ---------------------------------------------------------------------------
# FMEA workflow
# ---------------------------------------------------------------------------
def test_fmea_draft_approve_immutability_and_observed_evidence():
    admin = _login('omar', 'EUAS@2026')
    technician = _login('tech1', 'Tech@2026')

    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-FMEA')
        conn.commit()
    fmea_id = None
    try:
        with TestClient(app) as client:
            forbidden = client.post('/api/reliability/fmea', headers=technician, json={
                'asset_id': asset_id,
                'function_text': 'Circulate coolant at design flow',
                'failure_mode': 'BearingSeizure',
                'severity': 7, 'occurrence': 3, 'detection': 4,
            })
            assert forbidden.status_code == 403

            created = client.post('/api/reliability/fmea', headers=admin, json={
                'asset_id': asset_id,
                'function_text': 'Circulate coolant at design flow',
                'failure_mode': 'BearingSeizure',
                'failure_cause': 'Lubrication starvation',
                'failure_effect': 'Unplanned outage',
                'severity': 7, 'occurrence': 3, 'detection': 4,
            })
            assert created.status_code == 200, created.text
            body = created.json()
            fmea_id = body['id']
            assert body['rpn'] == 7 * 3 * 4
            assert body['status'] == 'Draft'

            patched = client.patch(f'/api/reliability/fmea/{fmea_id}', headers=admin, json={
                'occurrence': 4,
            })
            assert patched.status_code == 200
            assert patched.json()['rpn'] == 7 * 4 * 4

            # Seed observed history: two completed corrective WOs that match
            # the failure mode exactly plus one unrelated one.
            with db() as conn:
                for suffix, failure_code in (('A', 'BearingSeizure'), ('B', 'BearingSeizure'), ('C', 'Other')):
                    conn.execute(
                        '''INSERT INTO work_orders(
                             wo_no,title,asset_id,priority,status,work_type,failure_code,
                             requested_by,actual_finish,created_at,updated_at)
                           VALUES(?,?,?,?, 'Completed','Corrective',?,?,?,?,?)''',
                        (
                            f'WO-APM-FMEA-{suffix}', f'Fix {suffix}', asset_id, 'Medium',
                            failure_code, 1, '2026-08-20T10:00:00', now(), now(),
                        ),
                    )

            evidence = client.get(f'/api/reliability/fmea/{fmea_id}/observed-evidence', headers=admin)
            assert evidence.status_code == 200, evidence.text
            observed = evidence.json()
            assert observed['observed_occurrences'] == 2
            assert observed['expected_occurrence'] == 4
            assert observed['alignment'] in ('consistent', 'divergent')
            assert 'failure_code match' in observed['note']

            approved = client.post(f'/api/reliability/fmea/{fmea_id}/approve', headers=admin)
            assert approved.status_code == 200

            locked = client.patch(f'/api/reliability/fmea/{fmea_id}', headers=admin, json={
                'severity': 1,
            })
            assert locked.status_code == 409
    finally:
        with db() as conn:
            if fmea_id:
                conn.execute('DELETE FROM fmea_records WHERE id=?', (fmea_id,))
            conn.execute("DELETE FROM work_orders WHERE wo_no LIKE 'WO-APM-FMEA-%'")
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# Post-maintenance effectiveness
# ---------------------------------------------------------------------------
def test_effectiveness_requires_completed_work_order():
    headers = _login('planner', 'Planner@2026')
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-EFF')
        conn.commit()
    wo_id = None
    try:
        with TestClient(app) as client:
            created = client.post('/api/work-orders', headers=headers, json={
                'title': 'Effectiveness probe', 'asset_id': asset_id,
                'priority': 'Low', 'work_type': 'Corrective',
            })
            assert created.status_code == 200, created.text
            wo_id = created.json()['id']

            early = client.get(f'/api/work-orders/{wo_id}/effectiveness', headers=headers)
            assert early.status_code == 409
            assert 'Completed/Closed' in early.json()['detail']
    finally:
        with db() as conn:
            if wo_id:
                conn.execute('DELETE FROM work_order_sla WHERE work_order_id=?', (wo_id,))
                conn.execute('DELETE FROM work_orders WHERE id=?', (wo_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


def test_effectiveness_verdict_and_recurring_issue_detection():
    from datetime import datetime, timedelta

    headers = _login('planner', 'Planner@2026')
    finish = datetime.now() - timedelta(days=3)
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-EFF2')
        channel_id = _make_channel(conn, 'TEL-APM-EFF2', asset_id, warning_high=80)
        conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,asset_id,priority,status,work_type,failure_code,
                 requested_by,actual_start,actual_finish,created_at,updated_at)
               VALUES('WO-APM-EFF2','Cooling repair',?,'High','Completed','Corrective','Overheat',
                      ?,?,?,?,?)''',
            (
                asset_id, 1,
                _iso(finish - timedelta(hours=6)), _iso(finish), now(), now(),
            ),
        )
        wo_id = int(conn.execute(
            "SELECT id FROM work_orders WHERE wo_no='WO-APM-EFF2'"
        ).fetchone()[0])
        # Before repair: repeated abnormal readings and alarms on the channel.
        _add_readings(conn, channel_id, [82, 88, 91], step_minutes=60)
        _add_alarm(conn, 'ALM-APM-EFF2-PRE', asset_id, channel_id,
                   _iso(finish - timedelta(days=1)))
        # After repair: the same channel misbehaves again -> recurring issue.
        _add_alarm(conn, 'ALM-APM-EFF2-POST', asset_id, channel_id,
                   _iso(finish + timedelta(days=1)))
        conn.commit()
    try:
        with TestClient(app) as client:
            result = client.get(
                f'/api/work-orders/{wo_id}/effectiveness?window_days=30', headers=headers
            )
            assert result.status_code == 200, result.text
            payload = result.json()
            assert payload['verdict'] in (
                'improved', 'unchanged', 'regressed', 'mixed', 'insufficient_data'
            )
            # One alarm before and one after on the same channel must be
            # recognized as a recurring issue regardless of overall verdict.
            codes = {r['channel_code'] for r in payload['recurring_issues']}
            assert codes == {'TEL-APM-EFF2'}
            assert payload['pre']['alarms'] == 1
            assert payload['post']['alarms'] == 1

            listed = client.get('/api/reliability/maintenance-effectiveness', headers=headers)
            assert listed.status_code == 200
            mine = [x for x in listed.json() if x['wo_no'] == 'WO-APM-EFF2']
            assert len(mine) == 1
    finally:
        with db() as conn:
            for code in ('ALM-APM-EFF2-PRE', 'ALM-APM-EFF2-POST'):
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no=?', (code,))
            conn.execute('DELETE FROM work_order_sla WHERE work_order_id=?', (wo_id,))
            conn.execute('DELETE FROM work_orders WHERE id=?', (wo_id,))
            conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel_id,))
            conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# Scope-isolated fixtures
# ---------------------------------------------------------------------------
def _make_scoped_site(conn, site_code: str) -> tuple[int, int]:
    conn.execute('DELETE FROM sites WHERE site_code=?', (site_code,))
    cur = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type)
           VALUES(?,?, 'Greater Cairo', 'Cairo', 'Operations Centre')''',
        (site_code, f'Scope probe site {site_code}'),
    )
    site_id = int(cur.lastrowid)
    conn.execute('DELETE FROM locations WHERE location_code=?', (f'L-{site_code}',))
    loc = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?, 'Area', ?)''',
        (f'L-{site_code}', f'Scope bay {site_code}', site_id),
    )
    return site_id, int(loc.lastrowid)


def _make_site_asset(conn, asset_no: str, location_id: int, *, condition='Good', criticality='Medium') -> int:
    conn.execute('DELETE FROM assets WHERE asset_no=?', (asset_no,))
    cur = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (asset_no, f'APM Scoped Asset {asset_no}', 'PUMP', criticality, condition, 'Operating',
         location_id, now(), now()),
    )
    return int(cur.lastrowid)


def _add_site_alarm(
    conn, code: str, asset_id: int, channel_id: int, site_id: Optional[int], opened_at: str,
    *, severity='Warning',
) -> int:
    conn.execute('DELETE FROM operational_alarms WHERE alarm_no=?', (code,))
    cur = conn.execute(
        '''INSERT INTO operational_alarms(
             alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,message,
             trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count)
           VALUES(?,?,?,?, 'Open','Threshold',?,?,?,?,?,?,1)''',
        (code, channel_id, asset_id, site_id, severity, f'{code} message', 95.0, 80.0,
         opened_at, opened_at),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Site scoping across reliability reads
# ---------------------------------------------------------------------------
def test_reliability_reads_honor_site_scope():
    headers = _login('exec', 'Viewer@2026')
    manager = _login('seif', 'EUAS@2026')

    with db() as conn:
        site_a, loc_a = _make_scoped_site(conn, 'SCOPE-A')
        site_b, loc_b = _make_scoped_site(conn, 'SCOPE-B')
        asset_a = _make_site_asset(conn, 'AST-SCOPE-A', loc_a)
        asset_b = _make_site_asset(conn, 'AST-SCOPE-B', loc_b)
        chan_a = _make_channel(conn, 'TEL-SCOPE-A', asset_a, warning_high=80)
        chan_b = _make_channel(conn, 'TEL-SCOPE-B', asset_b, warning_high=80)
        _add_readings(conn, chan_a, [55, 62, 68, 75, 82, 86])
        _add_readings(conn, chan_b, [55, 62, 68, 75, 82, 86])
        conn.commit()
    try:
        with TestClient(app) as client:
            all_rows = client.get('/api/reliability/deterioration-watchlist', headers=headers)
            assert all_rows.status_code == 200, all_rows.text
            all_codes = {item['channel_code'] for item in all_rows.json()}
            assert {'TEL-SCOPE-A', 'TEL-SCOPE-B'} <= all_codes

            scoped_a = client.get(
                f'/api/reliability/deterioration-watchlist?site_id={site_a}', headers=headers
            )
            assert scoped_a.status_code == 200, scoped_a.text
            assert {item['channel_code'] for item in scoped_a.json()} == {'TEL-SCOPE-A'}

            scoped_b = client.get(
                f'/api/reliability/deterioration-watchlist?site_id={site_b}', headers=headers
            )
            assert {item['channel_code'] for item in scoped_b.json()} == {'TEL-SCOPE-B'}

            actors_a = client.get(
                f'/api/reliability/bad-actors?window_days=30&limit=100&site_id={site_a}',
                headers=headers,
            )
            assert actors_a.status_code == 200, actors_a.text
            actor_assets = {entry['asset_no'] for entry in actors_a.json()}
            assert 'AST-SCOPE-A' in actor_assets
            assert 'AST-SCOPE-B' not in actor_assets

            evaluation = client.post('/api/reliability/cbm-evaluation', headers=manager)
            assert evaluation.status_code == 200, evaluation.text

            cbm_a = client.get(
                f'/api/reliability/cbm-recommendations?site_id={site_a}', headers=headers
            )
            assert cbm_a.status_code == 200, cbm_a.text
            cbm_a_assets = {row['asset_no'] for row in cbm_a.json()}
            assert 'AST-SCOPE-A' in cbm_a_assets
            assert 'AST-SCOPE-B' not in cbm_a_assets

            cbm_by_asset = client.get(
                f'/api/reliability/cbm-recommendations?asset_id={asset_b}', headers=headers
            )
            cbm_b_assets = {row['asset_no'] for row in cbm_by_asset.json()}
            assert cbm_b_assets == {'AST-SCOPE-B'}
    finally:
        with db() as conn:
            for asset_id in (asset_a, asset_b):
                conn.execute('DELETE FROM cbm_recommendations WHERE asset_id=?', (asset_id,))
            for channel in (chan_a, chan_b):
                conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (channel,))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel,))
            for asset_id in (asset_a, asset_b):
                conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
            for site_id in (site_a, site_b):
                conn.execute('DELETE FROM locations WHERE site_id=?', (site_id,))
                conn.execute('DELETE FROM sites WHERE id=?', (site_id,))


# ---------------------------------------------------------------------------
# Alarm correlation identifiers
# ---------------------------------------------------------------------------
def test_alarm_correlation_identifiers_stable_attributed_and_scoped():
    from datetime import datetime, timedelta

    headers = _login('exec', 'Viewer@2026')
    base = datetime.now() - timedelta(minutes=30)
    with db() as conn:
        site_id, loc_id = _make_scoped_site(conn, 'COR-A')
        other_site, _other_loc = _make_scoped_site(conn, 'COR-B')
        asset_id = _make_site_asset(conn, 'AST-COR-A', loc_id)
        ch1 = _make_channel(conn, 'TEL-COR-A-C1', asset_id)
        ch2 = _make_channel(conn, 'TEL-COR-A-C2', asset_id)
        storm_codes = [f'ALM-COR-STORM{i}' for i in range(3)] + \
                      [f'ALM-COR-STORM-B{i}' for i in range(2)]
        storm_ids = {}
        for index in range(3):
            opened = _iso(base + timedelta(minutes=index))
            storm_ids[f'ALM-COR-STORM{index}'] = _add_site_alarm(
                conn, f'ALM-COR-STORM{index}', asset_id, ch1, site_id, opened,
                severity='Critical',
            )
        for index in range(2):
            opened = _iso(base + timedelta(minutes=3 + index))
            storm_ids[f'ALM-COR-STORM-B{index}'] = _add_site_alarm(
                conn, f'ALM-COR-STORM-B{index}', asset_id, ch2, site_id, opened,
            )
        # One alarm at another site must never appear in site A correlations.
        stray_id = _add_site_alarm(
            conn, 'ALM-COR-STRAY', asset_id, ch1, other_site,
            _iso(base + timedelta(minutes=1)),
        )
        conn.commit()
    try:
        with TestClient(app) as client:
            def fetch(site):
                url = '/api/reliability/alarm-correlation?hours=2&burst_threshold=5&burst_window_minutes=15'
                if site is not None:
                    url += f'&site_id={site}'
                response = client.get(url, headers=headers)
                assert response.status_code == 200, response.text
                return response.json()

            first = fetch(site_id)
            burst = next(b for b in first['bursts'] if b['asset_no'] == 'AST-COR-A')
            group = next(g for g in first['groups'] if g['asset_no'] == 'AST-COR-A')
            recurrences = [
                r for r in first['recurrence']
                if r['channel_code'] in ('TEL-COR-A-C1', 'TEL-COR-A-C2')
            ]

            expected_ids = sorted(storm_ids.values())
            for cluster in (burst, group):
                assert cluster['primary_alarm_id'] == expected_ids[0]
                assert cluster['related_alarm_ids'] == expected_ids[1:]
                assert sorted(cluster['alarm_nos']) == sorted(storm_codes)
                assert stray_id not in cluster['related_alarm_ids']
                assert cluster['correlation_id'].startswith('COR-')
            for entry in recurrences:
                assert entry['correlation_id'].startswith('COR-')
                assert entry['primary_alarm_id'] in storm_ids.values()

            second = fetch(site_id)
            burst_again = next(b for b in second['bursts'] if b['asset_no'] == 'AST-COR-A')
            group_again = next(g for g in second['groups'] if g['asset_no'] == 'AST-COR-A')
            assert burst_again['correlation_id'] == burst['correlation_id']
            assert group_again['correlation_id'] == group['correlation_id']

            elsewhere = fetch(other_site)
            for cluster in elsewhere['bursts'] + elsewhere['groups']:
                members = [cluster['primary_alarm_id'], *cluster['related_alarm_ids']]
                assert stray_id not in members or cluster['asset_no'] != 'AST-COR-A'
                if cluster['asset_no'] == 'AST-COR-A':
                    continue
                assert cluster['correlation_id'] != burst['correlation_id']

            recurrence_a = [
                r for r in first['recurrence'] if r['channel_code'] == 'TEL-COR-A-C1'
            ][0]
            assert stray_id not in [recurrence_a['primary_alarm_id'],
                                    *recurrence_a['related_alarm_ids']]
    finally:
        with db() as conn:
            conn.execute("DELETE FROM operational_alarms WHERE alarm_no LIKE 'ALM-COR-%'")
            for channel in (ch1, ch2):
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
            for sid in (site_id, other_site):
                conn.execute('DELETE FROM locations WHERE site_id=?', (sid,))
                conn.execute('DELETE FROM sites WHERE id=?', (sid,))


# ---------------------------------------------------------------------------
# CBM list pagination
# ---------------------------------------------------------------------------
def test_cbm_pagination_is_additive_and_validated():
    manager = _login('seif', 'EUAS@2026')
    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-CBMPG')
        seeded = []
        for index in range(5):
            cur = conn.execute(
                '''INSERT INTO cbm_recommendations(
                     recommendation_no,asset_id,condition_type,severity,
                     evidence_json,suggested_action,confidence,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)''',
                (
                    f'CBM-PG-{index}', asset_id, 'trend_deterioration', 'High',
                    '{}', 'Inspect channel', 'deterministic', 'Open', now(),
                ),
            )
            seeded.append(int(cur.lastrowid))
        conn.commit()
    try:
        with TestClient(app) as client:
            base = f'/api/reliability/cbm-recommendations?asset_id={asset_id}'

            default_view = client.get(base, headers=manager).json()
            assert set(seeded) <= {row['id'] for row in default_view}

            page_one = client.get(f'{base}&limit=2', headers=manager).json()
            page_two = client.get(f'{base}&limit=2&offset=2', headers=manager).json()
            assert len(page_one) == 2 and len(page_two) == 2
            ids_one = [row['id'] for row in page_one]
            ids_two = [row['id'] for row in page_two]
            assert not set(ids_one) & set(ids_two)

            # Exact windows of the canonical status-ordered, id-tiebroken sequence.
            full = client.get(f'{base}&limit=1000', headers=manager).json()
            expected = [row['id'] for row in full if row['id'] in seeded]
            assert expected == sorted(seeded, reverse=True)
            assert ids_one + ids_two == expected[:4]

            for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
                response = client.get(
                    '/api/reliability/cbm-recommendations', headers=manager, params=params
                )
                assert response.status_code == 422, params
    finally:
        with db() as conn:
            conn.execute('DELETE FROM cbm_recommendations WHERE asset_id=?', (asset_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))


# ---------------------------------------------------------------------------
# Site-level burst correlation
# ---------------------------------------------------------------------------
def test_site_burst_correlation_across_assets_and_scope():
    from datetime import datetime, timedelta

    headers = _login('exec', 'Viewer@2026')
    base = datetime.now() - timedelta(minutes=25)
    with db() as conn:
        site_id, loc_id = _make_scoped_site(conn, 'SBURST-A')
        quiet_site, quiet_loc = _make_scoped_site(conn, 'SBURST-B')
        asset_x = _make_site_asset(conn, 'AST-SBURST-X', loc_id)
        asset_y = _make_site_asset(conn, 'AST-SBURST-Y', loc_id)
        chan_x = _make_channel(conn, 'TEL-SBURST-X', asset_x)
        chan_y = _make_channel(conn, 'TEL-SBURST-Y', asset_y)
        quiet_asset = _make_site_asset(conn, 'AST-SBURST-Q', quiet_loc)
        quiet_chan = _make_channel(conn, 'TEL-SBURST-Q', quiet_asset)
        member_ids: list[int] = []
        # Six alarms across two different assets at one site inside 4 minutes.
        for index in range(3):
            opened = _iso(base + timedelta(minutes=index))
            member_ids.append(_add_site_alarm(
                conn, f'ALM-SBURST-X{index}', asset_x, chan_x, site_id, opened))
            member_ids.append(_add_site_alarm(
                conn, f'ALM-SBURST-Y{index}', asset_y, chan_y, site_id,
                _iso(base + timedelta(minutes=index, seconds=30))))
        # A quiet site must never be dragged into the cluster.
        _add_site_alarm(
            conn, 'ALM-SBURST-Q0', quiet_asset, quiet_chan, quiet_site,
            _iso(base + timedelta(minutes=1)),
        )
        conn.commit()
    try:
        with TestClient(app) as client:
            def fetch(site):
                url = ('/api/reliability/alarm-correlation'
                       '?hours=2&burst_threshold=50&site_burst_threshold=6'
                       '&burst_window_minutes=5')
                if site is not None:
                    url += f'&site_id={site}'
                response = client.get(url, headers=headers)
                assert response.status_code == 200, response.text
                return response.json()

            scoped = fetch(site_id)
            assert len(scoped['site_bursts']) == 1
            site_burst = scoped['site_bursts'][0]
            assert site_burst['distinct_assets'] == 2
            assert site_burst['alarms'] == 6
            assert sorted(site_burst['assets']) == ['AST-SBURST-X', 'AST-SBURST-Y']
            cluster_members = [site_burst['primary_alarm_id'], *site_burst['related_alarm_ids']]
            assert sorted(cluster_members) == sorted(member_ids)
            assert 'probable common upstream condition' in site_burst['rationale']

            repeat = fetch(site_id)
            assert repeat['site_bursts'][0]['correlation_id'] == \
                site_burst['correlation_id']

            elsewhere = fetch(quiet_site)
            for cluster in elsewhere['bursts'] + elsewhere['groups']:
                members = [cluster.get('primary_alarm_id'), *cluster.get('related_alarm_ids', [])]
                assert not set(member_ids) & set(members)

            unscoped = fetch(None)
            unscoped_members = [
                m for c in unscoped['site_bursts']
                for m in [c['primary_alarm_id'], *c['related_alarm_ids']]
            ]
            assert set(member_ids) <= set(unscoped_members)
    finally:
        with db() as conn:
            conn.execute("DELETE FROM operational_alarms WHERE alarm_no LIKE 'ALM-SBURST-%'")
            for channel in (chan_x, chan_y, quiet_chan):
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel,))
            conn.execute("DELETE FROM assets WHERE asset_no LIKE 'AST-SBURST-%'")
            for sid in (site_id, quiet_site):
                conn.execute('DELETE FROM locations WHERE site_id=?', (sid,))
                conn.execute('DELETE FROM sites WHERE id=?', (sid,))


# ---------------------------------------------------------------------------
# Reliability CSV exports
# ---------------------------------------------------------------------------
EXPORT_PATHS = [
    '/api/exports/reliability/bad-actors.csv',
    '/api/exports/reliability/deterioration-watchlist.csv',
    '/api/exports/reliability/fmea.csv',
]


@pytest.mark.parametrize('path', EXPORT_PATHS)
def test_reliability_exports_require_authentication(path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 401


def test_reliability_exports_render_seeded_records():
    from datetime import datetime, timedelta

    exec_headers = _login('exec', 'Viewer@2026')
    admin = _login('omar', 'EUAS@2026')

    with db() as conn:
        site_id, loc_id = _make_scoped_site(conn, 'EXP-A')
        asset_id = _make_site_asset(conn, 'AST-EXPORT-A', loc_id)
        chan_id = _make_channel(conn, 'TEL-EXPORT-A', asset_id, warning_high=80)
        _add_readings(conn, chan_id, [55, 62, 68, 75, 82, 86])
        opened = _iso(datetime.now() - timedelta(minutes=10))
        for index in range(2):
            _add_site_alarm(
                conn, f'ALM-EXPORT-{index}', asset_id, chan_id, site_id,
                _iso(datetime.now() - timedelta(minutes=10 + index)),
            )
        conn.commit()

    fmea_id = None
    try:
        with TestClient(app) as client:
            created = client.post('/api/reliability/fmea', headers=admin, json={
                'asset_id': asset_id,
                'function_text': 'Contain process fluid',
                'failure_mode': 'ExportProbeMode',
                'severity': 5, 'occurrence': 4, 'detection': 3,
            })
            assert created.status_code == 200, created.text
            fmea_id = created.json()['id']

            watchlist = client.get(
                f'/api/exports/reliability/deterioration-watchlist.csv?site_id={site_id}',
                headers=exec_headers,
            )
            assert watchlist.status_code == 200, watchlist.text
            assert 'text/csv' in watchlist.headers['content-type']
            body = watchlist.text
            assert body.splitlines()[0].startswith('Asset,Asset Name,Channel')
            assert 'AST-EXPORT-A' in body and 'TEL-EXPORT-A' in body
            other_site_view = client.get(
                '/api/exports/reliability/deterioration-watchlist.csv'
                f'?site_id={site_id}&window_days=30',
                headers=exec_headers,
            )
            assert 'AST-EXPORT-A' in other_site_view.text

            actors = client.get('/api/exports/reliability/bad-actors.csv', headers=admin)
            assert actors.status_code == 200, actors.text
            actor_body = actors.text
            assert actor_body.splitlines()[0].startswith('Asset,Name,Site,Bad Actor Points')
            assert 'AST-EXPORT-A' in actor_body

            catalog = client.get('/api/exports/reliability/fmea.csv', headers=exec_headers)
            assert catalog.status_code == 200, catalog.text
            catalog_body = catalog.text
            assert catalog_body.splitlines()[0].startswith('FMEA No,Asset,Function')
            assert 'ExportProbeMode' in catalog_body
    finally:
        with db() as conn:
            if fmea_id:
                conn.execute('DELETE FROM fmea_records WHERE id=?', (fmea_id,))
            conn.execute("DELETE FROM operational_alarms WHERE alarm_no LIKE 'ALM-EXPORT-%'")
            conn.execute('DELETE FROM telemetry_readings WHERE channel_id=?', (chan_id,))
            conn.execute('DELETE FROM telemetry_channels WHERE id=?', (chan_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
            conn.execute('DELETE FROM locations WHERE site_id=?', (site_id,))
            conn.execute('DELETE FROM sites WHERE id=?', (site_id,))


def test_bad_actor_export_enforces_role_ceiling():
    technician = _login('tech1', 'Tech@2026')
    with TestClient(app) as client:
        denied = client.get(
            '/api/exports/reliability/bad-actors.csv', headers=technician
        )
        assert denied.status_code == 403


# ---------------------------------------------------------------------------
# FMEA catalog listing
# ---------------------------------------------------------------------------
def test_fmea_catalog_listing_filters_and_pagination():
    admin = _login('omar', 'EUAS@2026')

    with db() as conn:
        asset_id = _make_asset(conn, 'AST-APM-FMEACAT')
        conn.commit()
    created_ids: list[int] = []
    try:
        with TestClient(app) as client:
            for mode, occ in (('BearingSeizure', 3), ('SealLeak', 5)):
                created = client.post('/api/reliability/fmea', headers=admin, json={
                    'asset_id': asset_id,
                    'function_text': 'Contain process fluid',
                    'failure_mode': mode,
                    'severity': 6, 'occurrence': occ, 'detection': 4,
                })
                assert created.status_code == 200, created.text
                created_ids.append(created.json()['id'])

            catalog = client.get(
                f'/api/reliability/fmea?asset_id={asset_id}&limit=100', headers=admin
            )
            assert catalog.status_code == 200, catalog.text
            payload = catalog.json()
            assert payload['total'] == 2
            assert payload['limit'] == 100 and payload['offset'] == 0
            modes = [r['failure_mode'] for r in payload['records']]
            assert modes == ['SealLeak', 'BearingSeizure']  # newest first
            assert payload['records'][0]['asset_no'] == 'AST-APM-FMEACAT'
            assert all(r['asset_id'] == asset_id for r in payload['records'])

            page = client.get(
                f'/api/reliability/fmea?asset_id={asset_id}&limit=1&offset=1', headers=admin
            ).json()
            assert page['total'] == 2
            assert [r['failure_mode'] for r in page['records']] == ['BearingSeizure']

            approved = client.post(
                f"/api/reliability/fmea/{payload['records'][0]['id']}/approve", headers=admin
            )
            assert approved.status_code == 200

            approved_view = client.get(
                f'/api/reliability/fmea?asset_id={asset_id}&status=Approved', headers=admin
            ).json()
            assert approved_view['total'] == 1
            assert approved_view['records'][0]['failure_mode'] == 'SealLeak'

            draft_view = client.get(
                f'/api/reliability/fmea?asset_id={asset_id}&status=Draft', headers=admin
            ).json()
            assert draft_view['total'] == 1
            assert draft_view['records'][0]['failure_mode'] == 'BearingSeizure'

            assert client.get(
                '/api/reliability/fmea?limit=0', headers=admin
            ).status_code == 422
            assert client.get(
                '/api/reliability/fmea?limit=501', headers=admin
            ).status_code == 422
            assert client.get(
                '/api/reliability/fmea?offset=-1', headers=admin
            ).status_code == 422
            assert client.get(
                '/api/reliability/fmea/99999999/observed-evidence', headers=admin
            ).status_code == 404
    finally:
        with db() as conn:
            for record_id in created_ids:
                conn.execute('DELETE FROM fmea_records WHERE id=?', (record_id,))
            conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
