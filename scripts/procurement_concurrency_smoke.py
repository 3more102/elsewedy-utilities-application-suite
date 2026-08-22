from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import ApprovalDecisionIn, POIn
from app.approval_store import ApprovalTransitionConflict, decide_approval_atomic
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.procurement_store import (
    ProcurementTransitionConflict,
    approve_requisition,
    create_purchase_order,
    receive_purchase_order,
)


WORKERS = 8


def run_race(operation):
    barrier = threading.Barrier(WORKERS)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            operation()
            wins.append(index)
        except ProcurementTransitionConflict:
            conflicts.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('procurement concurrency worker did not finish')
    if errors:
        raise RuntimeError(f'procurement concurrency worker failed: {errors!r}')
    if len(wins) != 1 or len(conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'expected one workflow winner and {WORKERS - 1} conflicts; '
            f'got wins={len(wins)} conflicts={len(conflicts)}'
        )


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    with db() as conn:
        ensure_audit_chain_lock(conn)
        user = conn.execute(
            """SELECT u.id,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        vendor = conn.execute('SELECT id FROM vendors ORDER BY id LIMIT 1').fetchone()
        warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
        if not user or not vendor or not warehouse:
            raise RuntimeError('procurement concurrency smoke requires seeded user/vendor/warehouse')
        actor = {'id': int(user['id']), 'role': user['role']}
        actor_id = actor['id']
        vendor_id = int(vendor['id'])
        warehouse_id = int(warehouse['id'])

    # Submitted -> Approved: one transition, one workflow event, one audit row.
    approve_no = f'PR-PG-APR-{suffix}'
    with db() as conn:
        created = conn.execute(
            '''INSERT INTO purchase_requisitions(
                 pr_no,title,requester_id,status,justification,total_estimate,created_at
               ) VALUES(?,?,?,'Submitted','postgres concurrency smoke',0,?)''',
            (approve_no, 'PostgreSQL concurrent approval', actor_id, now()),
        )
        approve_id = int(created.lastrowid)

    def approve_once() -> None:
        with db() as conn:
            approve_requisition(conn, approve_id, actor_id)

    run_race(approve_once)
    with db() as conn:
        status = conn.execute(
            'SELECT status FROM purchase_requisitions WHERE id=?', (approve_id,)
        ).fetchone()['status']
        events = int(
            conn.execute(
                '''SELECT COUNT(*) FROM workflow_events
                   WHERE module='Procurement' AND record_type='purchase_requisition'
                     AND record_id=? AND event='APPROVE' ''',
                (approve_id,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                '''SELECT COUNT(*) FROM audit_logs
                   WHERE module='Procurement' AND action='APPROVE' AND record_id=?''',
                (approve_no,),
            ).fetchone()[0]
        )
    if status != 'Approved' or events != 1 or audits != 1:
        raise RuntimeError(
            f'approval race violated invariants: status={status} events={events} audits={audits}'
        )

    # The direct procurement approval route and unified Approval Center must
    # contend on the same business row in the same lock order. This catches both
    # duplicate decisions and cross-route deadlocks.
    cross_no = f'PR-PG-XR-{suffix}'
    cross_approval_no = f'APR-PG-XR-{suffix}'
    with db() as conn:
        created = conn.execute(
            '''INSERT INTO purchase_requisitions(
                 pr_no,title,requester_id,status,justification,total_estimate,created_at
               ) VALUES(?,?,?,'Submitted','cross-route postgres race',0,?)''',
            (cross_no, 'PostgreSQL cross-route approval', actor_id, now()),
        )
        cross_pr_id = int(created.lastrowid)
        approval = conn.execute(
            '''INSERT INTO approval_requests(
                 approval_no,module,record_type,record_id,record_code,title,
                 requested_by,assigned_role,status,requested_at
               ) VALUES(?,?,?,?,?,?,?,'procurement','Pending',?)''',
            (
                cross_approval_no,
                'Procurement',
                'purchase_requisition',
                cross_pr_id,
                cross_no,
                f'Approve {cross_no}',
                actor_id,
                now(),
            ),
        )
        cross_approval_id = int(approval.lastrowid)

    cross_barrier = threading.Barrier(WORKERS)
    cross_wins: list[int] = []
    cross_conflicts: list[int] = []
    cross_errors: list[BaseException] = []
    decision_body = ApprovalDecisionIn(decision='approve', comments='postgres race')

    def cross_worker(index: int) -> None:
        try:
            cross_barrier.wait(timeout=15)
            with db() as conn:
                if index % 2:
                    decide_approval_atomic(
                        conn, cross_approval_id, decision_body, actor
                    )
                else:
                    approve_requisition(conn, cross_pr_id, actor_id)
            cross_wins.append(index)
        except (ProcurementTransitionConflict, ApprovalTransitionConflict):
            cross_conflicts.append(index)
        except BaseException as exc:
            cross_errors.append(exc)

    cross_threads = [
        threading.Thread(target=cross_worker, args=(i,)) for i in range(WORKERS)
    ]
    for thread in cross_threads:
        thread.start()
    for thread in cross_threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in cross_threads):
        raise RuntimeError('cross-route approval worker did not finish (possible deadlock)')
    if cross_errors:
        raise RuntimeError(f'cross-route approval race failed: {cross_errors!r}')
    if len(cross_wins) != 1 or len(cross_conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'cross-route approval expected one winner; wins={len(cross_wins)} '
            f'conflicts={len(cross_conflicts)}'
        )
    with db() as conn:
        cross_pr_status = conn.execute(
            'SELECT status FROM purchase_requisitions WHERE id=?', (cross_pr_id,)
        ).fetchone()['status']
        cross_approval_status = conn.execute(
            'SELECT status FROM approval_requests WHERE id=?', (cross_approval_id,)
        ).fetchone()['status']
        cross_events = int(
            conn.execute(
                '''SELECT COUNT(*) FROM workflow_events
                   WHERE module='Procurement' AND record_type='purchase_requisition'
                     AND record_id=? AND event='APPROVE' ''',
                (cross_pr_id,),
            ).fetchone()[0]
        )
    if (
        cross_pr_status != 'Approved'
        or cross_approval_status != 'Approved'
        or cross_events != 1
    ):
        raise RuntimeError(
            'cross-route approval state mismatch: '
            f'pr={cross_pr_status} approval={cross_approval_status} events={cross_events}'
        )

    # Approved -> Ordered: only one request may create a PO from one PR.
    order_no = f'PR-PG-PO-{suffix}'
    with db() as conn:
        created = conn.execute(
            '''INSERT INTO purchase_requisitions(
                 pr_no,title,requester_id,status,justification,total_estimate,created_at
               ) VALUES(?,?,?,'Approved','postgres PO concurrency smoke',25,?)''',
            (order_no, 'PostgreSQL concurrent PO creation', actor_id, now()),
        )
        order_pr_id = int(created.lastrowid)
    body = POIn(pr_id=order_pr_id, vendor_id=vendor_id)

    def create_po_once() -> None:
        with db() as conn:
            create_purchase_order(conn, body, actor_id)

    run_race(create_po_once)
    with db() as conn:
        pr_status = conn.execute(
            'SELECT status FROM purchase_requisitions WHERE id=?', (order_pr_id,)
        ).fetchone()['status']
        po_rows = conn.execute(
            'SELECT id,po_no,status FROM purchase_orders WHERE pr_id=?', (order_pr_id,)
        ).fetchall()
    if pr_status != 'Ordered' or len(po_rows) != 1 or po_rows[0]['status'] != 'Ordered':
        raise RuntimeError(
            f'PO creation race violated invariants: pr_status={pr_status} po_rows={po_rows!r}'
        )

    # Ordered -> Received: only one receiver may increase inventory.
    receive_pr_no = f'PR-PG-RCV-{suffix}'
    receive_po_no = f'PO-PG-RCV-{suffix}'
    item_no = f'ITM-PG-RCV-{suffix}'
    starting_stock = 10.0
    quantity = 4.0
    with db() as conn:
        item = conn.execute(
            '''INSERT INTO inventory_items(
                 item_no,name,category,warehouse_id,current_stock,reserved_stock,
                 min_level,max_level,reorder_point,unit_price,unit
               ) VALUES(?,?,?,?,?,0,0,100,0,1,'ea')''',
            (item_no, 'PostgreSQL receipt race item', 'CI', warehouse_id, starting_stock),
        )
        item_id = int(item.lastrowid)
        pr = conn.execute(
            '''INSERT INTO purchase_requisitions(
                 pr_no,title,requester_id,status,justification,total_estimate,created_at
               ) VALUES(?,?,?,'Ordered','postgres receipt concurrency smoke',0,?)''',
            (receive_pr_no, 'PostgreSQL concurrent receipt', actor_id, now()),
        )
        receive_pr_id = int(pr.lastrowid)
        po = conn.execute(
            '''INSERT INTO purchase_orders(
                 po_no,pr_id,vendor_id,status,order_date,total_cost
               ) VALUES(?,?,?,'Ordered',?,0)''',
            (receive_po_no, receive_pr_id, vendor_id, now()[:10]),
        )
        receive_po_id = int(po.lastrowid)
        conn.execute(
            '''INSERT INTO purchase_order_items(
                 po_id,inventory_item_id,description,quantity,unit_cost
               ) VALUES(?,?,?,?,1)''',
            (receive_po_id, item_id, 'PostgreSQL receipt race item', quantity),
        )

    def receive_once() -> None:
        with db() as conn:
            receive_purchase_order(conn, receive_po_id, actor_id)

    run_race(receive_once)
    with db() as conn:
        stock = float(
            conn.execute(
                'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
            ).fetchone()['current_stock']
        )
        po_status = conn.execute(
            'SELECT status FROM purchase_orders WHERE id=?', (receive_po_id,)
        ).fetchone()['status']
        pr_status = conn.execute(
            'SELECT status FROM purchase_requisitions WHERE id=?', (receive_pr_id,)
        ).fetchone()['status']
        receipts = int(
            conn.execute(
                '''SELECT COUNT(*) FROM inventory_transactions
                   WHERE item_id=? AND tx_type='RECEIPT' AND reference=?''',
                (item_id, receive_po_no),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                '''SELECT COUNT(*) FROM audit_logs
                   WHERE module='Procurement' AND action='RECEIVE' AND record_id=?''',
                (receive_po_no,),
            ).fetchone()[0]
        )
    if stock != starting_stock + quantity:
        raise RuntimeError(f'double receipt changed stock to {stock}')
    if po_status != 'Received' or pr_status != 'Received':
        raise RuntimeError(
            f'receipt states inconsistent: po={po_status} pr={pr_status}'
        )
    if receipts != 1 or audits != 1:
        raise RuntimeError(
            f'receipt race duplicated side effects: receipts={receipts} audits={audits}'
        )

    print(
        'procurement concurrency smoke: PASS '
        f'workers={WORKERS} approval=1 cross_route=1 po_creation=1 receipt=1'
    )


if __name__ == '__main__':
    main()
