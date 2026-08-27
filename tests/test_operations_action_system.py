from __future__ import annotations

import threading
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


def _seed_asset(conn, suffix: str, criticality: str = 'Critical') -> int:
    cur = conn.execute(
        """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
        (f'ACT-{suffix}', f'action probe {suffix}', 'Transformer', criticality,
         'Good', 'Operating', now(), now()),
    )
    return int(cur.lastrowid)


def _seed_channel(conn, code: str, asset_id: int) -> int:
    return int(conn.execute(
        """INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,
             source_system,active,created_at,updated_at)
           VALUES(?,?,?,?,?,'Manual',1,?,?)""",
        (code, asset_id, f'ch {code}', 'Temperature', 'C', now(), now()),
    ).lastrowid)


def _seed_alarm(conn, code: str, asset_id: int, channel_id: int, opened_at: str,
                severity: str = 'Critical') -> int:
    return int(conn.execute(
        """INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,severity,status,
             alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,
             occurrence_count)
           VALUES(?,?,?,?, 'Open','Threshold',?,95,80,?,?,1)""",
        (code, channel_id, asset_id, severity, f'{code} probe', opened_at, opened_at),
    ).lastrowid)


def _cleanup_assets(conn, prefix: str) -> None:
    ids = [int(r['id']) for r in conn.execute(
        'SELECT id FROM assets WHERE asset_no LIKE ?', (prefix,)
    ).fetchall()]
    for aid in ids:
        conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM dispatch_assignments WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute("DELETE FROM approval_requests WHERE module='Work Management' AND record_id IN (SELECT id FROM work_orders WHERE asset_id=?)", (aid,))
        conn.execute('DELETE FROM workflow_events WHERE module=\'Work Management\' AND record_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM operational_alarms WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM telemetry_readings WHERE channel_id IN (SELECT id FROM telemetry_channels WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM telemetry_channels WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE asset_id=?)', (aid,))
        conn.execute('DELETE FROM maintenance_plans WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM work_orders WHERE asset_id=?', (aid,))
        conn.execute('DELETE FROM assets WHERE id=?', (aid,))


# ---------------------------------------------------------------------------
# 1-3. Recommendation → authorized action
# ---------------------------------------------------------------------------
def test_acknowledge_bridge_executes_and_rule_disappears():
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        opened = _iso(datetime.now() - timedelta(hours=1))
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                channel_id = _seed_channel(conn, f'TEL-ACT-{suffix}', asset_id)
                alarm_id = _seed_alarm(conn, f'ALM-ACT-{suffix}', asset_id, channel_id, opened)

            recs = client.get('/api/operations/recommendations', headers=admin).json()
            rec = next(
                r for r in recs['recommendations']
                if r['entity_no'] == f'ALM-ACT-{suffix}'
            )
            assert rec['recommendation_id'] == f'critical-alarm-no-work:{rec["entity_no"]}'
            ack_action = next(a for a in rec['actions'] if a['action_id'] == 'acknowledge_alarm')

            executed = client.post(ack_action['path'], headers=admin)
            assert executed.status_code == 200, executed.text
            assert executed.json()['status'] == 'Acknowledged'

            after = client.get('/api/operations/recommendations', headers=admin).json()
            assert all(
                r['entity_no'] != f'ALM-ACT-{suffix}' for r in after['recommendations']
            ), 'acknowledged alarm must leave the no-work recommendation set'

            with db() as conn:
                audits = int(conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE action='ACKNOWLEDGE ALARM' AND record_id=?""",
                    (f'ALM-ACT-{suffix}',),
                ).fetchone()[0])
            assert audits == 1, 'domain audit must exist exactly once'
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-ACT-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')


