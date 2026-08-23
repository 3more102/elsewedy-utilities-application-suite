from __future__ import annotations

from threading import Barrier, Thread

from app.auth import hash_password
from apps.procurement import (
    ProcurementConflict,
    approve_requisition,
    create_purchase_order,
    create_requisition,
    receive_purchase_order,
    submit_requisition,
)
from core.database import db, init_db


def _admin_id(conn) -> int:
    return int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id'])


def _vendor_id(conn) -> int:
    return int(conn.execute("SELECT id FROM vendors ORDER BY id LIMIT 1").fetchone()['id'])


def _item(conn) -> dict:
    return dict(conn.execute('SELECT * FROM inventory_items ORDER BY id LIMIT 1').fetchone())


def _run_race(fn):
    barrier = Barrier(2)
    outcomes: list[str] = []
    errors: list[Exception] = []

    def runner():
        try:
            barrier.wait()
            fn()
            outcomes.append('ok')
        except ProcurementConflict:
            outcomes.append('conflict')
        except Exception as exc:
            errors.append(exc)

    threads = [Thread(target=runner), Thread(target=runner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert sorted(outcomes) == ['conflict', 'ok'], outcomes


def test_concurrent_approval_and_po_creation_have_single_winner():
    init_db(hash_password)
    with db() as conn:
        actor_id = _admin_id(conn)
        item = _item(conn)
        pr = create_requisition(conn, {
            'title': 'Concurrent procurement command regression',
            'items': [{'inventory_item_id': item['id'], 'description': item['name'], 'quantity': 2, 'estimated_unit_cost': item['unit_price']}],
        }, actor_id)
        submit_requisition(conn, pr['id'], actor_id)

    def approve_once():
        with db() as conn:
            approve_requisition(conn, pr['id'], _admin_id(conn))

    _run_race(approve_once)
    with db() as conn:
        assert conn.execute("SELECT status FROM purchase_requisitions WHERE id=?", (pr['id'],)).fetchone()['status'] == 'Approved'
        vendor_id = _vendor_id(conn)

    def create_po_once():
        with db() as conn:
            create_purchase_order(conn, {'pr_id': pr['id'], 'vendor_id': vendor_id, 'expected_delivery': None}, _admin_id(conn))

    _run_race(create_po_once)
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM purchase_orders WHERE pr_id=?', (pr['id'],)).fetchone()[0] == 1
        assert conn.execute('SELECT status FROM purchase_requisitions WHERE id=?', (pr['id'],)).fetchone()['status'] == 'Ordered'


def test_concurrent_purchase_order_receipt_increments_inventory_once():
    init_db(hash_password)
    with db() as conn:
        actor_id = _admin_id(conn)
        item = _item(conn)
        before = float(item['current_stock'])
        quantity = 3.0
        pr = create_requisition(conn, {
            'title': 'Concurrent receipt regression',
            'items': [{'inventory_item_id': item['id'], 'description': item['name'], 'quantity': quantity, 'estimated_unit_cost': item['unit_price']}],
        }, actor_id)
        submit_requisition(conn, pr['id'], actor_id)
        approve_requisition(conn, pr['id'], actor_id)
        po = create_purchase_order(conn, {'pr_id': pr['id'], 'vendor_id': _vendor_id(conn), 'expected_delivery': None}, actor_id)

    def receive_once():
        with db() as conn:
            receive_purchase_order(conn, po['id'], _admin_id(conn))

    _run_race(receive_once)
    with db() as conn:
        current = float(conn.execute('SELECT current_stock FROM inventory_items WHERE id=?', (item['id'],)).fetchone()['current_stock'])
        receipts = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions WHERE item_id=? AND tx_type='RECEIPT' AND reference=?",
            (item['id'], po['po_no']),
        ).fetchone()[0]
        assert current == before + quantity
        assert receipts == 1
        assert conn.execute('SELECT status FROM purchase_orders WHERE id=?', (po['id'],)).fetchone()['status'] == 'Received'
