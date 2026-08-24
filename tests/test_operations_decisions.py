from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client):
    r = client.post('/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


def _seed_asset(conn, suffix: str, criticality: str = 'Critical') -> int:
    cur = conn.execute(
        """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
        (f'OPSX-{suffix}', f'Decision probe {suffix}', 'Transformer', criticality,
         'Good', 'Operating', now(), now()),
    )
    return int(cur.lastrowid)


def _seed_channel(conn, code: str, asset_id: int) -> int:
    cur = conn.execute(
        """INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,
             source_system,active,created_at,updated_at) VALUES(?,?,?,?,?,'Manual',1,?,?)""",
        (code, asset_id, f'Channel {code}', 'Temperature', 'C', now(), now()),
    )
    return int(cur.lastrowid)


def _cleanup_assets(conn, like: str) -> None:
    ids = [int(r['id']) for r in conn.execute(
        'SELECT id FROM assets WHERE asset_no LIKE ?', (like,)
    ).fetchall()]
    for aid in ids:
        conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM dispatch_assignments WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM workflow_events WHERE module=\'Work Management\' AND record_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute("DELETE FROM approval_requests WHERE module='Work Management' AND record_id IN (SELECT id FROM work_orders WHERE asset_id=?)", (aid,))
        conn.execute('DELETE FROM operational_alarms WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM telemetry_readings WHERE channel_id IN (SELECT id FROM telemetry_channels WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM telemetry_channels WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM work_orders WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM maintenance_plans WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM assets WHERE id=?', (aid,))


def test_why_red_open_outages_shares_sum_to_total():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        started = _iso(datetime.now() - timedelta(hours=2))
        try:
            with db() as conn:
                a1 = _seed_asset(conn, suffix + 'a')
                a2 = _seed_asset(conn, suffix + 'b')
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                for i, aid in enumerate((a1, a2)):
                    conn.execute(
                        """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,
                             impact,start_at,reported_by,created_at,updated_at)
                           VALUES(?,?,'Forced','Open','x',?,?,?,?)""",
                        (f'OUT-WHY-{suffix}{i}', aid, started, int(user['id']), now(), now()),
                    )
            result = client.get('/api/operations/why-red?key=open_outages', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()
            mine = [c for c in payload['contributors'] if f'OUT-WHY-{suffix}' in c['detail'] or c['count'] == 1 and 'OPSX-' in c['label']]
            assert len(payload['contributors']) >= 2
            assert payload['total'] >= 2
            total_share = sum(c['share_pct'] for c in payload['contributors'])
            assert abs(total_share - 100.0) < 0.5
            target = next(c for c in payload['contributors'] if suffix + 'a' in c['label'])
            assert 'Forced outage open' in target['detail']
        finally:
            with db() as conn:
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-WHY-{suffix}%',))
                _cleanup_assets(conn, f'OPSX-{suffix}%')


def test_why_red_overdue_buckets_and_unknown_key():
    with TestClient(app) as client:
        headers = _auth(client)
        before = client.get('/api/operations/why-red?key=overdue_work_orders', headers=headers)
        assert before.status_code == 200, before.text
        before_payload = before.json()
        before_unassigned = next(
            (c['count'] for c in before_payload['contributors'] if c['label'] == 'unassigned'),
            0,
        )
        suffix = uuid.uuid4().hex[:8]
        past = _iso(datetime.now() - timedelta(days=3))
        wo_ids = []
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                cur = conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,target_finish,created_at,updated_at)
                       VALUES(?,?,?,'Approved','Corrective',?,?,?,?,?)""",
                    (f'WO-WHY-{suffix}', 'overdue probe', 'High', asset_id,
                     int(user['id']), past[:10], now(), now()),
                )
                wo_ids.append(int(cur.lastrowid))

            result = client.get('/api/operations/why-red?key=overdue_work_orders', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()
            # Evidence details are intentionally capped per cause to keep the
            # response bounded. Verify the stable aggregate contract instead
            # of depending on this probe landing in that display sample.
            unassigned = next(
                c for c in payload['contributors'] if c['label'] == 'unassigned'
            )
            assert payload['total'] == before_payload['total'] + 1
            assert unassigned['count'] == before_unassigned + 1

            missing = client.get('/api/operations/why-red?key=nope', headers=headers)
            assert missing.status_code == 404
        finally:
            with db() as conn:
                conn.execute(
                    "DELETE FROM work_orders WHERE wo_no LIKE ? OR id IN ({})".format(
                        ','.join('?' * len(wo_ids))
                    ) if wo_ids else 'DELETE FROM work_orders WHERE wo_no LIKE ?',
                    tuple([f'WO-WHY-{suffix}%'] + wo_ids),
                )
                _cleanup_assets(conn, f'OPSX-{suffix}%')


def test_recommendations_cover_alarm_material_repeat_and_pm_rules():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        past = _iso(datetime.now() - timedelta(days=120))
        try:
            with db() as conn:
                alarm_asset = _seed_asset(conn, suffix + 'r1')
                channel_id = _seed_channel(conn, f'TEL-RULE-{suffix}', alarm_asset)
                opened = _iso(datetime.now() - timedelta(hours=2))
                conn.execute(
                    """INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,severity,
                         status,alarm_type,message,trigger_value,threshold_value,opened_at,
                         last_seen_at,occurrence_count)
                       VALUES(?,?,?,'Critical','Open','Threshold',?,95,80,?,?,1)""",
                    (f'ALM-RULE-{suffix}', channel_id, alarm_asset,
                     'unacked probe', opened, opened),
                )
                # Material-blocked emergency work.
                blocked_asset = _seed_asset(conn, suffix + 'r2')
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,target_finish,created_at,updated_at)
                       VALUES(?,?, 'Emergency','Approved','Corrective',?,?,?,?,?)""",
                    (f'WO-RULE-{suffix}', 'blocked restore', blocked_asset,
                     int(user['id']), _iso(datetime.now() + timedelta(days=1)), now(), now()),
                )
                wo_id = int(conn.execute(
                    "SELECT id FROM work_orders WHERE wo_no=?", (f'WO-RULE-{suffix}',)
                ).fetchone()['id'])
                warehouse = conn.execute('SELECT id FROM warehouses LIMIT 1').fetchone()
                item = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,0,0,0,10,?)""",
                    (f'RULE-ITM-{suffix}', 'rule spare', 'QA', 1.0, 'EA', int(warehouse['id'])),
                )
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,6,'Required')""",
                    (wo_id, int(item.lastrowid)),
                )
                # Repeat failures: two corrective completions inside 90 days.
                for tag in ('x1', 'x2'):
                    conn.execute(
                        """INSERT INTO work_orders(wo_no,title,priority,status,work_type,
                             asset_id,requested_by,actual_finish,created_at,updated_at)
                           VALUES(?,?, 'Medium','Completed','Corrective Maintenance',?,?,?,?,?)""",
                        (f'WO-RPT-{suffix}{tag}', f'repeat probe {tag}', alarm_asset,
                         int(user['id']), _iso(datetime.now() - timedelta(days=10 * (int(tag[-1]) + 1))),
                         now(), now()),
                    )
                # Overdue PM on a critical asset.
                conn.execute(
                    """INSERT INTO maintenance_plans(pm_no,name,asset_id,trigger_type,
                         interval_days,next_due,priority,active)
                       VALUES(?,?,?,'Calendar',30,?,'High',1)""",
                    (f'PM-RULE-{suffix}', 'rule pm', alarm_asset,
                     (_iso(datetime.now() - timedelta(days=5)))[:10]),
                )

            result = client.get('/api/operations/recommendations', headers=headers)
            assert result.status_code == 200, result.text
            recs = result.json()['recommendations']
            by_rule = {}
            for r in recs:
                by_rule.setdefault(r['rule_id'], []).append(r)

            alarm_rule = by_rule.get('critical-alarm-no-work') or []
            assert any(r['entity_no'] == f'ALM-RULE-{suffix}' for r in alarm_rule), 'R1 missing'
            assert all(r['evidence'] and r['recommended_action'] for group in by_rule.values() for r in group)

            material_rule = by_rule.get('critical-work-material-blocked') or []
            assert any(r['entity_no'] == f'WO-RULE-{suffix}' for r in material_rule), 'R2 missing'
            mr = next(r for r in material_rule if r['entity_no'] == f'WO-RULE-{suffix}')
            assert any(f'RULE-ITM-{suffix}' in e for e in mr['evidence'])

            repeat_rule = by_rule.get('repeat-failure-investigation') or []
            assert any(r['entity_no'] == f'OPSX-{suffix}r1' for r in repeat_rule), 'R3 missing'

            pm_rule = by_rule.get('critical-pm-overdue') or []
            assert any(r['entity_no'] == f'PM-RULE-{suffix}' for r in pm_rule), 'R4 missing'
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-RULE-%',))
                conn.execute('DELETE FROM cbm_recommendations WHERE asset_id IN (SELECT id FROM assets WHERE asset_no LIKE ?)', (f'OPSX-{suffix}%',))
                conn.execute('DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-RULE-{suffix}%',))
                conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-RULE-{suffix}%',))
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-RULE-{suffix}%',))
                conn.execute('DELETE FROM inventory_items WHERE item_no LIKE ?', (f'RULE-ITM-{suffix}%',))
                conn.execute('DELETE FROM maintenance_plans WHERE pm_no LIKE ?', (f'PM-RULE-{suffix}%',))
                _cleanup_assets(conn, f'OPSX-{suffix}%')


def test_blocker_chain_follows_real_relationships():
    with TestClient(app) as client:
        headers = _auth(client)
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,target_finish,created_at,updated_at)
                       VALUES(?,?, 'Emergency','Approved','Corrective',?,?,?,?,?)"""
                    .replace(', ?,', ', ?,'),
                    (f'WO-CHAIN-{suffix}', 'chain probe', asset_id,
                     int(user['id']), _iso(datetime.now() + timedelta(days=1)), now(), now()),
                )
                wo = conn.execute(
                    'SELECT id,wo_no FROM work_orders WHERE wo_no=?', (f'WO-CHAIN-{suffix}',),
                ).fetchone()
                warehouse = conn.execute('SELECT id FROM warehouses LIMIT 1').fetchone()
                vendor = conn.execute('SELECT id FROM vendors LIMIT 1').fetchone()
                pr_cur = conn.execute(
                    """INSERT INTO purchase_requisitions(pr_no,title,requester_id,status,
                         created_at) VALUES(?,?,?,'Submitted',?)""",
                    (f'PR-CHAIN-{suffix}', 'chain spare', int(user['id']), now()),
                )
                po_cur = conn.execute(
                    """INSERT INTO purchase_orders(po_no,vendor_id,status,order_date,total_cost)
                       VALUES(?,?, 'Ordered',?,100.0)""",
                    (f'PO-CHAIN-{suffix}', int(vendor['id']), now()),
                )
                eta = _iso(datetime.now() + timedelta(days=6))[:10]
                conn.execute(
                    'UPDATE purchase_orders SET expected_delivery=?,pr_id=? WHERE id=?',
                    (eta, int(pr_cur.lastrowid), int(po_cur.lastrowid)),
                )
                item = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,0,0,0,10,?)""",
                    (f'CHAIN-ITM-{suffix}', 'chain spare part', 'QA', 2.0, 'EA', int(warehouse['id'])),
                )
                conn.execute(
                    """INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,
                         description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)""",
                    (int(pr_cur.lastrowid), int(item.lastrowid), 'spare', 4, 2.0),
                )
                conn.execute(
                    '''UPDATE purchase_orders SET pr_id=? WHERE id=?''',
                    (int(pr_cur.lastrowid), int(po_cur.lastrowid)),
                )
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,9,'Required')""",
                    (int(wo['id']), int(item.lastrowid)),
                )
                wo_pk = int(wo['id'])

            result = client.get(f'/api/operations/blocker-chain/{wo_pk}', headers=headers)
            assert result.status_code == 200, result.text
            payload = result.json()
            assert payload['work_order']['wo_no'] == f'WO-CHAIN-{suffix}'
            stages = {s['stage']: s for s in payload['stages']}
            materials = stages['materials']
            assert materials['state'] == 'blocked'
            node = materials['nodes'][0]
            assert node['state'] == 'blocked'
            downstream = node['downstream']
            assert downstream['state'] == 'pending'
            assert f'PO-CHAIN-{suffix}' in downstream['detail']
            assert eta in downstream['detail']

            missing = client.get('/api/operations/blocker-chain/99999999', headers=headers)
            assert missing.status_code == 404
        finally:
            with db() as conn:
                conn.execute(
                    '''DELETE FROM purchase_requisition_items WHERE pr_id IN (
                         SELECT id FROM purchase_requisitions WHERE pr_no LIKE ?)''',
                    (f'PR-CHAIN-{suffix}%',),
                )
                conn.execute('UPDATE purchase_orders SET pr_id=NULL WHERE po_no LIKE ?', (f'PO-CHAIN-{suffix}%',))
                conn.execute('DELETE FROM purchase_requisitions WHERE pr_no LIKE ?', (f'PR-CHAIN-{suffix}%',))
                conn.execute('DELETE FROM purchase_orders WHERE po_no LIKE ?', (f'PO-CHAIN-{suffix}%',))
                conn.execute(
                    'DELETE FROM work_order_requirements WHERE inventory_item_id IN (SELECT id FROM inventory_items WHERE item_no LIKE ?)',
                    (f'CHAIN-ITM-{suffix}%',),
                )
                conn.execute('DELETE FROM inventory_items WHERE item_no LIKE ?', (f'CHAIN-ITM-{suffix}%',))
                _cleanup_assets(conn, f'OPSX-{suffix}%')