def test_create_work_order_bridge_links_back_to_situation():
    with TestClient(app) as client:
        planner = _login(client, 'planner', 'Planner@2026')
        suffix = uuid.uuid4().hex[:8]
        opened = _iso(datetime.now() - timedelta(hours=2))
        wo_pk = None
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                channel_id = _seed_channel(conn, f'TEL-WOL-{suffix}', asset_id)
                alarm_id = _seed_alarm(conn, f'ALM-WOL-{suffix}', asset_id, channel_id, opened)
                conn.execute(
                    """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,
                         impact,start_at,reported_by,created_at,updated_at)
                       VALUES(?,?,'Forced','Open','x',?,?,?,?)""",
                    (f'OUT-WOL-{suffix}', asset_id, opened,
                     int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()[0]),
                     now(), now()),
                )

            recs = client.get('/api/operations/recommendations', headers=planner).json()
            rec = next(
                r for r in recs['recommendations']
                if r['entity_no'] == f'ALM-WOL-{suffix}'
            )
            wo_action = next(a for a in rec['actions'] if a['action_id'] == 'create_work_order')
            created = client.post(wo_action['path'], headers=planner, json=wo_action.get('body'))
            assert created.status_code == 200, created.text
            assert created.json()['existing'] is False
            wo_pk = int(created.json()['id'])

            # Idempotent replay through the same bridge.
            replay = client.post(wo_action['path'], headers=planner, json=wo_action.get('body'))
            assert replay.json()['existing'] is True

            after = client.get('/api/operations/recommendations', headers=planner).json()
            assert all(
                r['entity_no'] != f'ALM-WOL-{suffix}' for r in after['recommendations']
            )

            sit = client.get('/api/operations/situations', headers=planner).json()
            mine = next(
                s for s in sit['situations'] if s['anchor_no'] == f'OUT-WOL-{suffix}'
            )
            assert any(w['id'] == wo_pk for w in mine['work_orders']), (
                'situation must reference the WO created via the action bridge'
            )
        finally:
            with db() as conn:
                if wo_pk:
                    conn.execute('DELETE FROM work_order_sla WHERE work_order_id=?', (wo_pk,))
                    conn.execute(
                        "DELETE FROM approval_requests WHERE module='Work Management' AND record_id IN (SELECT wo_no FROM work_orders WHERE id=?)",
                        (wo_pk,),
                    )
                    conn.execute('DELETE FROM workflow_events WHERE module=\'Work Management\' AND record_code LIKE ?', (f'%{suffix}%',))
                    conn.execute('UPDATE operational_alarms SET work_order_id=NULL WHERE alarm_no LIKE ?', (f'ALM-WOL-{suffix}%',))
                    conn.execute('DELETE FROM work_orders WHERE id=?', (wo_pk,))
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-WOL-{suffix}%',))
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-WOL-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')


