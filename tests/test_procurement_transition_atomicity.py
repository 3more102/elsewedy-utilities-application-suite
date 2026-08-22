from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app
from app.procurement_store import (
    ProcurementTransitionConflict,
    approve_requisition,
    receive_purchase_order,
)


WORKERS = 8


def _seed_admin_and_warehouse(conn):
    user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
    assert user and warehouse
    return int(user['id']), int(warehouse['id'])


def test_concurrent_requisition_approval_has_exactly_one_winner():
    suffix = uuid.uuid4().hex[:10]
    pr_no = f'PR-CAS-{suffix}'

    with TestClient(app):
        with db() as conn:
            actor_id, _ = _seed_admin_and_warehouse(conn)
            created = conn.execute(
                '''INSERT INTO purchase_requisitions(
                     pr_no,title,requester_id,status,justification,total_estimate,created_at
                   ) VALUES(?,?,?,'Submitted','concurrency regression',0,?)''',
                (pr_no, 'Concurrent approval regression', actor_id, now()),
            )
            pr_id = int(created.lastrowid)

        barrier = threading.Barrier(WORKERS)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    approve_requisition(conn, pr_id, actor_id)
                wins.append(index)
            except ProcurementTransitionConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 1
        assert len(conflicts) == WORKERS - 1

        with db() as conn:
            pr = conn.execute(
                'SELECT status,approved_at FROM purchase_requisitions WHERE id=?',
                (pr_id,),
            ).fetchone()
            transitions = int(
                conn.execute(
                    '''SELECT COUNT(*) FROM workflow_events
                       WHERE module='Procurement' AND record_type='purchase_requisition'
                         AND record_id=? AND event='APPROVE' ''',
                    (pr_id,),
                ).fetchone()[0]
            )
            audits = int(
                conn.execute(
                    '''SELECT COUNT(*) FROM audit_logs
                       WHERE module='Procurement' AND action='APPROVE' AND record_id=?''',
                    (pr_no,),
                ).fetchone()[0]
            )

        assert pr['status'] == 'Approved'
        assert pr['approved_at']
        assert transitions == 1
        assert audits == 1


def test_concurrent_purchase_order_receipt_posts_stock_once():
    suffix = uuid.uuid4().hex[:10]
    item_no = f'ITM-RCV-{suffix}'
    pr_no = f'PR-RCV-{suffix}'
    po_no = f'PO-RCV-{suffix}'
    quantity = 3.0
    starting_stock = 10.0

    with TestClient(app):
        with db() as conn:
            actor_id, warehouse_id = _seed_admin_and_warehouse(conn)
            item = conn.execute(
                '''INSERT INTO inventory_items(
                     item_no,name,category,warehouse_id,current_stock,reserved_stock,
                     min_level,max_level,reorder_point,unit_price,unit
                   ) VALUES(?,?,?,?,?,0,0,100,0,1,'ea')''',
                (item_no, 'Concurrent receipt item', 'CI', warehouse_id, starting_stock),
            )
            item_id = int(item.lastrowid)
            pr = conn.execute(
                '''INSERT INTO purchase_requisitions(
                     pr_no,title,requester_id,status,justification,total_estimate,created_at
                   ) VALUES(?,?,?,'Ordered','receipt concurrency regression',0,?)''',
                (pr_no, 'Concurrent receipt regression', actor_id, now()),
            )
            pr_id = int(pr.lastrowid)
            po = conn.execute(
                '''INSERT INTO purchase_orders(
                     po_no,pr_id,vendor_id,status,order_date,total_cost
                   ) VALUES(?,?,(SELECT id FROM vendors ORDER BY id LIMIT 1),'Ordered',?,0)''',
                (po_no, pr_id, now()[:10]),
            )
            po_id = int(po.lastrowid)
            conn.execute(
                '''INSERT INTO purchase_order_items(
                     po_id,inventory_item_id,description,quantity,unit_cost
                   ) VALUES(?,?,?,?,1)''',
                (po_id, item_id, 'Concurrent receipt item', quantity),
            )

        barrier = threading.Barrier(WORKERS)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    receive_purchase_order(conn, po_id, actor_id)
                wins.append(index)
            except ProcurementTransitionConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 1
        assert len(conflicts) == WORKERS - 1

        with db() as conn:
            stock = float(
                conn.execute(
                    'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
                ).fetchone()['current_stock']
            )
            po_status = conn.execute(
                'SELECT status FROM purchase_orders WHERE id=?', (po_id,)
            ).fetchone()['status']
            pr_status = conn.execute(
                'SELECT status FROM purchase_requisitions WHERE id=?', (pr_id,)
            ).fetchone()['status']
            receipts = int(
                conn.execute(
                    '''SELECT COUNT(*) FROM inventory_transactions
                       WHERE item_id=? AND tx_type='RECEIPT' AND reference=?''',
                    (item_id, po_no),
                ).fetchone()[0]
            )
            audits = int(
                conn.execute(
                    '''SELECT COUNT(*) FROM audit_logs
                       WHERE module='Procurement' AND action='RECEIVE' AND record_id=?''',
                    (po_no,),
                ).fetchone()[0]
            )

        assert stock == starting_stock + quantity
        assert po_status == 'Received'
        assert pr_status == 'Received'
        assert receipts == 1
        assert audits == 1
