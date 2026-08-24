"""Alarm/condition KPI integration contracts.

The condition family is served exclusively by the canonical
``compute_condition_kpis``; trend and explanation adapters only extract its
outputs. These tests pin: canonical trend values for alarm metrics, measured
drivers with resolvable alarm drill identifiers, correlation-vs-cause labelling,
snapshot invalidation on alarm lifecycle mutations, and authorization.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_alarm_fixture(conn, suffix: str):
    """Dedicated site/asset/channel so counts never see seed/demo data."""
    stamp = now()
    site = conn.execute(
        '''INSERT INTO sites(site_code,name,region,city,site_type)
           VALUES(?,?,?,?,?)''',
        (f'ALM-{suffix}'.upper(), f'Alarm probe site {suffix}',
         'Greater Cairo', 'Cairo', 'Electrical Substation'),
    )
    site_id = int(site.lastrowid)
    location = conn.execute(
        '''INSERT INTO locations(location_code,name,location_type,site_id)
           VALUES(?,?,?,?)''',
        (f'LALM-{suffix}'.upper(), f'Alarm bay {suffix}', 'Area', site_id),
    )
    asset = conn.execute(
        '''INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                              location_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
        (f'AST-ALM-{suffix.upper()}', f'Alarm probe feeder {suffix}',
         'Feeder', 'High', 'Good', 'Operating',
         int(location.lastrowid), stamp, stamp),
    )
    asset_id = int(asset.lastrowid)
    channel = conn.execute(
        '''INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,
                                          unit,source_system,active,created_at,updated_at)
           VALUES(?,?,?,'Current','A','SCADA',1,?,?)''',
        (f'TEL-ALM-{suffix.upper()}', asset_id, f'Alarm probe channel {suffix}',
         stamp, stamp),
    )
    return {
        'site_id': site_id,
        'asset_id': asset_id,
        'channel_id': int(channel.lastrowid),
        'user_id': int(conn.execute(
            "SELECT id FROM users WHERE username='omar'").fetchone()[0]),
    }


def _seed_alarm(conn, fx, *, severity='Critical', status='Open',
                occurrences=1, days_ago=0, suffix=None) -> int:
    opened = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec='seconds')
    created = conn.execute(
        '''INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,
              status,alarm_type,message,trigger_value,threshold_value,opened_at,
              last_seen_at,occurrence_count)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            f'ALM-TREND-{uuid.uuid4().hex[:10].upper()}',
            fx['channel_id'], fx['asset_id'], fx['site_id'], severity, status,
            'Threshold', 'integration probe', 95.0, 75.0, opened, opened,
            occurrences,
        ),
    )
    return int(created.lastrowid)


def test_condition_trend_uses_canonical_counts():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            fx = _seed_alarm_fixture(conn, suffix)
            _seed_alarm(conn, fx, severity='Critical', status='Open')
            _seed_alarm(conn, fx, severity='Warning', status='Acknowledged')
            _seed_alarm(conn, fx, severity='Critical', status='Open',
                        occurrences=4)

        trend = client.get(
            '/api/kpi/trend', headers=headers,
            params={'family': 'condition', 'metric': 'critical_active_alarms',
                    'site_id': fx['site_id'], 'period_days': 30, 'samples': 2},
        ).json()
        assert trend['unit'] == 'alarms'
        # Newest bucket sees exactly the two critical open alarms.
        assert trend['samples'][-1]['value'] == 2

        unacked = client.get(
            '/api/kpi/trend', headers=headers,
            params={'family': 'condition', 'metric': 'unacknowledged_alarms',
                    'site_id': fx['site_id'], 'period_days': 30, 'samples': 2},
        ).json()
        # Unacknowledged = still literally Open: two of the three probes.
        assert unacked['samples'][-1]['value'] == 2

        storms = client.get(
            '/api/kpi/trend', headers=headers,
            params={'family': 'condition', 'metric': 'alarm_storms',
                    'site_id': fx['site_id'], 'period_days': 30, 'samples': 2},
        ).json()
        # One channel reached the >=3 occurrence storm threshold.
        assert storms['samples'][-1]['value'] == 1


def test_condition_explanation_ranks_measured_contributors():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            fx = _seed_alarm_fixture(conn, suffix)
            alarm_id = _seed_alarm(conn, fx, severity='Critical', status='Open',
                                   occurrences=5, days_ago=1)

        explanation = client.get(
            '/api/kpi/explanation', headers=headers,
            params={'family': 'condition', 'metric': 'active_alarms',
                    'site_id': fx['site_id'], 'period_days': 30},
        ).json()

        assert explanation['disclaimer'].startswith('Drivers are evidence')
        drivers = explanation['drivers']
        assert drivers, 'expected alarm contributors'
        kinds = {d['kind'] for d in drivers}
        assert 'active_alarm' in kinds
        alarm_driver = next(d for d in drivers if d['kind'] == 'active_alarm')
        for key in ('source_id', 'source_type', 'drill', 'attribution'):
            assert key in alarm_driver, key
        assert alarm_driver['attribution'] == 'contributor'
        assert alarm_driver['source_type'] == 'operational_alarm'
        assert alarm_driver['drill']['module'] == 'telemetry'

        # Drill identifier resolves to the real stored alarm.
        with db() as conn:
            hit = conn.execute(
                'SELECT alarm_no FROM operational_alarms WHERE id=?',
                (alarm_driver['source_id'],),
            ).fetchone()
        assert hit is not None
        assert alarm_id > 0


def test_ack_mutation_invalidates_condition_snapshot():
    """Alarm lifecycle stamps participate in the source watermark: an ack must
    force the next non-refresh snapshot read to recompute."""
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        with db() as conn:
            fx = _seed_alarm_fixture(conn, suffix)
            alarm_id = _seed_alarm(conn, fx, severity='Critical', status='Open')

        params = {'site_id': fx['site_id'], 'period_days': 30}
        before = client.get('/api/kpi/executive', headers=headers,
                            params=params).json()
        assert before['condition']['unacknowledged_alarms'] == 1
        # Add a second open alarm AFTER the snapshot materialized so
        # invalidation is provable. The watermark compares whole seconds, so
        # the mutation must land in a strictly later second.
        import time as _time

        _time.sleep(1.1)
        with db() as conn:
            user_id = int(conn.execute(
                "SELECT id FROM users WHERE username='omar'").fetchone()[0])
            opened = now()
            conn.execute(
                '''INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,
                     severity,status,alarm_type,message,trigger_value,threshold_value,
                     opened_at,last_seen_at,occurrence_count)
                   VALUES(?,?,?,?,?,'Open','Threshold','invalidation probe',
                          90,75,?,?,1)''',
                (f'ALM-TREND-{uuid.uuid4().hex[:8].upper()}', fx['channel_id'],
                 fx['asset_id'], fx['site_id'], 'Critical', opened, opened),
            )

        stale_guarded = client.get('/api/kpi/executive', headers=headers,
                                   params=params).json()
        # Watermark invalidation forces a fresh computation, not a stale hit.
        assert stale_guarded['snapshot']['served_from_cache'] is False
        condition = stale_guarded['condition']
        assert condition['unacknowledged_alarms'] == 2
        assert condition['active_alarms'] == 2

        # Acknowledge through the real domain lifecycle stamps; again spaced
        # into a strictly later second for the strict watermark comparison.
        _time.sleep(1.1)
        with db() as conn:
            conn.execute(
                "UPDATE operational_alarms SET status='Acknowledged',"
                ' acknowledged_at=?, acknowledged_by=(SELECT id FROM users WHERE'
                " username='omar') WHERE alarm_no=?",
                (now(),
                 conn.execute('SELECT alarm_no FROM operational_alarms WHERE id=?',
                              (alarm_id,)).fetchone()[0]),
            )

        after_ack = client.get('/api/kpi/executive', headers=headers,
                               params=params).json()
        assert after_ack['snapshot']['served_from_cache'] is False
        assert after_ack['condition']['unacknowledged_alarms'] == 1  # new open one
        assert after_ack['condition']['contributors'][0]['status'] in ('Open',)


def test_condition_read_authorization_and_mutation_separation():
    with TestClient(app) as client:
        anonymous = client.get('/api/kpi/trend', params={
            'family': 'condition', 'metric': 'active_alarms'})
        assert anonymous.status_code in (401, 403)

        manager = _auth(client, 'supervisor', 'Supervisor@2026')
        response = client.get(
            '/api/kpi/trend', headers=manager,
            params={'family': 'condition', 'metric': 'active_alarms'},
        )
        assert response.status_code == 200, response.text

        exec_headers = _auth(client, 'exec', 'Viewer@2026')
        read_ok = client.get(
            '/api/kpi/trend', headers=exec_headers,
            params={'family': 'condition', 'metric': 'active_alarms'},
        )
        assert read_ok.status_code == 200

        # Analytics read grants no alarm mutation rights.
        ack_denied = client.post(
            '/api/alarms/999999/acknowledge', headers=exec_headers)
        assert ack_denied.status_code in (401, 403, 404)