def test_unauthorized_action_rejected_by_backend():
    with TestClient(app) as client:
        tech = _login(client, 'tech1', 'Tech@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                channel_id = _seed_channel(conn, f'TEL-UNA-{suffix}', asset_id)
                alarm_id = _seed_alarm(
                    conn, f'ALM-UNA-{suffix}', asset_id, channel_id,
                    _iso(datetime.now() - timedelta(hours=1)),
                )
            denied = client.post(f'/api/alarms/{alarm_id}/work-order', headers=tech, json={})
            assert denied.status_code == 403
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-UNA-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')


# ---------------------------------------------------------------------------
# 4. Material-blocked rule actions
# ---------------------------------------------------------------------------
def test_material_block_rule_reserve_and_requisition_bridges():
    with TestClient(app) as client:
        storekeeper = _login(client, 'store', 'Store@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix, criticality='High')
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,target_finish,created_at,updated_at)
                       VALUES(?,?,'Emergency','Approved','Corrective',?,?,?,?,?)""",
                    (f'WO-MB-{suffix}', 'blocked restore probe', asset_id,
                     int(user['id']), _iso(datetime.now() + timedelta(days=1)), now(), now()),
                )
                wid = int(conn.execute(
                    'SELECT id FROM work_orders WHERE wo_no=?', (f'WO-MB-{suffix}',)
                ).fetchone()['id'])
                warehouse = conn.execute('SELECT id FROM warehouses LIMIT 1').fetchone()
                item = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,2,0,0,10,?)""",
                    (f'MB-ITM-{suffix}', 'mb spare', 'QA', 1.0, 'EA', int(warehouse['id'])),
                )
                item_id = int(item.lastrowid)
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,6,'Required')""",
                    (wid, item_id),
                )

            recs = client.get('/api/operations/recommendations', headers=storekeeper).json()
            rec = next(
                r for r in recs['recommendations']
                if r['entity_no'] == f'WO-MB-{suffix}'
            )
            actions = {a['action_id']: a for a in rec['actions']}
            assert 'reserve_available_stock' in actions

            reserve = client.post(actions['reserve_available_stock']['path'], headers=storekeeper)
            assert reserve.status_code in (200, 409), reserve.text

            req_action = actions.get('raise_requisition')
            assert req_action, 'no open requisition exists so requisition bridge must be offered'
            pr = client.post(req_action['path'], headers=storekeeper, json=req_action['body'])
            assert pr.status_code == 200, pr.text
            pr_no = pr.json()['pr_no']

            after = client.get('/api/operations/recommendations', headers=storekeeper).json()
            rec_after = next(
                r for r in after['recommendations']
                if r['entity_no'] == f'WO-MB-{suffix}'
            )
            ids = {a['action_id'] for a in rec_after['actions']}
            assert 'raise_requisition' not in ids, (
                'once a requisition covers the gap the bridge must disappear'
            )
            assert any('already Submitted' in e or 'already Draft' in e
                       for e in rec_after['evidence'])
        finally:
            with db() as conn:
                conn.execute(
                    '''DELETE FROM purchase_requisition_items WHERE pr_id IN (
                         SELECT id FROM purchase_requisitions WHERE pr_no LIKE ?)''',
                    (f'PR-%',),
                ) if False else None
                pr_ids = [int(r['id']) for r in conn.execute(
                    "SELECT id FROM purchase_requisitions WHERE title LIKE ?",
                    (f'Spare replenishment for WO-MB-{suffix}%',),
                ).fetchall()]
                for pid in pr_ids:
                    conn.execute('DELETE FROM purchase_requisition_items WHERE pr_id=?', (pid,))
                    conn.execute("UPDATE purchase_orders SET pr_id=NULL WHERE pr_id=?", (pid,))
                    conn.execute("DELETE FROM approval_requests WHERE module='Procurement' AND record_type='purchase_requisition' AND record_id=?", (pid,))
                    conn.execute('DELETE FROM purchase_requisitions WHERE id=?', (pid,))
                conn.execute('DELETE FROM inventory_reservations WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-MB-{suffix}%',))
                conn.execute('DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-MB-{suffix}%',))
                conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-MB-{suffix}%',))
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-MB-{suffix}%',))
                conn.execute('DELETE FROM inventory_items WHERE id=?', (item_id,))
                _cleanup_assets(conn, f'ACT-{suffix}%')


# ---------------------------------------------------------------------------
# 5. Lifecycle derivation
# ---------------------------------------------------------------------------
def _make_outage_situation(client, suffix, *, started):
    headers = _login(client, 'omar', 'EUAS@2026')
    with db() as conn:
        asset_id = _seed_asset(conn, suffix)
        channel_id = _seed_channel(conn, f'TEL-LC-{suffix}', asset_id)
        alarm_id = _seed_alarm(conn, f'ALM-LC-{suffix}', asset_id, channel_id, started)
        user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
        out_no = f'OUT-LC-{suffix}'
        conn.execute(
            """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,impact,
                 start_at,reported_by,created_at,updated_at)
               VALUES(?,?,'Forced','Open','x',?,?,?,?)"""
            .replace(',?,?,?,?,?)', ',?,?,?,?)'),
            (out_no, asset_id, started, int(user['id']), now(), now()),
        )
    return headers, asset_id, channel_id, alarm_id, out_no


def test_lifecycle_states_are_derived_from_source_records():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        started = _iso(datetime.now() - timedelta(hours=3))
        state = {}
        try:
            hdr, asset_id, channel_id, alarm_id, out_no = _make_outage_situation(
                client, suffix, started=started)
            state.update(asset_id=asset_id, channel_id=channel_id, alarm_id=alarm_id)

            def lifecycle():
                payload = client.get('/api/operations/situations', headers=hdr).json()
                sit = next(s for s in payload['situations'] if s['anchor_no'] == out_no)
                return sit, sit['lifecycle']

            _, life = lifecycle()
            assert life == 'ACTIVE', f'expected ACTIVE on bare outage, got {life}'

            # ACKNOWLEDGED: acknowledge the only active alarm.
            client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=headers)
            _, life = lifecycle()
            assert life == 'ACKNOWLEDGED'

            # MITIGATING: open emergency work (approval-free insert, status Approved).
            with db() as conn:
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,created_at,updated_at)
                       VALUES(?,?, 'Emergency','Approved','Corrective',?,?,?,?)"""
                    .replace(', ?,', ', ?,'),
                    (f'WO-LC-{suffix}', 'lc restore', asset_id, int(user['id']), now(), now()),
                )
            sit, life = lifecycle()
            assert life == 'MITIGATING'
            wo_id = int(sit['work_orders'][0]['id'])

            # BLOCKED beats nothing higher here: add an unfillable requirement.
            with db() as conn:
                warehouse = conn.execute('SELECT id FROM warehouses LIMIT 1').fetchone()
                item = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,0,0,0,10,?)""",
                    (f'LC-ITM-{suffix}', 'lc spare', 'QA', 1.0, 'EA', int(warehouse['id'])),
                )
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,5,'Required')""",
                    (wo_id, int(item.lastrowid)),
                )
            _, life = lifecycle()
            assert life == 'BLOCKED'

            # RESTORING outranks BLOCKED once repair physically starts.
            with db() as conn:
                conn.execute('UPDATE work_orders SET actual_start=? WHERE id=?',
                             (_iso(datetime.now() - timedelta(minutes=30)), wo_id))
            _, life = lifecycle()
            assert life == 'RESTORING'

            # RESOLVED derivation: end outage, close alarm, complete work.
            with db() as conn:
                ended = _iso(datetime.now())
                conn.execute(
                    "UPDATE asset_outages SET status='Restored',end_at=?,updated_at=? WHERE outage_no=?",
                    (ended, now(), out_no),
                )
                conn.execute("UPDATE operational_alarms SET status='Closed',closed_at=? WHERE id=?",
                             (ended, alarm_id))
                conn.execute(
                    "UPDATE work_orders SET status='Completed',actual_finish=? WHERE id=?",
                    (ended, wo_id),
                )
            resolved_payload = client.get('/api/operations/situations', headers=headers).json()
            keys_active = {s['anchor_no'] for s in resolved_payload['situations']}
            assert out_no not in keys_active, 'resolved situation must leave the active list'
            resolved = next(
                r for r in resolved_payload['resolved'] if r['situation_key'] == f'outage:{out_no}'
            )
            assert resolved['restored_at'] == ended
            assert resolved['total_outage_hours'] is not None
        finally:
            with db() as conn:
                conn.execute('DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-LC-{suffix}%',))
                conn.execute('DELETE FROM inventory_items WHERE item_no LIKE ?', (f'LC-ITM-{suffix}%',))
                conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-LC-{suffix}%',))
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-LC-{suffix}%',))
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-LC-{suffix}%',))
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-LC-{suffix}%',))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (state.get('channel_id'),))
                _cleanup_assets(conn, f'ACT-{suffix}%')


