from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _iso(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _auth(client):
    r = client.post(
        '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
    )
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_asset(conn, criticality: str) -> int:
    user = admin(conn)
    asset_no = f'RISK-{uuid.uuid4().hex[:8]}'
    cur = conn.execute(
        """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            asset_no,
            f'Risk asset {asset_no}',
            'Transformer',
            criticality,
            'Poor',
            'Operating',
            now(),
            now(),
        ),
    )
    return int(cur.lastrowid)


def _seed_work(
    conn,
    asset_id: int | None,
    priority: str,
    *,
    days_open: float = 1.0,
    target_finish: str | None = None,
    safety: str = '',
    work_type: str = 'Corrective Maintenance',
) -> int:
    user = admin(conn)
    wo_no = f'WO-RISK-{uuid.uuid4().hex[:10]}'
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,asset_id,requested_by,
             target_finish,safety_requirements,created_at,updated_at)
           VALUES(?,?,?,'Approved',?,?,?,?,?,?,?)''',
        (
            wo_no,
            f'Risk ranking probe {wo_no}',
            priority,
            work_type,
            asset_id,
            user['id'],
            target_finish,
            safety,
            _iso(days_open),
            now(),
        ),
    )
    return int(cur.lastrowid)


def date_today_minus(days: int) -> str:
    from datetime import date as _date, timedelta as _td
    return (_date.today() - _td(days=days)).isoformat()


def test_backlog_ranks_evidence_weighted_work_above_routine_work():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            critical_asset = _seed_asset(conn, 'Critical')
            plain_asset = _seed_asset(conn, 'Low')
            # High-stakes job: emergency priority, overdue, old, unassigned.
            risky = _seed_work(
                conn,
                critical_asset,
                'Emergency',
                days_open=21.0,
                target_finish=date_today_minus(5),
                safety='LOTO required',
            )
            # Routine job: low priority, young, assigned to nobody but fresh.
            routine = _seed_work(
                conn,
                plain_asset,
                'Low',
                days_open=0.5,
            )
        response = client.get('/api/work-orders/backlog', headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        items = {item['id']: item for item in payload['items']}
        assert risky in items and routine in items
        risky_item = items[risky]
        routine_item = items[routine]
        assert risky_item['risk_score'] > routine_item['risk_score']
        assert risky_item['risk_band'] == 'HIGH RISK'
        assert routine_item['risk_band'] == 'ROUTINE'
        assert risky_item['factors'], 'ranking factors must be auditable'
        joined = ' | '.join(risky_item['factors'])
        assert 'Emergency' in joined or 'days open' in joined
        ids = [item['id'] for item in payload['items']]
        assert ids.index(risky) < ids.index(routine)


def test_backlog_flags_material_blocked_and_alarm_and_health_evidence():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            asset_id = _seed_asset(conn, 'High')
            channel = conn.execute(
                'SELECT id FROM telemetry_channels ORDER BY id LIMIT 1'
            ).fetchone()
            stamp = now()
            alarm_no = f'ALM-RISK-{uuid.uuid4().hex[:8]}'
            if channel:
                conn.execute(
                    """INSERT INTO operational_alarms(
                         alarm_no,channel_id,asset_id,severity,status,alarm_type,
                         message,trigger_value,opened_at,last_seen_at)
                       VALUES(?,?,?,?, 'Open','Threshold','risk backlog probe',90,?,?)""",
                    (alarm_no, int(channel['id']), asset_id, 'Critical', stamp, stamp),
                )

            conn.execute(
                """INSERT INTO asset_health_snapshots(
                     asset_id,score,risk_band,factors_json,calculated_at)
                   VALUES(?,?,?,?,'2026-08-01T00:00:00')""",
                (asset_id, 35.0, 'Poor', '{"probe":true}'),
            )
            warehouse = conn.execute(
                'SELECT id FROM warehouses ORDER BY id LIMIT 1'
            ).fetchone()
            assert warehouse
            item_no = f'RISK-ITM-{uuid.uuid4().hex[:8]}'
            item = conn.execute(
                """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                     current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                   VALUES(?,?,?,?,?,1,0,2,20,?)""",
                (item_no, 'Risk spare', 'QA', 5.0, 'EA', int(warehouse['id'])),
            )
            blocked_wo = _seed_work(conn, asset_id, 'Medium', days_open=2.0)
            conn.execute(
                """INSERT INTO work_order_requirements(
                     work_order_id,inventory_item_id,quantity,status)
                   VALUES(?,?,4,'Required')""",
                (blocked_wo, int(item.lastrowid)),
            )
        response = client.get('/api/work-orders/backlog', headers=headers)
        assert response.status_code == 200, response.text
        items = {i['id']: i for i in response.json()['items']}
        blocked = items[blocked_wo]
        assert blocked['material_blocked'] is True
        assert blocked['material_shortage_items'] >= 1
        assert any('MATERIAL BLOCKED' in f for f in blocked['factors'])
        evidenced = items[blocked_wo]
        assert evidenced['health_score'] is not None and evidenced['health_score'] <= 40
        if channel:
            assert evidenced['active_alarm_tier'] == 'Critical'


def test_backlog_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            seeded = [
                _seed_work(conn, None, 'Medium', days_open=float(i))
                for i in range(6)
            ]
        page_one = client.get(
            '/api/work-orders/backlog', headers=headers, params={'limit': 3}
        ).json()
        full_view = client.get('/api/work-orders/backlog', headers=headers).json()
        ids_one = [i['id'] for i in page_one['items']]
        assert len(ids_one) == 3
        assert set(seeded).issubset({i['id'] for i in full_view['items']})
        scores = [i['risk_score'] for i in full_view['items']]
        assert scores == sorted(scores, reverse=True)
        offset = 200
        while True:
            tail = client.get(
                '/api/work-orders/backlog',
                headers=headers,
                params={'limit': 200, 'offset': offset},
            ).json()
            assert not set(ids_one) & {i['id'] for i in tail['items']}
            if not tail['items']:
                break
            offset += 200
            if offset > 10000:  # pragma: no cover - defensive
                break


def test_backlog_excludes_terminal_work():
    with TestClient(app) as client:
        headers = _auth(client)
        with db() as conn:
            closed = _seed_work(conn, None, 'High', days_open=3.0)
            conn.execute(
                "UPDATE work_orders SET status='Closed' WHERE id=?", (closed,)
            )
        payload = client.get('/api/work-orders/backlog', headers=headers).json()
        assert closed not in {i['id'] for i in payload['items']}
