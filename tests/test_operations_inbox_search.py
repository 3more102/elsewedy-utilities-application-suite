from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _login(client, username, password):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


def test_inbox_is_role_aware_and_flood_safe():
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        admin = _login(client, 'omar', 'EUAS@2026')
        tech = _login(client, 'tech1', 'Tech@2026')

        wo_id = None
        try:
            with db() as conn:
                tech_row = conn.execute(
                    "SELECT id FROM users WHERE username='tech1'"
                ).fetchone()
                asset = conn.execute('SELECT id FROM assets LIMIT 1').fetchone()
                cur = conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                         asset_id,requested_by,assigned_to,target_finish,created_at,updated_at)
                       VALUES(?,?,'Emergency','Approved','Corrective',?,?,?,?,?,?)""",
                    (f'WO-INBX-{suffix}', f'inbox probe {suffix}', int(asset['id']),
                     int(tech_row['id']), int(tech_row['id']),
                     _iso(datetime.now() - timedelta(days=2))[:10], now(), now()),
                )
                wo_id = int(cur.lastrowid)
                # Insert an unacknowledged critical alarm on a seeded channel.
                channel = conn.execute('SELECT id,asset_id FROM telemetry_channels LIMIT 1').fetchone()
                conn.execute(
                    """INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,severity,
                         status,alarm_type,message,trigger_value,threshold_value,opened_at,
                         last_seen_at,occurrence_count)
                       VALUES(?,?,?,'Critical','Open','Threshold','inbox probe',95,80,?,?,1)""",
                    (f'ALM-INBX-{suffix}', int(channel['id']), int(channel['asset_id']),
                     _iso(datetime.now() - timedelta(hours=1)),
                     _iso(datetime.now() - timedelta(hours=1))),
                )

            inbox = client.get('/api/operations/inbox', headers=tech).json()
            assigned = inbox['my_actions'].get('assigned_work') or []
            assert any(w['wo_no'] == f'WO-INBX-{suffix}' for w in assigned), (
                'technician must see their own assigned emergency work'
            )
            assert all(len(v) <= 20 for v in inbox['my_actions'].values())
            assert all(len(v) <= 20 for v in inbox['my_risks'].values())

            admin_inbox = client.get('/api/operations/inbox', headers=admin).json()
            acks = admin_inbox['my_actions'].get('alarms_to_acknowledge') or []
            assert any(a['alarm_no'] == f'ALM-INBX-{suffix}' for a in acks), (
                'admin is an alarm-owning role and must see the unacknowledged critical alarm'
            )
            # The technician role does not own alarms.
            tech_acks = (inbox['my_actions'].get('alarms_to_acknowledge') or [])
            assert tech_acks == []
        finally:
            with db() as conn:
                if wo_id:
                    conn.execute('DELETE FROM work_order_sla WHERE work_order_id=?', (wo_id,))
                    conn.execute('DELETE FROM work_orders WHERE id=?', (wo_id,))
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-INBX-{suffix}%',))


def test_command_search_finds_entities_and_requires_authentication():
    client = TestClient(app)
    assert client.get('/api/command-search?q=x').status_code == 401

    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                cur = conn.execute(
                    """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                         created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (f'SRCH-{suffix}', f'search probe {suffix}', 'Transformer',
                     'High', 'Good', 'Operating', now(), now()),
                )
                asset_id = int(cur.lastrowid)
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,created_at,updated_at)
                       VALUES(?,?, 'High','Approved','Corrective',?,?,?,?)""",
                    (f'WO-SRCH-{suffix}', f'distinctive-{suffix}', asset_id,
                     int(user['id']), now(), now()),
                )

            empty = client.get('/api/command-search?q=', headers=headers).json()
            assert empty == {'query': '', 'results': {}, 'total': 0}

            found = client.get(f'/api/command-search?q={suffix}', headers=headers).json()
            kinds = set(found['results'].keys())
            assert 'asset' in kinds and 'work_order' in kinds
            assert any(r['no'] == f'SRCH-{suffix}' for r in found['results']['asset'])
            assert any(r['title'] == f'distinctive-{suffix}'
                       for r in found['results']['work_order'])
            # Every result carries a stable entity reference for drill-downs.
            for kind, rows in found['results'].items():
                for row in rows:
                    assert row['entity_type'] == kind and row['entity_id']

            people = client.get('/api/command-search?q=Mariam', headers=headers).json()
            assert any(r['no'] == 'planner' for r in people['results'].get('employee', []))
        finally:
            with db() as conn:
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-SRCH-{suffix}%',))
                conn.execute('DELETE FROM assets WHERE asset_no LIKE ?', (f'SRCH-{suffix}%',))
