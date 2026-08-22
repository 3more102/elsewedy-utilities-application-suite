from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import InventoryIn, InventoryTxIn
from app.database import db, now
from app.main import app
from app.transfer_store import (
    InventoryTransferConflict,
    InventoryTransferIdempotencyConflict,
    create_inventory_atomic,
    transfer_inventory_atomic,
)


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _warehouses(conn, suffix: str, count: int) -> list[int]:
    site = conn.execute('SELECT id FROM sites ORDER BY id LIMIT 1').fetchone()
    assert site
    result = []
    for index in range(count):
        row = conn.execute(
            '''INSERT INTO warehouses(warehouse_code,name,site_id,status)
               VALUES(?,?,?,'Active')''',
            (
                f'WH-TX-{suffix}-{index}',
                f'Transfer Test Warehouse {suffix}-{index}',
                site['id'],
            ),
        )
        result.append(int(row.lastrowid))
    return result


def _item(
    conn,
    suffix: str,
    warehouse_id: int,
    stock: float,
    *,
    name: str = 'Transfer Test Part',
    category: str = 'CI-TRANSFER',
) -> int:
    row = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,
             reserved_stock,min_level,max_level,reorder_point,unit_price,unit,bin
           ) VALUES(?,?,?,?,?,?,0,0,100,0,1,'ea','CI')''',
        (
            f'ITM-TX-{suffix}',
            name,
            'transfer atomicity regression',
            category,
            warehouse_id,
            stock,
        ),
    )
    return int(row.lastrowid)


def _stock(conn, item_id: int) -> float:
    return float(
        conn.execute(
            'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
        ).fetchone()['current_stock']
    )


def test_opposite_direction_transfers_preserve_total_stock():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            wh_a, wh_b = _warehouses(conn, suffix, 2)
            item_a = _item(conn, suffix + '-A', wh_a, 10)
            item_b = _item(conn, suffix + '-B', wh_b, 10)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def move(source: int, target_warehouse: int, quantity: float) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    transfer_inventory_atomic(
                        conn,
                        source,
                        InventoryTxIn(
                            tx_type='TRANSFER',
                            quantity=quantity,
                            to_warehouse_id=target_warehouse,
                            reference=f'opposite-{suffix}',
                        ),
                        user,
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=move, args=(item_a, wh_b, 3)),
            threading.Thread(target=move, args=(item_b, wh_a, 4)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        with db() as conn:
            stock_a = _stock(conn, item_a)
            stock_b = _stock(conn, item_b)
            transfer_rows = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='TRANSFER'""",
                    (f'opposite-{suffix}',),
                ).fetchone()[0]
            )
        assert stock_a == 11
        assert stock_b == 9
        assert stock_a + stock_b == 20
        assert transfer_rows == 4


def test_same_source_overdemand_has_one_winner_and_no_negative_stock():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            wh_a, wh_b, wh_c = _warehouses(conn, suffix, 3)
            source = _item(conn, suffix + '-SRC', wh_a, 10)
            dest_b = _item(conn, suffix + '-B', wh_b, 0)
            dest_c = _item(conn, suffix + '-C', wh_c, 0)

        barrier = threading.Barrier(2)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def move(index: int, warehouse_id: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    transfer_inventory_atomic(
                        conn,
                        source,
                        InventoryTxIn(
                            tx_type='TRANSFER',
                            quantity=8,
                            to_warehouse_id=warehouse_id,
                            reference=f'overdemand-{suffix}',
                        ),
                        user,
                    )
                wins.append(index)
            except InventoryTransferConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=move, args=(0, wh_b)),
            threading.Thread(target=move, args=(1, wh_c)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 1
        assert len(conflicts) == 1
        with db() as conn:
            source_stock = _stock(conn, source)
            total = source_stock + _stock(conn, dest_b) + _stock(conn, dest_c)
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='TRANSFER'""",
                    (f'overdemand-{suffix}',),
                ).fetchone()[0]
            )
        assert source_stock == 2
        assert source_stock >= 0
        assert total == 10
        assert tx_count == 2


def test_concurrent_missing_destination_creates_one_counterpart():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        workers = 4
        with db() as conn:
            user = _admin(conn)
            wh_source, wh_dest = _warehouses(conn, suffix, 2)
            source = _item(conn, suffix + '-SRC', wh_source, 20)
            source_row = conn.execute(
                'SELECT name,category FROM inventory_items WHERE id=?', (source,)
            ).fetchone()

        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []

        def move(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    transfer_inventory_atomic(
                        conn,
                        source,
                        InventoryTxIn(
                            tx_type='TRANSFER',
                            quantity=2,
                            to_warehouse_id=wh_dest,
                            reference=f'missing-{suffix}',
                        ),
                        user,
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=move, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        with db() as conn:
            destinations = conn.execute(
                '''SELECT id,current_stock FROM inventory_items
                   WHERE warehouse_id=? AND name=? AND category=?''',
                (wh_dest, source_row['name'], source_row['category']),
            ).fetchall()
            source_stock = _stock(conn, source)
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='TRANSFER'""",
                    (f'missing-{suffix}',),
                ).fetchone()[0]
            )
        assert len(destinations) == 1
        assert float(destinations[0]['current_stock']) == 8
        assert source_stock == 12
        assert source_stock + float(destinations[0]['current_stock']) == 20
        assert tx_count == workers * 2


def test_transfer_idempotency_replays_once_and_rejects_conflicting_payload():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        workers = 6
        key = f'transfer-key-{suffix}'
        with db() as conn:
            user = _admin(conn)
            wh_source, wh_dest = _warehouses(conn, suffix, 2)
            source = _item(conn, suffix + '-SRC', wh_source, 10)
            destination = _item(conn, suffix + '-DST', wh_dest, 0)

        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []
        results: list[dict] = []

        def move() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(
                        transfer_inventory_atomic(
                            conn,
                            source,
                            InventoryTxIn(
                                tx_type='TRANSFER',
                                quantity=3,
                                to_warehouse_id=wh_dest,
                                reference=f'idem-{suffix}',
                            ),
                            user,
                            key,
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=move) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == workers
        assert all(result == {'ok': True, 'current_stock': 7.0} for result in results)

        with db() as conn:
            assert _stock(conn, source) == 7
            assert _stock(conn, destination) == 3
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='TRANSFER'""",
                    (f'idem-{suffix}',),
                ).fetchone()[0]
            )
            audit_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Inventory' AND action='TRANSFER'
                         AND record_id=(SELECT item_no FROM inventory_items WHERE id=?)""",
                    (source,),
                ).fetchone()[0]
            )
        assert tx_count == 2
        assert audit_count == 1

        with db() as conn:
            try:
                transfer_inventory_atomic(
                    conn,
                    source,
                    InventoryTxIn(
                        tx_type='TRANSFER',
                        quantity=4,
                        to_warehouse_id=wh_dest,
                        reference=f'idem-{suffix}',
                    ),
                    user,
                    key,
                )
            except InventoryTransferIdempotencyConflict:
                pass
            else:
                raise AssertionError('conflicting idempotency payload unexpectedly succeeded')

        with db() as conn:
            assert _stock(conn, source) == 7
            assert _stock(conn, destination) == 3


def test_failed_transfer_rolls_back_debit_credit_and_idempotency_claim():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            wh_source, wh_dest = _warehouses(conn, suffix, 2)
            source = _item(conn, suffix + '-SRC', wh_source, 10)
            destination = _item(conn, suffix + '-DST', wh_dest, 0)

        try:
            with db() as conn:
                transfer_inventory_atomic(
                    conn,
                    source,
                    InventoryTxIn(
                        tx_type='TRANSFER',
                        quantity=2,
                        to_warehouse_id=wh_dest,
                        work_order_id=999999999,
                        reference=f'rollback-{suffix}',
                    ),
                    user,
                    f'rollback-key-{suffix}',
                )
        except Exception:
            pass
        else:
            raise AssertionError('invalid-FK transfer unexpectedly committed')

        with db() as conn:
            assert _stock(conn, source) == 10
            assert _stock(conn, destination) == 0
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='TRANSFER'""",
                    (f'rollback-{suffix}',),
                ).fetchone()[0]
            )
            idem = conn.execute(
                '''SELECT 1 FROM inventory_transfer_idempotency
                   WHERE user_id=? AND idempotency_key=?''',
                (user['id'], f'rollback-key-{suffix}'),
            ).fetchone()
        assert tx_count == 0
        assert idem is None


def test_manual_inventory_creation_and_transfer_destination_share_number_lock():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        workers = 4
        with db() as conn:
            user = _admin(conn)
            warehouse = _warehouses(conn, suffix, 1)[0]

        barrier = threading.Barrier(workers)
        numbers: list[str] = []
        errors: list[BaseException] = []

        def create(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    result = create_inventory_atomic(
                        conn,
                        InventoryIn(
                            name=f'Concurrent created item {suffix}-{index}',
                            category='CI-CREATE',
                            warehouse_id=warehouse,
                        ),
                        user,
                    )
                numbers.append(result['item_no'])
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(numbers) == workers
        assert len(set(numbers)) == workers
