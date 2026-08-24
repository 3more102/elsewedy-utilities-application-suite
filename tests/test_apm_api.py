from __future__ import annotations

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
