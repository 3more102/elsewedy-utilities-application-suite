from __future__ import annotations

from app.database import now
from apps.audit import audit
from core.shared import next_no


class InventoryItemNotFound(LookupError):
    pass


class InventoryTransactionConflict(RuntimeError):
    pass


class InventoryTransactionInvalid(ValueError):
    pass


def apply_inventory_transaction(conn, item_id: int, data: dict, actor_id: int) -> dict:
    """Apply one inventory movement while protecting reserved stock."""
    row = conn.execute('SELECT * FROM inventory_items WHERE id=?', (item_id,)).fetchone()
    if not row:
        raise InventoryItemNotFound('Item not found')
    item = dict(row)
    tx_type = str(data.get('tx_type') or '').upper()
    quantity = float(data.get('quantity') or 0)
    to_warehouse_id = data.get('to_warehouse_id')
    work_order_id = data.get('work_order_id')
    reference = data.get('reference') or ''

    if tx_type == 'ISSUE':
        quantity = -abs(quantity)
        available = max(float(item['current_stock']) - float(item['reserved_stock']), 0)
        if available < abs(quantity):
            raise InventoryTransactionConflict(
                'Insufficient unreserved stock; release or issue the work-order reservation first'
            )
    elif tx_type in ('RETURN', 'RECEIPT'):
        quantity = abs(quantity)
    elif tx_type == 'ADJUSTMENT':
        quantity = float(data.get('quantity') or 0) - float(item['current_stock'])
    elif tx_type == 'TRANSFER':
        if not to_warehouse_id:
            raise InventoryTransactionInvalid('Destination warehouse required')
        if to_warehouse_id == item['warehouse_id']:
            raise InventoryTransactionInvalid('Destination warehouse must be different')
        move = abs(quantity)
        available = max(float(item['current_stock']) - float(item['reserved_stock']), 0)
        if available < move:
            raise InventoryTransactionConflict(
                'Insufficient unreserved stock; reserved material cannot be transferred'
            )
        quantity = -move
        destination = conn.execute(
            'SELECT * FROM inventory_items WHERE warehouse_id=? AND name=? AND category=?',
            (to_warehouse_id, item['name'], item['category']),
        ).fetchone()
        if destination:
            conn.execute(
                'UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',
                (move, destination['id']),
            )
            destination_id = destination['id']
        else:
            destination_no = next_no(conn, 'inventory_items', 'item_no', 'ITM-', 1000)
            created = conn.execute(
                '''INSERT INTO inventory_items(
                     item_no,name,description,category,warehouse_id,current_stock,reserved_stock,
                     min_level,max_level,reorder_point,unit_price,unit,vendor_id,bin
                   ) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)''',
                (
                    destination_no,
                    item['name'],
                    item['description'],
                    item['category'],
                    to_warehouse_id,
                    move,
                    item['min_level'],
                    item['max_level'],
                    item['reorder_point'],
                    item['unit_price'],
                    item['unit'],
                    item['vendor_id'],
                    item['bin'],
                ),
            )
            destination_id = created.lastrowid
        conn.execute(
            '''INSERT INTO inventory_transactions(
                 item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,reference,user_id,created_at
               ) VALUES(?,?,?,?,?,?,?,?)''',
            (
                destination_id,
                'TRANSFER',
                move,
                item['warehouse_id'],
                to_warehouse_id,
                reference or item['item_no'],
                actor_id,
                now(),
            ),
        )
    else:
        raise InventoryTransactionInvalid('Invalid transaction type')

    new_stock = float(item['current_stock']) + quantity
    conn.execute('UPDATE inventory_items SET current_stock=? WHERE id=?', (new_stock, item_id))
    conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,work_order_id,reference,user_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            item_id,
            tx_type,
            quantity,
            item['warehouse_id'],
            to_warehouse_id,
            work_order_id,
            reference,
            actor_id,
            now(),
        ),
    )
    audit(conn, actor_id, tx_type, 'Inventory', item['item_no'], item['current_stock'], new_stock)
    return {
        'ok': True,
        'current_stock': new_stock,
        'item_no': item['item_no'],
        'item_name': item['name'],
        'reserved_stock': float(item['reserved_stock']),
        'reorder_point': float(item['reorder_point']),
        'low_stock': new_stock - float(item['reserved_stock']) <= float(item['reorder_point']),
    }
