from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


class ProcurementTransitionConflict(RuntimeError):
    """Raised when a workflow state changed before this transaction claimed it."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _require_row(conn, sql: str, args: tuple, message: str):
    row = conn.execute(sql, args).fetchone()
    if not row:
        raise KeyError(message)
    return row


def submit_requisition(conn, pr_id: int, actor_id: int) -> dict:
    pr = _require_row(
        conn,
        'SELECT * FROM purchase_requisitions WHERE id=?',
        (pr_id,),
        'PR not found',
    )
    claimed = conn.execute(
        """UPDATE purchase_requisitions SET status='Submitted'
           WHERE id=? AND status IN ('Draft','Rejected')""",
        (pr_id,),
    )
    if not _rowcount_one(claimed):
        raise ProcurementTransitionConflict(
            'Only Draft or Rejected requisitions can be submitted'
        )

    _application.create_approval(
        conn,
        'Procurement',
        'purchase_requisition',
        pr_id,
        pr['pr_no'],
        f"Approve {pr['pr_no']} — {pr['title']}",
        actor_id,
        assigned_role='procurement',
    )
    _application.workflow_event(
        conn,
        'Procurement',
        'purchase_requisition',
        pr_id,
        pr['pr_no'],
        'SUBMIT',
        pr['status'],
        'Submitted',
        actor_id,
    )
    append_audit(
        conn,
        actor_id,
        'SUBMIT',
        'Procurement',
        pr['pr_no'],
        pr['status'],
        'Submitted',
    )
    return {'ok': True, 'status': 'Submitted'}


def approve_requisition(conn, pr_id: int, actor_id: int) -> dict:
    pr = _require_row(
        conn,
        'SELECT * FROM purchase_requisitions WHERE id=?',
        (pr_id,),
        'PR not found',
    )
    claimed = conn.execute(
        """UPDATE purchase_requisitions
           SET status='Approved',approved_at=?
           WHERE id=? AND status='Submitted'""",
        (now(), pr_id),
    )
    if not _rowcount_one(claimed):
        raise ProcurementTransitionConflict(
            'Purchase requisition must be Submitted before approval'
        )

    _application.resolve_approval(
        conn,
        'Procurement',
        'purchase_requisition',
        pr_id,
        'approve',
        actor_id,
    )
    _application.workflow_event(
        conn,
        'Procurement',
        'purchase_requisition',
        pr_id,
        pr['pr_no'],
        'APPROVE',
        pr['status'],
        'Approved',
        actor_id,
    )
    append_audit(
        conn,
        actor_id,
        'APPROVE',
        'Procurement',
        pr['pr_no'],
        pr['status'],
        'Approved',
    )
    return {'ok': True, 'status': 'Approved'}


def create_purchase_order(conn, body, actor_id: int) -> dict:
    pr = _require_row(
        conn,
        'SELECT * FROM purchase_requisitions WHERE id=?',
        (body.pr_id,),
        'PR not found',
    )
    claimed = conn.execute(
        """UPDATE purchase_requisitions SET status='Ordered'
           WHERE id=? AND status='Approved'""",
        (body.pr_id,),
    )
    if not _rowcount_one(claimed):
        raise ProcurementTransitionConflict(
            'Purchase requisition must be approved first'
        )

    number = _application.next_no(
        conn, 'purchase_orders', 'po_no', 'PO-', 9001
    )
    created = conn.execute(
        '''INSERT INTO purchase_orders(
             po_no,pr_id,vendor_id,status,order_date,expected_delivery,
             total_cost,work_order_id,project_id
           ) VALUES(?,?,?,'Ordered',?,?,?,?,?)''',
        (
            number,
            body.pr_id,
            body.vendor_id,
            date.today().isoformat(),
            body.expected_delivery,
            pr['total_estimate'],
            pr['work_order_id'],
            pr['project_id'],
        ),
    )
    po_id = int(created.lastrowid)
    items = conn.execute(
        'SELECT * FROM purchase_requisition_items WHERE pr_id=?',
        (body.pr_id,),
    ).fetchall()
    for item in items:
        conn.execute(
            '''INSERT INTO purchase_order_items(
                 po_id,inventory_item_id,description,quantity,unit_cost
               ) VALUES(?,?,?,?,?)''',
            (
                po_id,
                item['inventory_item_id'],
                item['description'],
                item['quantity'],
                item['estimated_unit_cost'],
            ),
        )

    append_audit(
        conn,
        actor_id,
        'CREATE PO',
        'Procurement',
        number,
        '',
        {'pr': pr['pr_no']},
    )
    return {'id': po_id, 'po_no': number}


def receive_purchase_order(conn, po_id: int, actor_id: int) -> dict:
    po = _require_row(
        conn,
        'SELECT * FROM purchase_orders WHERE id=?',
        (po_id,),
        'PO not found',
    )

    # Claim the one legal receipt transition before any inventory side effects.
    # PostgreSQL serializes concurrent writers on this row; after the winner
    # commits, every stale receiver observes rowcount=0 and performs no stock
    # mutation. SQLite obtains equivalent single-writer behavior.
    claimed = conn.execute(
        """UPDATE purchase_orders
           SET status='Received',actual_receipt=?
           WHERE id=? AND status='Ordered'""",
        (date.today().isoformat(), po_id),
    )
    if not _rowcount_one(claimed):
        raise ProcurementTransitionConflict('PO already received')

    items = conn.execute(
        'SELECT * FROM purchase_order_items WHERE po_id=?', (po_id,)
    ).fetchall()
    for po_item in items:
        item_id = po_item['inventory_item_id']
        if not item_id:
            continue
        item = _require_row(
            conn,
            'SELECT * FROM inventory_items WHERE id=?',
            (item_id,),
            'Inventory item not found',
        )
        updated = conn.execute(
            '''UPDATE inventory_items
               SET current_stock=current_stock+?
               WHERE id=?''',
            (po_item['quantity'], item_id),
        )
        if not _rowcount_one(updated):
            raise KeyError('Inventory item not found')
        conn.execute(
            '''INSERT INTO inventory_transactions(
                 item_id,tx_type,quantity,from_warehouse_id,reference,user_id,created_at
               ) VALUES(?,?,?,?,?,?,?)''',
            (
                item_id,
                'RECEIPT',
                po_item['quantity'],
                item['warehouse_id'],
                po['po_no'],
                actor_id,
                now(),
            ),
        )

    if po['pr_id']:
        pr_updated = conn.execute(
            """UPDATE purchase_requisitions SET status='Received'
               WHERE id=? AND status='Ordered'""",
            (po['pr_id'],),
        )
        if not _rowcount_one(pr_updated):
            raise ProcurementTransitionConflict(
                'Purchase requisition is not in Ordered state'
            )

    append_audit(
        conn,
        actor_id,
        'RECEIVE',
        'Procurement',
        po['po_no'],
        po['status'],
        'Received',
    )
    return {'ok': True, 'status': 'Received'}


def install_procurement_routes() -> None:
    """Replace only procurement workflow routes that require atomic transitions."""
    app = _application.app
    marker = '_euas_procurement_transition_hardening'
    if getattr(app.state, marker, False):
        return

    replacements = {
        ('/api/procurement/requisitions/{pr_id}/submit', 'POST'),
        ('/api/procurement/requisitions/{pr_id}/approve', 'POST'),
        ('/api/procurement/purchase-orders', 'POST'),
        ('/api/procurement/purchase-orders/{po_id}/receive', 'POST'),
    }
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and method in set(getattr(route, 'methods', set()) or set())
            for path, method in replacements
        )
    ]

    @app.post('/api/procurement/requisitions/{pr_id}/submit')
    def submit_pr_atomic(
        pr_id: int,
        user=Depends(
            require_roles(
                'admin', 'storekeeper', 'maintenance_manager', 'procurement', 'planner'
            )
        ),
    ):
        try:
            with db() as conn:
                return submit_requisition(conn, pr_id, user['id'])
        except KeyError:
            raise HTTPException(404, 'PR not found')
        except ProcurementTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/procurement/requisitions/{pr_id}/approve')
    def approve_pr_atomic(
        pr_id: int,
        user=Depends(require_roles(*_application.PROC_ROLES)),
    ):
        try:
            with db() as conn:
                return approve_requisition(conn, pr_id, user['id'])
        except KeyError:
            raise HTTPException(404, 'PR not found')
        except ProcurementTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/procurement/purchase-orders')
    def create_po_atomic(
        body: _application.POIn,
        user=Depends(require_roles(*_application.PROC_ROLES)),
    ):
        try:
            with db() as conn:
                return create_purchase_order(conn, body, user['id'])
        except KeyError:
            raise HTTPException(404, 'PR not found')
        except ProcurementTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/procurement/purchase-orders/{po_id}/receive')
    def receive_po_atomic(
        po_id: int,
        user=Depends(require_roles('admin', 'procurement', 'storekeeper')),
    ):
        try:
            with db() as conn:
                return receive_purchase_order(conn, po_id, user['id'])
        except KeyError as exc:
            message = str(exc).strip("'")
            raise HTTPException(404, message)
        except ProcurementTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    app.openapi_schema = None
    setattr(app.state, marker, True)
