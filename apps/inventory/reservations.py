from __future__ import annotations


def _rows(cur):
    return [dict(row) for row in cur.fetchall()]


def reservation_rows(conn, work_order_id: int) -> list[dict]:
    return _rows(conn.execute(
        """SELECT r.*,i.item_no,i.name item_name,i.unit,i.current_stock,i.reserved_stock,u.full_name reserved_by_name
           FROM inventory_reservations r JOIN inventory_items i ON i.id=r.inventory_item_id JOIN users u ON u.id=r.reserved_by
           WHERE r.work_order_id=? ORDER BY r.id DESC""",
        (work_order_id,),
    ))


def sync_reserved_stock(conn, item_id: int) -> float:
    reserved = conn.execute(
        """SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
           WHERE inventory_item_id=? AND status IN ('Reserved','Partially Issued')""",
        (item_id,),
    ).fetchone()[0] or 0
    value = max(0.0, float(reserved))
    conn.execute('UPDATE inventory_items SET reserved_stock=? WHERE id=?', (value, item_id))
    return value


def reconcile_reserved_stock(conn) -> int:
    """Rebuild the cached reserved_stock values from the reservation ledger."""
    item_ids = [row[0] for row in conn.execute('SELECT id FROM inventory_items').fetchall()]
    for item_id in item_ids:
        sync_reserved_stock(conn, int(item_id))
    return len(item_ids)


def work_order_parts_readiness(conn, work_order_id: int) -> dict:
    requirements = _rows(conn.execute(
        """SELECT r.*,i.item_no,i.name,i.unit,i.current_stock,i.reserved_stock,w.warehouse_code,w.name warehouse_name
           FROM work_order_requirements r JOIN inventory_items i ON i.id=r.inventory_item_id JOIN warehouses w ON w.id=i.warehouse_id
           WHERE r.work_order_id=? AND r.status<>'Cancelled' ORDER BY i.item_no""",
        (work_order_id,),
    ))
    if not requirements:
        return {'state': 'Unknown', 'ready': None, 'requirements': [], 'shortage_items': 0, 'reserved_items': 0}
    shortages = 0
    reserved_items = 0
    for requirement in requirements:
        issued = float(conn.execute(
            'SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',
            (work_order_id, requirement['inventory_item_id']),
        ).fetchone()[0] or 0)
        reserved = float(conn.execute(
            """SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
               WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')""",
            (work_order_id, requirement['inventory_item_id']),
        ).fetchone()[0] or 0)
        unreserved = max(0.0, float(requirement['current_stock']) - float(requirement['reserved_stock']))
        remaining = max(0.0, float(requirement['quantity']) - issued)
        secured = reserved + unreserved
        requirement['available_stock'] = round(unreserved, 3)
        requirement['reserved_for_work'] = round(reserved, 3)
        requirement['issued_quantity'] = round(issued, 3)
        requirement['remaining_required'] = round(remaining, 3)
        requirement['shortage'] = round(max(0.0, remaining - secured), 3)
        requirement['ready'] = requirement['shortage'] <= 0
        if reserved > 0:
            reserved_items += 1
        if not requirement['ready']:
            shortages += 1
    return {
        'state': 'Ready' if shortages == 0 else 'Shortage', 'ready': shortages == 0,
        'requirements': requirements, 'shortage_items': shortages, 'reserved_items': reserved_items,
    }
