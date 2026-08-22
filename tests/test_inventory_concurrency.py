from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.inventory_store import (
    InventoryConcurrencyConflict,
    adjust_stock_if_unchanged,
    increment_stock,
    issue_unreserved_stock,
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''CREATE TABLE inventory_items(
             id INTEGER PRIMARY KEY,
             current_stock REAL NOT NULL,
             reserved_stock REAL NOT NULL DEFAULT 0
           )'''
    )


def test_relative_stock_updates_compose_instead_of_overwriting_stale_values():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        'INSERT INTO inventory_items(id,current_stock,reserved_stock) VALUES(1,10,0)'
    )

    first_old, first_new = increment_stock(conn, 1, 2)
    second_old, second_new = increment_stock(conn, 1, 3)

    assert (first_old, first_new) == (10.0, 12.0)
    assert (second_old, second_new) == (12.0, 15.0)
    assert conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=1'
    ).fetchone()[0] == 15.0


def test_adjustment_rejects_stale_snapshot_instead_of_losing_new_stock():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        'INSERT INTO inventory_items(id,current_stock,reserved_stock) VALUES(1,10,0)'
    )

    stale_stock = 10.0
    increment_stock(conn, 1, 2)

    with pytest.raises(InventoryConcurrencyConflict, match='stock_changed'):
        adjust_stock_if_unchanged(conn, 1, stale_stock, 7)

    assert conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=1'
    ).fetchone()[0] == 12.0


def test_issue_guard_uses_current_reserved_stock_atomically():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        'INSERT INTO inventory_items(id,current_stock,reserved_stock) VALUES(1,10,4)'
    )

    old_stock, new_stock = issue_unreserved_stock(conn, 1, 6)
    assert (old_stock, new_stock) == (10.0, 4.0)

    with pytest.raises(
        InventoryConcurrencyConflict, match='insufficient_unreserved_stock'
    ):
        issue_unreserved_stock(conn, 1, 1)

    assert conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=1'
    ).fetchone()[0] == 4.0


def test_two_concurrent_receipts_are_both_preserved(tmp_path: Path):
    database = tmp_path / 'inventory-concurrency.db'
    setup = sqlite3.connect(database)
    setup.row_factory = sqlite3.Row
    _schema(setup)
    setup.execute(
        'INSERT INTO inventory_items(id,current_stock,reserved_stock) VALUES(1,10,0)'
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        conn = sqlite3.connect(database, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=5)
            increment_stock(conn, 1, 1)
            conn.commit()
        except BaseException as exc:  # surfaced in the parent test thread
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    check = sqlite3.connect(database)
    assert check.execute(
        'SELECT current_stock FROM inventory_items WHERE id=1'
    ).fetchone()[0] == 12.0
    check.close()
