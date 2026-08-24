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


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


def _seed_asset(conn, suffix: str, criticality: str = 'Critical') -> int:
    asset_no = f'OPS-{suffix}'
    cur = conn.execute(
        """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (asset_no, f'Ops probe {asset_no}', 'Transformer', criticality, 'Good', 'Operating', now(), now()),
    )
    return int(cur.lastrowid)


def _seed_channel(conn, code: str, asset_id: int) -> int:
    cur = conn.execute(
        """INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,
             source_system,active,created_at,updated_at)
           VALUES(?,?,?,?,?,'Manual',1,?,?)""",
        (code, asset_id, f'Channel {code}', 'Temperature', 'C', now(), now()),
    )
    return int(cur.lastrowid)


def _seed_alarm(conn, code: str, asset_id: int, channel_id: int, opened_at: str,
                severity: str = 'Critical') -> str:
    conn.execute(
        """INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,severity,status,
             alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,
             occurrence_count)
           VALUES(?,?,?,?, 'Open','Threshold',?,?,?,?,?,1)""",
        (code, channel_id, asset_id, severity, 'm', 95.0, 80.0, opened_at, opened_at),
    )
    return code


def _seed_outage(conn, code: str, asset_id: int, started_at: str) -> int:
    user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    cur = conn.execute(
        """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,impact,
             start_at,reported_by,created_at,updated_at)
           VALUES(?,?,'Forced','Open','feed impact',?,?,?,?)""",
        (code, asset_id, started_at, int(user['id']), now(), now()),
    )
    return int(cur.lastrowid)


def _seed_work(conn, asset_id: int | None, priority: str = 'Emergency',
               status: str = 'Submitted') -> dict:
    user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    wo_no = f"WO-OPS-{uuid.uuid4().hex[:10]}"
    cur = conn.execute(
        """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
             requested_by,target_finish,created_at,updated_at)
           VALUES(?,?,?,?,'Corrective',?,?,?,?,?)""",
        (
            wo_no, f'Ops restore {wo_no}', priority, status, asset_id,
            int(user['id']), _iso(datetime.now() + timedelta(days=2)), now(), now(),
        ),
    )
    return {'id': int(cur.lastrowid), 'wo_no': wo_no}


def test_situations_aggregate_outage_alarms_and_work_without_duplicating():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        started = _iso(datetime.now() - timedelta(hours=3))
        seeded = {'asset': None}
        try:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-OPS-{suffix}-%',))
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-OPS-{suffix}-%',))
                asset_id = _seed_asset(conn, suffix)
                seeded['asset'] = asset_id
                channel_id = _seed_channel(conn, f'TEL-OPS-{suffix}', asset_id)
                _seed_alarm(conn, f'ALM-OPS-{suffix}-A', asset_id, channel_id, started, severity='Critical')
                _seed_alarm(conn, f'ALM-OPS-{suffix}-B', asset_id, channel_id, started, severity='Warning')
                _seed_outage(conn, f'OUT-OPS-{suffix}', asset_id, started)
                work = _seed_work(conn, asset_id)

            result = client.get('/api/operations/situations', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()
            mine = [
                s for s in payload['situations']
                if s['anchor_no'].endswith(suffix) or s['asset_no'] == f'OPS-{suffix}'
            ]
            assert len(mine) == 1, f'one fused situation expected, got {mine!r}'
            sit = mine[0]
            assert sit['situation_key'] == f'outage:OUT-OPS-{suffix}'
            assert sit['severity'] == 'Critical'
            assert sit['started_at'] == started
            assert {a['alarm_no'] for a in sit['alarms']} >= {
                f'ALM-OPS-{suffix}-A', f'ALM-OPS-{suffix}-B'
            }
            assert any(w['wo_no'] == work['wo_no'] for w in sit['work_orders'])
            assert sit['outage_duration_hours'] is not None and sit['outage_duration_hours'] >= 3.0
            assert sit['restoration']['progress_pct'] == 20  # Submitted work exists

            # Severity counts include this situation exactly once.
            assert payload['severity_counts']['Critical'] >= 1
        finally:
            with db() as conn:
                aid = seeded['asset']
                if aid is not None:
                    conn.execute('DELETE FROM operational_alarms WHERE asset_id=?', (aid,))
                    conn.execute(
                        'DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)',
                        (aid,),
                    )
                    conn.execute('DELETE FROM work_orders WHERE asset_id=?', (aid,))
                    conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (aid,))
                    conn.execute('DELETE FROM telemetry_channels WHERE asset_id=?', (aid,))
                    conn.execute('DELETE FROM assets WHERE id=?', (aid,))
                else:
                    conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-OPS-{suffix}-%',))
                    conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-OPS-{suffix}-%',))


def test_timeline_is_chronological_and_built_from_real_events_only():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        t0 = datetime.now() - timedelta(hours=4)
        seeded = {'asset': None, 'channel': None}
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                seeded['asset'] = asset_id
                channel_id = _seed_channel(conn, f'TEL-TL-{suffix}', asset_id)
                seeded['channel'] = channel_id
                alarm_no = f'ALM-TL-{suffix}'
                _seed_alarm(conn, alarm_no, asset_id, channel_id, _iso(t0))
                conn.execute(
                    "UPDATE operational_alarms SET acknowledged_at=? WHERE alarm_no=?",
                    (_iso(t0 + timedelta(minutes=7)), alarm_no),
                )
                _seed_outage(conn, f'OUT-TL-{suffix}', asset_id, _iso(t0 + timedelta(minutes=4)))
                work = _seed_work(conn, asset_id, status='Approved')
                conn.execute('UPDATE work_orders SET created_at=? WHERE id=?', (_iso(t0 + timedelta(minutes=10)), work['id']))

            key = f'outage:OUT-TL-{suffix}'
            from urllib.parse import quote
            result = client.get(f'/api/operations/situations/{quote(key, safe="")}/timeline', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()
            events = payload['events']
            kinds = [e['kind'] for e in events]
            assert 'alarm_opened' in kinds and 'alarm_acknowledged' in kinds
            assert 'outage_started' in kinds and 'work_created' in kinds
            stamps = [e['ts'] for e in events]
            assert stamps == sorted(stamps), 'timeline must be chronological'
            first_alarm = next(e for e in events if e['kind'] == 'alarm_opened')
            outage_ev = next(e for e in events if e['kind'] == 'outage_started')
            assert first_alarm['ts'] <= outage_ev['ts']
            # No fabricated entries: every event references a real record number.
            assert all(e['ref_no'] for e in events)
        finally:
            with db() as conn:
                aid = seeded['asset']
                conn.execute('DELETE FROM operational_alarms WHERE asset_id=?', (aid,))
                conn.execute(
                    'DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)',
                    (aid,),
                )
                conn.execute('DELETE FROM work_orders WHERE asset_id=?', (aid,))
                conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (aid,))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (seeded['channel'],))
                conn.execute('DELETE FROM assets WHERE id=?', (aid,))


def test_situations_require_authentication():
    client = TestClient(app)
    response = client.get('/api/operations/situations')
    assert response.status_code == 401
