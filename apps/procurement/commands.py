from __future__ import annotations

from datetime import date

from apps.approvals import create_approval, resolve_approval
from apps.audit import audit
from apps.events import emit_event, workflow_event
from apps.suppliers import SupplierError, supplier_for_procurement
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no

from .workflow import InvalidProcurementTransition, purchase_order_receive_target, requisition_target


class ProcurementCommandError(RuntimeError):
    status_code = 409


class ProcurementNotFound(ProcurementCommandError):
    status_code = 404


class ProcurementConflict(ProcurementCommandError):
    status_code = 409


def _begin_write(conn) -> None:
    # SQLite DEFERRED transactions can otherwise let two readers race into a
    # write-lock error. Acquire the writer slot before reading command state.
    if DB_BACKEND == 'sqlite' and not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def _row(conn, table: str, record_id: int, message: str) -> dict:
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()
    if not row:
        raise ProcurementNotFound(message)
    return dict(row)


def create_requisition(conn, data: dict, actor_id: int) -> dict:
    payload = dict(data)
    number = next_no(conn, 'purchase_requisitions', 'pr_no', 'PR-', 8001)
    items = list(payload.get('items') or [])
    total = sum(float(item.get('quantity', 0)) * float(item.get('estimated_unit_cost', 0)) for item in items)
    cur = conn.execute(
        '''INSERT INTO purchase_requisitions(
             pr_no,title,requester_id,site_id,work_order_id,project_id,status,justification,total_estimate,created_at
           ) VALUES(?,?,?,?,?,?,'Draft',?,?,?)''',
        (
            number, payload['title'], actor_id, payload.get('site_id'), payload.get('work_order_id'),
            payload.get('project_id'), payload.get('justification', ''), total, now(),
        ),
    )
    for item in items:
        conn.execute(
            '''INSERT INTO purchase_requisition_items(
                 pr_id,inventory_item_id,description,quantity,estimated_unit_cost
               ) VALUES(?,?,?,?,?)''',
            (
                cur.lastrowid, item.get('inventory_item_id'), item.get('description', 'Item'),
                item.get('quantity', 1), item.get('estimated_unit_cost', 0),
            ),
        )
    audit(conn, actor_id, 'CREATE', 'Procurement', number, '', payload)
    return {'id': cur.lastrowid, 'pr_no': number}


def submit_requisition(conn, requisition_id: int, actor_id: int) -> dict:
    _begin_write(conn)
    requisition = _row(conn, 'purchase_requisitions', requisition_id, 'PR not found')
    try:
        target = requisition_target(requisition['status'], 'submit')
    except InvalidProcurementTransition as exc:
        raise ProcurementConflict('Only Draft or Rejected requisitions can be submitted') from exc
    cur = conn.execute(
        "UPDATE purchase_requisitions SET status=? WHERE id=? AND status=?",
        (target, requisition_id, requisition['status']),
    )
    if cur.rowcount != 1:
        raise ProcurementConflict('Purchase requisition state changed concurrently; reload and retry')
    create_approval(
        conn, 'Procurement', 'purchase_requisition', requisition_id, requisition['pr_no'],
        f"Approve {requisition['pr_no']} — {requisition['title']}", actor_id, assigned_role='procurement',
    )
    workflow_event(
        conn, 'Procurement', 'purchase_requisition', requisition_id, requisition['pr_no'],
        'SUBMIT', requisition['status'], target, actor_id,
    )
    audit(conn, actor_id, 'SUBMIT', 'Procurement', requisition['pr_no'], requisition['status'], target)
    return {'ok': True, 'status': target}


def approve_requisition(conn, requisition_id: int, actor_id: int) -> dict:
    _begin_write(conn)
    requisition = _row(conn, 'purchase_requisitions', requisition_id, 'PR not found')
    try:
        target = requisition_target(requisition['status'], 'approve')
    except InvalidProcurementTransition as exc:
        raise ProcurementConflict('Purchase requisition must be Submitted before approval') from exc
    stamp = now()
    cur = conn.execute(
        "UPDATE purchase_requisitions SET status=?,approved_at=? WHERE id=? AND status=?",
        (target, stamp, requisition_id, requisition['status']),
    )
    if cur.rowcount != 1:
        raise ProcurementConflict('Purchase requisition state changed concurrently; approval was not applied')
    resolve_approval(conn, 'Procurement', 'purchase_requisition', requisition_id, 'approve', actor_id)
    workflow_event(
        conn, 'Procurement', 'purchase_requisition', requisition_id, requisition['pr_no'],
        'APPROVE', requisition['status'], target, actor_id,
    )
    audit(conn, actor_id, 'APPROVE', 'Procurement', requisition['pr_no'], requisition['status'], target)
    emit_event(
        conn, 'procurement.requisition.approved', 'purchase_requisition', requisition['pr_no'],
        {'requisition_id': requisition_id, 'pr_no': requisition['pr_no'], 'status': target},
    )
    return {'ok': True, 'status': target}


