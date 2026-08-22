from __future__ import annotations


class InventoryConcurrencyConflict(RuntimeError):
    """Raised when a stock write would overwrite a concurrent change."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def increment_stock(conn, item_id: int, quantity: float) -> tuple[float, float]:
    """Atomically add quantity and return (previous_stock, current_stock)."""
    cursor = conn.execute(
        'UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',
        (float(quantity), item_id),
    )
    if not _rowcount_one(cursor):
        raise KeyError('item_not_found')
    row = conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
    ).fetchone()
    current = float(row['current_stock'])
    return current - float(quantity), current


def issue_unreserved_stock(conn, item_id: int, quantity: float) -> tuple[float, float]:
    """Atomically issue stock only when enough unreserved quantity exists."""
    amount = abs(float(quantity))
    cursor = conn.execute(
        '''UPDATE inventory_items
           SET current_stock=current_stock-?
           WHERE id=? AND current_stock-reserved_stock>=?''',
        (amount, item_id, amount),
    )
    if not _rowcount_one(cursor):
        exists = conn.execute(
            'SELECT id FROM inventory_items WHERE id=?', (item_id,)
        ).fetchone()
        if not exists:
            raise KeyError('item_not_found')
        raise InventoryConcurrencyConflict('insufficient_unreserved_stock')
    row = conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
    ).fetchone()
    current = float(row['current_stock'])
    return current + amount, current


def adjust_stock_if_unchanged(
    conn, item_id: int, expected_stock: float, target_stock: float
) -> tuple[float, float]:
    """Set an absolute stock target only if the caller's snapshot is still current."""
    cursor = conn.execute(
        '''UPDATE inventory_items
           SET current_stock=?
           WHERE id=? AND current_stock=?''',
        (float(target_stock), item_id, float(expected_stock)),
    )
    if not _rowcount_one(cursor):
        exists = conn.execute(
            'SELECT id FROM inventory_items WHERE id=?', (item_id,)
        ).fetchone()
        if not exists:
            raise KeyError('item_not_found')
        raise InventoryConcurrencyConflict('stock_changed')
    return float(expected_stock), float(target_stock)


# ``app.main`` already imports this focused transaction module as part of its
# compatibility façade. Install state-machine, reservation, and authorization
# extensions here so the monolithic application does not need a large rewrite.
# All installers are idempotent and preserve historical role authorization.
from .approval_store import install_atomic_approval_route
from .procurement_store import install_procurement_routes
from .reservation_authorization import install_reservation_authorization_contract
from .reservation_store import install_reservation_routes

install_reservation_authorization_contract()
install_procurement_routes()
install_atomic_approval_route()
install_reservation_routes()
