from __future__ import annotations

from threading import Barrier, Thread
from uuid import uuid4

from app.auth import hash_password
from apps.inventory import (
    InventoryTransactionConflict,
    ReservationConflict,
    apply_inventory_transaction,
    issue_material_reservation,
    release_material_reservation,
    reserve_material,
)
from apps.maintenance import create_work_order
from core.database import db, init_db


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.username,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _new_item(conn, stock: float = 5.0) -> dict:
    warehouse_id = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()['id']
    code = f'RACE-{uuid4().hex[:10]}'
    cur = conn.execute(
        """INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,reserved_stock,min_level,max_level,
             reorder_point,unit_price,unit,bin
           ) VALUES(?,?,?,'Test',?,?,0,0,100,0,1,'ea','')""",
        (code, f'Race item {code}', 'isolated concurrency fixture', warehouse_id, stock),
    )
    return dict(conn.execute('SELECT * FROM inventory_items WHERE id=?', (cur.lastrowid,)).fetchone())


def _new_work(conn, actor: dict, title: str) -> int:
    return int(create_work_order(conn, {'title': title, 'priority': 'Medium'}, actor)['id'])


def _race(functions, conflict_types):
    barrier = Barrier(len(functions))
    outcomes: list[str] = []
    errors: list[Exception] = []

    def runner(fn):
        try:
            barrier.wait()
            fn()
            outcomes.append('ok')
        except conflict_types:
            outcomes.append('conflict')
        except Exception as exc:
            errors.append(exc)

    threads = [Thread(target=runner, args=(fn,)) for fn in functions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    return sorted(outcomes)


def test_two_reservations_competing_for_final_stock_have_one_winner():
    init_db(hash_password)
    with db() as conn:
        actor = _admin(conn)
        item = _new_item(conn, 5)
        work_a = _new_work(conn, actor, 'Inventory reservation race A')
        work_b = _new_work(conn, actor, 'Inventory reservation race B')

    def reserve_once(work_id):
        def command():
            with db() as conn:
                reserve_material(conn, work_id, item['id'], 4, _admin(conn)['id'], 'race')
        return command

    outcomes = _race([reserve_once(work_a), reserve_once(work_b)], (ReservationConflict,))
    assert outcomes == ['conflict', 'ok']
    with db() as conn:
        row = conn.execute('SELECT reserved_stock,current_stock FROM inventory_items WHERE id=?', (item['id'],)).fetchone()
        ledger = conn.execute(
            """SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
               WHERE inventory_item_id=? AND status IN ('Reserved','Partially Issued')""",
            (item['id'],),
        ).fetchone()[0]
        assert float(row['current_stock']) == 5
        assert float(row['reserved_stock']) == 4
        assert float(ledger) == 4


def test_release_and_issue_race_has_single_terminal_effect():
    init_db(hash_password)
    with db() as conn:
        actor = _admin(conn)
        item = _new_item(conn, 5)
        work = _new_work(conn, actor, 'Reservation release issue race')
        reservation = reserve_material(conn, work, item['id'], 4, actor['id'], 'race')

    def release_once():
        with db() as conn:
            release_material_reservation(conn, reservation['id'], _admin(conn)['id'])

    def issue_once():
        with db() as conn:
            issue_material_reservation(conn, reservation['id'], 4, _admin(conn))

    outcomes = _race([release_once, issue_once], (ReservationConflict,))
    assert outcomes == ['conflict', 'ok']
    with db() as conn:
        state = conn.execute('SELECT status,issued_quantity FROM inventory_reservations WHERE id=?', (reservation['id'],)).fetchone()
        stock = float(conn.execute('SELECT current_stock FROM inventory_items WHERE id=?', (item['id'],)).fetchone()['current_stock'])
        if state['status'] == 'Issued':
            assert float(state['issued_quantity']) == 4 and stock == 1
        else:
            assert state['status'] == 'Released' and float(state['issued_quantity']) == 0 and stock == 5


def test_inventory_transaction_idempotency_prevents_duplicate_receipt():
    init_db(hash_password)
    with db() as conn:
        actor = _admin(conn)
        item = _new_item(conn, 5)
        key = f'receipt-{uuid4().hex}'
        first = apply_inventory_transaction(conn, item['id'], {'tx_type': 'RECEIPT', 'quantity': 3, 'reference': 'RCV-1', 'idempotency_key': key}, actor['id'])
        second = apply_inventory_transaction(conn, item['id'], {'tx_type': 'RECEIPT', 'quantity': 3, 'reference': 'RCV-1', 'idempotency_key': key}, actor['id'])
        assert not first['idempotent_replay']
        assert second['idempotent_replay']
        assert second['transaction_id'] == first['transaction_id']
        assert second['current_stock'] == 8
        assert conn.execute('SELECT COUNT(*) FROM inventory_transactions WHERE idempotency_key=?', (key,)).fetchone()[0] == 1


def test_reusing_idempotency_key_for_different_operation_is_rejected():
    init_db(hash_password)
    with db() as conn:
        actor = _admin(conn)
        item = _new_item(conn, 5)
        key = f'inventory-op-{uuid4().hex}'
        apply_inventory_transaction(conn, item['id'], {'tx_type': 'RECEIPT', 'quantity': 1, 'idempotency_key': key}, actor['id'])
        try:
            apply_inventory_transaction(conn, item['id'], {'tx_type': 'RECEIPT', 'quantity': 2, 'idempotency_key': key}, actor['id'])
        except InventoryTransactionConflict:
            pass
        else:
            raise AssertionError('conflicting idempotency-key reuse must be rejected')
        assert float(conn.execute('SELECT current_stock FROM inventory_items WHERE id=?', (item['id'],)).fetchone()['current_stock']) == 6


def test_two_unreserved_issues_cannot_both_consume_final_stock():
    init_db(hash_password)
    with db() as conn:
        actor = _admin(conn)
        item = _new_item(conn, 5)

    def issue_once(key):
        def command():
            with db() as conn:
                apply_inventory_transaction(
                    conn, item['id'],
                    {'tx_type': 'ISSUE', 'quantity': 4, 'idempotency_key': key},
                    _admin(conn)['id'],
                )
        return command

    outcomes = _race(
        [issue_once(f'issue-a-{uuid4().hex}'), issue_once(f'issue-b-{uuid4().hex}')],
        (InventoryTransactionConflict,),
    )
    assert outcomes == ['conflict', 'ok']
    with db() as conn:
        assert float(conn.execute('SELECT current_stock FROM inventory_items WHERE id=?', (item['id'],)).fetchone()['current_stock']) == 1
        assert conn.execute("SELECT COUNT(*) FROM inventory_transactions WHERE item_id=? AND tx_type='ISSUE'", (item['id'],)).fetchone()[0] == 1