def create_purchase_order(conn, data: dict, actor_id: int) -> dict:
    _begin_write(conn)
    payload = dict(data)
    requisition = _row(conn, 'purchase_requisitions', int(payload['pr_id']), 'PR not found')
    try:
        supplier_for_procurement(conn, int(payload['vendor_id']))
    except SupplierError as exc:
        raise ProcurementConflict(str(exc)) from exc
    try:
        ordered_status = requisition_target(requisition['status'], 'order')
    except InvalidProcurementTransition as exc:
        raise ProcurementConflict('Purchase requisition must be approved first') from exc

    # Claim the Approved requisition before creating side effects. Only one concurrent
    # purchase-order command can change this row from Approved -> Ordered.
    claimed = conn.execute(
        "UPDATE purchase_requisitions SET status=? WHERE id=? AND status=?",
        (ordered_status, requisition['id'], requisition['status']),
    )
    if claimed.rowcount != 1:
        raise ProcurementConflict('Purchase requisition was already ordered or changed concurrently')

    number = next_no(conn, 'purchase_orders', 'po_no', 'PO-', 9001)
    cur = conn.execute(
        '''INSERT INTO purchase_orders(
             po_no,pr_id,vendor_id,status,order_date,expected_delivery,total_cost,work_order_id,project_id
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            number, requisition['id'], payload['vendor_id'], ordered_status, date.today().isoformat(),
            payload.get('expected_delivery'), requisition['total_estimate'], requisition.get('work_order_id'),
            requisition.get('project_id'),
        ),
    )
    items = conn.execute('SELECT * FROM purchase_requisition_items WHERE pr_id=?', (requisition['id'],)).fetchall()
    for raw in items:
        item = dict(raw)
        conn.execute(
            '''INSERT INTO purchase_order_items(po_id,inventory_item_id,description,quantity,unit_cost)
               VALUES(?,?,?,?,?)''',
            (cur.lastrowid, item['inventory_item_id'], item['description'], item['quantity'], item['estimated_unit_cost']),
        )
    audit(conn, actor_id, 'CREATE PO', 'Procurement', number, '', {'pr': requisition['pr_no']})
    emit_event(
        conn, 'procurement.purchase_order.created', 'purchase_order', number,
        {'purchase_order_id': cur.lastrowid, 'po_no': number, 'pr_no': requisition['pr_no']},
    )
    return {'id': cur.lastrowid, 'po_no': number}


def receive_purchase_order(conn, purchase_order_id: int, actor_id: int) -> dict:
    _begin_write(conn)
    purchase_order = _row(conn, 'purchase_orders', purchase_order_id, 'PO not found')
    try:
        received_status = purchase_order_receive_target(purchase_order['status'])
    except InvalidProcurementTransition as exc:
        raise ProcurementConflict(str(exc)) from exc

    # Claim the PO before increasing stock. A duplicate/concurrent receipt must not
    # execute inventory side effects twice.
    claimed = conn.execute(
        "UPDATE purchase_orders SET status=?,actual_receipt=? WHERE id=? AND status=?",
        (received_status, date.today().isoformat(), purchase_order_id, purchase_order['status']),
    )
    if claimed.rowcount != 1:
        raise ProcurementConflict('Purchase order was already received or changed concurrently')

    items = conn.execute('SELECT * FROM purchase_order_items WHERE po_id=?', (purchase_order_id,)).fetchall()
    for raw in items:
        item = dict(raw)
        if not item.get('inventory_item_id'):
            continue
        inventory = conn.execute('SELECT * FROM inventory_items WHERE id=?', (item['inventory_item_id'],)).fetchone()
        if not inventory:
            raise ProcurementNotFound('Inventory item referenced by purchase order was not found')
        conn.execute(
            'UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',
            (item['quantity'], item['inventory_item_id']),
        )
        conn.execute(
            '''INSERT INTO inventory_transactions(
                 item_id,tx_type,quantity,from_warehouse_id,reference,user_id,created_at
               ) VALUES(?,?,?,?,?,?,?)''',
            (
                item['inventory_item_id'], 'RECEIPT', item['quantity'], inventory['warehouse_id'],
                purchase_order['po_no'], actor_id, now(),
            ),
        )
    if purchase_order.get('pr_id'):
        conn.execute(
            "UPDATE purchase_requisitions SET status=? WHERE id=? AND status='Ordered'",
            (received_status, purchase_order['pr_id']),
        )
    audit(
        conn, actor_id, 'RECEIVE', 'Procurement', purchase_order['po_no'], purchase_order['status'], received_status,
    )
    emit_event(
        conn, 'procurement.purchase_order.received', 'purchase_order', purchase_order['po_no'],
        {'purchase_order_id': purchase_order_id, 'po_no': purchase_order['po_no'], 'status': received_status},
    )
    return {'ok': True, 'status': received_status}
