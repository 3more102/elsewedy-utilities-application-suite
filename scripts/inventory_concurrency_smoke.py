from __future__ import annotations

import threading
import uuid

from app.database import db
from app.inventory_store import (
    InventoryConcurrencyConflict,
    adjust_stock_if_unchanged,
    increment_stock,
)


def main() -> None:
    item_no = f'CI-CONC-{uuid.uuid4().hex[:12]}'
    item_id = None
    try:
        with db() as conn:
            warehouse = conn.execute(
                'SELECT id FROM warehouses ORDER BY id LIMIT 1'
            ).fetchone()
            if not warehouse:
                raise RuntimeError('inventory concurrency smoke requires a warehouse')
            created = conn.execute(
                '''INSERT INTO inventory_items(
                     item_no,name,category,warehouse_id,current_stock,unit
                   ) VALUES(?,?,?,?,?,?)''',
                (item_no, 'Concurrency smoke item', 'CI', warehouse['id'], 10.0, 'ea'),
            )
            item_id = int(created.lastrowid)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def receipt_worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    increment_stock(conn, item_id, 1.0)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=receipt_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        if any(thread.is_alive() for thread in threads):
            raise RuntimeError('inventory concurrency smoke worker did not finish')
        if errors:
            raise RuntimeError(f'inventory concurrency smoke worker failed: {errors!r}')

        with db() as conn:
            stock = float(
                conn.execute(
                    'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
                ).fetchone()['current_stock']
            )
        if stock != 12.0:
            raise RuntimeError(
                f'concurrent receipts lost an update: expected 12.0, got {stock}'
            )

        stale_stock = stock
        with db() as conn:
            increment_stock(conn, item_id, 1.0)

        conflict_seen = False
        try:
            with db() as conn:
                adjust_stock_if_unchanged(conn, item_id, stale_stock, 5.0)
        except InventoryConcurrencyConflict as exc:
            if str(exc) != 'stock_changed':
                raise
            conflict_seen = True
        if not conflict_seen:
            raise RuntimeError('stale absolute adjustment unexpectedly overwrote stock')

        with db() as conn:
            final_stock = float(
                conn.execute(
                    'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
                ).fetchone()['current_stock']
            )
        if final_stock != 13.0:
            raise RuntimeError(
                f'stale adjustment corrupted stock: expected 13.0, got {final_stock}'
            )

        print('inventory concurrency smoke: PASS')
    finally:
        if item_id is not None:
            with db() as conn:
                conn.execute('DELETE FROM inventory_items WHERE id=?', (item_id,))


if __name__ == '__main__':
    main()