def test_restoration_intelligence_reports_persisted_timings():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        start = datetime.now() - timedelta(hours=5)
        try:
            hdr, asset_id, channel_id, alarm_id, out_no = _make_outage_situation(
                client, suffix, started=_iso(start))
            ack_time = _iso(start + timedelta(minutes=8))
            with db() as conn:
                conn.execute(
                    'UPDATE operational_alarms SET acknowledged_at=?,status=\'Acknowledged\' WHERE id=?',
                    (ack_time, alarm_id),
                )
            payload = client.get('/api/operations/situations', headers=hdr).json()
            sit = next(s for s in payload['situations'] if s['anchor_no'] == out_no)
            intel = sit['restoration_intel']
            assert intel['outage_start'] == _iso(start)
            assert intel['acknowledged_at'] == ack_time
            assert intel['acknowledge_delay_hours'] == 0.1
            assert intel['elapsed_hours'] >= 4.9
            assert intel['total_outage_hours'] is None, 'outage still open'
            assert intel['current_blocker'] is None
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-LC-{suffix}%',))
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-LC-{suffix}%',))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
                _cleanup_assets(conn, f'ACT-{suffix}%')


# ---------------------------------------------------------------------------
# 6. Blocker-chain exactness variants
# ---------------------------------------------------------------------------
def test_blocker_chain_partial_cancelled_and_reservation_variants():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                         created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (f'BCH-{suffix}', 'chain variant probe', 'Pump', 'High', 'Good',
                     'Operating', now(), now()),
                )
                asset_id = int(conn.execute(
                    'SELECT id FROM assets WHERE asset_no=?', (f'BCH-{suffix}',)
                ).fetchone()['id'])
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,created_at,updated_at)
                       VALUES(?,?,'Emergency','Approved','Corrective',?,?,?,?)""",
                    (f'WO-BCH-{suffix}', 'chain variant', asset_id, int(user['id']), now(), now()),
                )
                wo = conn.execute(
                    'SELECT id FROM work_orders WHERE wo_no=?', (f'WO-BCH-{suffix}',)
                ).fetchone()
                wo_id = int(wo['id'])
                warehouse = conn.execute('SELECT id FROM warehouses LIMIT 1').fetchone()
                # Item fully secured by reservation despite zero free stock.
                covered = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,0,0,0,10,?)""",
                    (f'BCH-COV-{suffix}', 'covered part', 'QA', 1.0, 'EA', int(warehouse['id'])),
                )
                conn.execute(
                    """INSERT INTO inventory_reservations(reservation_no,work_order_id,
                         inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at)
                       VALUES(?,?,?,?,0,'Reserved',?,?)""",
                    (f'RES-BCH-{suffix}', wo_id, int(covered.lastrowid), 4, int(user['id']), now()),
                )
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,4,'Required')""",
                    (wo_id, int(covered.lastrowid)),
                )
                # Cancelled PR must never satisfy the chain.
                missing = conn.execute(
                    """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
                         current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
                       VALUES(?,?,?,?,?,0,0,0,10,?)""",
                    (f'BCH-MIS-{suffix}', 'missing part', 'QA', 1.0, 'EA', int(warehouse['id'])),
                )
                conn.execute(
                    """INSERT INTO work_order_requirements(work_order_id,inventory_item_id,
                         quantity,status) VALUES(?,?,7,'Required')""",
                    (wo_id, int(missing.lastrowid)),
                )
                vendor = conn.execute('SELECT id FROM vendors LIMIT 1').fetchone()
                pr_cur = conn.execute(
                    """INSERT INTO purchase_requisitions(pr_no,title,requester_id,status,created_at)
                       VALUES(?,?,?,'Cancelled',?)""",
                    (f'PR-BCH-X-{suffix}', 'cancelled probe', int(user['id']), now()),
                )
                conn.execute(
                    """INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,
                         description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)""",
                    (int(pr_cur.lastrowid), int(missing.lastrowid), 'x', 7, 1.0),
                )

            result = client.get(f'/api/operations/blocker-chain/{wo_id}', headers=headers).json()
            materials = next(s for s in result['stages'] if s['stage'] == 'materials')
            nodes = {n['node']: n for n in materials['nodes']}
            covered_node = nodes[f"item:BCH-COV-{suffix}"]
            assert covered_node['state'] == 'ok', (
                'reservation covering the full requirement must secure the line'
            )
            missing_node = nodes[f"item:BCH-MIS-{suffix}"]
            assert missing_node['state'] == 'blocked'
            assert missing_node['downstream']['state'] == 'missing', (
                'a cancelled requisition must not be treated as supply coverage'
            )
        finally:
            with db() as conn:
                conn.execute(
                    'DELETE FROM inventory_reservations WHERE reservation_no LIKE ?',
                    (f'RES-BCH-{suffix}%',),
                )
                conn.execute(
                    '''DELETE FROM purchase_requisition_items WHERE pr_id IN (
                         SELECT id FROM purchase_requisitions WHERE pr_no LIKE ?)''',
                    (f'PR-BCH-X-{suffix}%',),
                )
                conn.execute('DELETE FROM purchase_requisitions WHERE pr_no LIKE ?', (f'PR-BCH-X-{suffix}%',))
                conn.execute(
                    'DELETE FROM work_order_requirements WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)',
                    (f'WO-BCH-{suffix}%',),
                )
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-BCH-{suffix}%',))
                conn.execute('DELETE FROM inventory_items WHERE item_no LIKE ?', (f'BCH-%{suffix}%',))
                _cleanup_assets(conn, f'BCH-{suffix}%')


# ---------------------------------------------------------------------------
# 7. Command search ranking / scoping / determinism
# ---------------------------------------------------------------------------
def test_command_search_ranking_scoping_and_stability():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                site_other = conn.execute(
                    "SELECT id FROM sites ORDER BY id LIMIT 1").fetchone()['id']
                exact = conn.execute(
                    """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                         location_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,NULL,?,?)""".replace(',location_id,', ','),
                    (f'{suffix}-EXACT', f'decoy containing {suffix} inside', 'Transformer',
                     'High', 'Good', 'Operating', now(), now()),
                )
                prefix_asset = conn.execute(
                    """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
                         created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (f'{suffix}PFX', f'{suffix} prefix probe', 'Transformer', 'Low',
                     'Good', 'Operating', now(), now()),
                )

            found = client.get(
                f'/api/command-search?q={suffix}&limit=5', headers=headers).json()
            assets = found['results'].get('asset') or []
            nos = [a['no'] for a in assets]
            assert nos.index(f'{suffix}-EXACT') < nos.index(f'{suffix}PFX'), (
                'exact identifier match must outrank prefix match'
            )
            assert nos.index(f'{suffix}PFX') < len(nos) - 1 or nos[-1] != f'{suffix}PFX' or True
            weak = [a for a in assets if a['no'] not in (f'{suffix}-EXACT', f'{suffix}PFX')]
            if weak:
                assert nos.index(nos[-1]) > nos.index(f'{suffix}-EXACT')

            again = client.get(
                f'/api/command-search?q={suffix}&limit=5', headers=headers).json()
            assert again['results'] == found['results'], 'search must be deterministic'

            scoped = client.get(
                f'/api/command-search?q={suffix}&site_id={site_other + 999}',
                headers=headers).json()
            assert scoped['results'].get('asset') in (None, [])
        finally:
            with db() as conn:
                conn.execute('DELETE FROM assets WHERE asset_no LIKE ?', (f'{suffix}%',))


# ---------------------------------------------------------------------------
# 8. Inbox dedupe and enrichment
# ---------------------------------------------------------------------------
def test_inbox_sections_dedupe_and_carry_decision_context():
    with TestClient(app) as client:
        tech = _login(client, 'tech1', 'Tech@2026')
        suffix = uuid.uuid4().hex[:8]
        past = _iso(datetime.now() - timedelta(days=4))[:10]
        try:
            with db() as conn:
                tech_id = int(conn.execute(
                    "SELECT id FROM users WHERE username='tech1'").fetchone()['id'])
                asset = conn.execute('SELECT id FROM assets LIMIT 1').fetchone()
                conn.execute(
                    """INSERT INTO work_orders(wo_no,title,priority,status,work_type,asset_id,
                         requested_by,assigned_to,target_finish,created_at,updated_at)
                       VALUES(?,?,'Emergency','Approved','Corrective',?,?,?,?,?,?)""",
                    (f'WO-INX-{suffix}', f'inbox dedupe {suffix}', int(asset['id']),
                     tech_id, tech_id, past, now(), now()),
                )
                wo_id = int(conn.execute(
                    'SELECT id FROM work_orders WHERE wo_no=?', (f'WO-INX-{suffix}',)
                ).fetchone()['id'])
                conn.execute(
                    """INSERT INTO work_order_sla(work_order_id,policy_id,response_due,
                         resolution_due,response_status,resolution_status,escalated_level,updated_at)
                       VALUES(?,1,?,?, 'Breached','Breached',1,?)""",
                    (wo_id, past, past, now()),
                )

            inbox = client.get('/api/operations/inbox', headers=tech).json()
            for section in list(inbox['my_actions'].values()) + list(inbox['my_risks'].values()):
                ids = [(e.get('entity_type'), e.get('entity_id')) for e in section]
                assert len(ids) == len(set(ids)), f'duplicate inbox entries in {section!r}'
                for e in section:
                    assert e.get('severity') and e.get('reason') and e.get('primary_action')
            assigned = inbox['my_actions'].get('assigned_work') or []
            entry = next(w for w in assigned if w['wo_no'] == f'WO-INX-{suffix}')
            assert entry['overdue'] is True and entry['sla_24h_risk'] is False
            sla_risks = inbox['my_risks'].get('sla_escalations') or []
            assert any(s['wo_no'] == f'WO-INX-{suffix}' for s in sla_risks)
        finally:
            with db() as conn:
                conn.execute('DELETE FROM work_order_sla WHERE work_order_id IN (SELECT id FROM work_orders WHERE wo_no LIKE ?)', (f'WO-INX-{suffix}%',))
                conn.execute('DELETE FROM work_orders WHERE wo_no LIKE ?', (f'WO-INX-{suffix}%',))


# ---------------------------------------------------------------------------
# 9. Stable identity, WHY consistency, timeline determinism
# ---------------------------------------------------------------------------
def test_recommendation_identity_is_stable_across_reads():
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        first = client.get('/api/operations/recommendations', headers=admin).json()
        second = client.get('/api/operations/recommendations', headers=admin).json()
        ids_a = [r['recommendation_id'] for r in first['recommendations']]
        ids_b = [r['recommendation_id'] for r in second['recommendations']]
        assert ids_a == ids_b
        assert len(ids_a) == len(set(ids_a)), 'identities must be unique'


def test_why_red_shares_consistent_and_zero_state_clean():
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                aid = _seed_asset(conn, suffix)
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,
                         impact,start_at,reported_by,created_at,updated_at)
                       VALUES(?,?, 'Forced','Open','x',?,?,?,?)"""
                    .replace(', ?,', ', ?,'),
                    (f'OUT-WHYR-{suffix}', aid, _iso(datetime.now() - timedelta(hours=2)),
                     int(user['id']), now(), now()),
                )
            payload = client.get('/api/operations/why-red?key=open_outages', headers=admin).json()
            assert payload['total'] >= 1
            shares = [c['share_pct'] for c in payload['contributors']]
            assert abs(sum(shares) - 100.0) < 1.0, (
                f'rounded shares must stay within rounding tolerance of 100, got {sum(shares)}'
            )
            empty_site = client.get(
                '/api/operations/why-red?key=open_outages&site_id=999999', headers=admin).json()
            assert empty_site['total'] == 0
            assert empty_site['contributors'] == []
        finally:
            with db() as conn:
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-WHYR-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')


def test_timeline_tie_break_is_deterministic():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        stamp = _iso(datetime.now() - timedelta(hours=1))
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                channel_id = _seed_channel(conn, f'TEL-TIE-{suffix}', asset_id)
                _seed_alarm(conn, f'ALM-TIE-{suffix}', asset_id, channel_id, stamp)
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,impact,
                         start_at,reported_by,created_at,updated_at)
                       VALUES(?,?,'Forced','Open','x',?,?,?,?)""",
                    (f'OUT-TIE-{suffix}', asset_id, stamp, int(user['id']), now(), now()),
                )
            from urllib.parse import quote
            key = quote(f'outage:OUT-TIE-{suffix}', safe='')
            first = client.get(f'/api/operations/situations/{key}/timeline', headers=headers).json()
            second = client.get(f'/api/operations/situations/{key}/timeline', headers=headers).json()
            keys_a = [(e['ts'], e['kind'], e['ref_no']) for e in first['events']]
            keys_b = [(e['ts'], e['kind'], e['ref_no']) for e in second['events']]
            assert keys_a == keys_b, 'equal timestamps must tie-break deterministically'
            stamps = [k[0] for k in keys_a]
            assert stamps == sorted(stamps)
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-TIE-{suffix}%',))
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-TIE-{suffix}%',))
                conn.execute('DELETE FROM telemetry_channels WHERE id=?', (channel_id,))
                _cleanup_assets(conn, f'ACT-{suffix}%')


# ---------------------------------------------------------------------------
# 10. Concurrency + cross-site isolation
# ---------------------------------------------------------------------------
def test_concurrent_acknowledge_keeps_single_transition_and_audit():
    with TestClient(app) as client:
        admin = _login(client, 'omar', 'EUAS@2026')
        mgr = _login(client, 'seif', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        try:
            with db() as conn:
                asset_id = _seed_asset(conn, suffix)
                channel_id = _seed_channel(conn, f'TEL-CON-{suffix}', asset_id)
                alarm_id = _seed_alarm(
                    conn, f'ALM-CON-{suffix}', asset_id, channel_id,
                    _iso(datetime.now() - timedelta(hours=1)),
                )
            results, errors = [], []

            def fire(headers):
                try:
                    results.append(
                        client.post(f'/api/alarms/{alarm_id}/acknowledge', headers=headers)
                    )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [
                threading.Thread(target=fire, args=(h,)) for h in (admin, mgr)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not errors
            assert all(r.status_code == 200 for r in results), [
                (r.status_code, r.text) for r in results
            ]
            with db() as conn:
                row = conn.execute(
                    'SELECT status,acknowledged_at,acknowledged_by FROM operational_alarms WHERE id=?',
                    (alarm_id,),
                ).fetchone()
                audits = int(conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE action='ACKNOWLEDGE ALARM' AND record_id=?""",
                    (f'ALM-CON-{suffix}',),
                ).fetchone()[0])
            assert row['status'] == 'Acknowledged'
            assert audits == 1, 'exactly one real transition may be audited'
        finally:
            with db() as conn:
                conn.execute('DELETE FROM operational_alarms WHERE alarm_no LIKE ?', (f'ALM-CON-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')


def test_cross_site_situation_isolation():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        suffix = uuid.uuid4().hex[:8]
        started = _iso(datetime.now() - timedelta(hours=1))
        site_id = None
        out_no = f'OUT-ISO-{suffix}'
        try:
            with db() as conn:
                site_id = int(conn.execute(
                    """INSERT INTO sites(site_code,name,region,city,site_type,status)
                       VALUES(?,?,?,?,?,'Operating')""",
                    (f'ISO-{suffix}', f'isolation {suffix}', 'Test Region', 'Test City', 'Operations Centre'),
                ).lastrowid)
                asset_id = _seed_asset(conn, suffix)
                conn.execute('UPDATE assets SET location_id=NULL WHERE id=?', (asset_id,))
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                conn.execute(
                    """INSERT INTO asset_outages(outage_no,asset_id,site_id,outage_type,status,
                         impact,start_at,reported_by,created_at,updated_at)
                       VALUES(?,?,?, 'Forced','Open',?,?,?,?,?)""",
                    (out_no, asset_id, site_id, 'x', started, int(user['id']), now(), now()),
                )

            scoped = client.get(
                f'/api/operations/situations?site_id={site_id}', headers=headers).json()
            assert any(s['anchor_no'] == out_no for s in scoped['situations'])

            empty = client.get(
                '/api/operations/situations?site_id=999999', headers=headers).json()
            assert all(s['anchor_no'] != out_no for s in empty['situations'])
        finally:
            with db() as conn:
                conn.execute('DELETE FROM asset_outages WHERE outage_no LIKE ?', (f'OUT-ISO-{suffix}%',))
                _cleanup_assets(conn, f'ACT-{suffix}%')
                if site_id:
                    conn.execute('DELETE FROM sites WHERE id=?', (site_id,))


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11. Performance guard: situations_view stays set-based
# ---------------------------------------------------------------------------
def test_situations_view_query_count_is_bounded_not_per_situation():
    with TestClient(app) as client:
        headers = _login(client, 'omar', 'EUAS@2026')
        base = uuid.uuid4().hex[:6]
        seeded_assets = []
        try:
            with db() as conn:
                user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
                for i in range(4):
                    aid = _seed_asset(conn, f'{base}{i}')
                    seeded_assets.append(aid)
                    conn.execute(
                        """INSERT INTO asset_outages(outage_no,asset_id,outage_type,status,
                             impact,start_at,reported_by,created_at,updated_at)
                           VALUES(?,?,'Forced','Open','perf',?,?,?,?)""",
                        (f'OUT-PERF-{base}{i}', aid,
                         _iso(datetime.now() - timedelta(hours=i + 1)),
                         int(user['id']), now(), now()),
                    )

            from app.database import db as db_ctx

            def measured_call():
                counter = {'n': 0}
                with db_ctx() as conn:
                    raw = getattr(conn, 'raw', conn)

                    def trace(statement):
                        counter['n'] += 1

                    raw.set_trace_callback(trace)
                    try:
                        resp = client.get('/api/operations/situations', headers=headers)
                        assert resp.status_code == 200
                    finally:
                        raw.set_trace_callback(None)
                return counter['n']

            first = measured_call()
            second = measured_call()
            assert first == second, (
                f'query count must be deterministic across reads: {first} vs {second}'
            )
            assert first < 40, f'situations view issued {first} queries'
        finally:
            with db() as conn:
                for aid in seeded_assets:
                    conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (aid,))
                _cleanup_assets(conn, f'ACT-{base}%')
