from __future__ import annotations

import hashlib
import json

from apps.audit import audit
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no


class InventoryItemNotFound(LookupError):
    pass


class InventoryTransactionConflict(RuntimeError):
    pass


class InventoryTransactionInvalid(ValueError):
    pass


def _begin_write(conn) -> None:
    if DB_BACKEND == 'sqlite' and not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def _operation_fingerprint(item_id: int, data: dict) -> str:
    payload = {
        'item_id': int(item_id),
        'tx_type': str(data.get('tx_type') or '').upper(),
        'quantity': float(data.get('quantity') or 0),
        'to_warehouse_id': data.get('to_warehouse_id'),
        'work_order_id': data.get('work_order_id'),
        'reference': str(data.get('reference') or ''),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _lock_item(conn, item_id: int):
    _begin_write(conn)
    suffix = ' FOR UPDATE' if DB_BACKEND == 'postgresql' else ''
    row = conn.execute(f'SELECT * FROM inventory_items WHERE id=?{suffix}', (item_id,)).fetchone()
    if not row:
        raise InventoryItemNotFound('Item not found')
    return dict(row)


def _result_for_item(conn, item_id: int, *, replay: bool = False, transaction_id=None) -> dict:
    item = conn.execute('SELECT * FROM inventory_items WHERE id=?', (item_id,)).fetchone()
    if not item:
        raise InventoryItemNotFound('Item not found')
    item = dict(item)
    current = float(item['current_stock'])
    reserved = float(item['reserved_stock'])
    return {
        'ok': True,
        'current_stock': current,
        'item_no': item['item_no'],
        'item_name': item['name'],
        'reserved_stock': reserved,
        'reorder_point': float(item['reorder_point']),
        'low_stock': current - reserved <= float(item['reorder_point']),
        'idempotent_replay': bool(replay),
        'transaction_id': transaction_id,
    }


def apply_inventory_transaction(conn, item_id: int, data: dict, actor_id: int) -> dict:
    """Apply one serialized inventory movement with reserved-stock and idempotency protection."""
    payload = dict(data)
    tx_type = str(payload.get('tx_type') or '').upper()
    requested_quantity = float(payload.get('quantity') or 0)
    to_warehouse_id = payload.get('to_warehouse_id')
    work_order_id = payload.get('work_order_id')
    reference = payload.get('reference') or ''
    idempotency_key = str(payload.get('idempotency_key') or '').strip() or None
    fingerprint = _operation_fingerprint(item_id, payload)

    _begin_write(conn)
    if idempotency_key:
        existing = conn.execute(
            'SELECT id,item_id,operation_fingerprint FROM inventory_transactions WHERE idempotency_key=?',
            (idempotency_key,),
        ).fetchone()
        if existing:
            if int(existing['item_id']) != int(item_id) or str(existing['operation_fingerprint'] or '') != fingerprint:
                raise InventoryTransactionConflict('Idempotency key was already used for a different inventory operation')
            return _result_for_item(conn, item_id, replay=True, transaction_id=existing['id'])

    item = _lock_item(conn, item_id)
    quantity = requested_quantity
    if tx_type == 'ISSUE':
        move = abs(quantity)
        if move <= 0:
            raise InventoryTransactionInvalid('Quantity must be greater than zero')
        available = max(float(item['current_stock']) - float(item['reserved_stock']), 0)
        if available + 1e-9 < move:
            raise InventoryTransactionConflict(
                'Insufficient unreserved stock; release or issue the work-order reservation first'
            )
        quantity = -move
    elif tx_type in ('RETURN', 'RECEIPT'):
        quantity = abs(quantity)
        if quantity <= 0:
            raise InventoryTransactionInvalid('Quantity must be greater than zero')
    elif tx_type == 'ADJUSTMENT':
        desired = requested_quantity
        if desired < -1e-9:
            raise InventoryTransactionInvalid('Inventory stock cannot be negative')
        if desired + 1e-9 < float(item['reserved_stock']):
            raise InventoryTransactionConflict('Adjustment cannot reduce stock below reserved quantity')
        quantity = desired - float(item['current_stock'])
    elif tx_type == 'TRANSFER':
        if not to_warehouse_id:
            raise InventoryTransactionInvalid('Destination warehouse required')
        if int(to_warehouse_id) == int(item['warehouse_id']):
            raise InventoryTransactionInvalid('Destination warehouse must be different')
        move = abs(quantity)
        if move <= 0:
            raise InventoryTransactionInvalid('Quantity must be greater than zero')
        available = max(float(item['current_stock']) - float(item['reserved_stock']), 0)
        if available + 1e-9 < move:
            raise InventoryTransactionConflict(
                'Insufficient unreserved stock; reserved material cannot be transferred'
            )
        quantity = -move
        destination = conn.execute(
            'SELECT * FROM inventory_items WHERE warehouse_id=? AND name=? AND category=?',
            (to_warehouse_id, item['name'], item['category']),
        ).fetchone()
        if destination:
            if DB_BACKEND == 'postgresql':
                destination = conn.execute('SELECT * FROM inventory_items WHERE id=? FOR UPDATE', (destination['id'],)).fetchone()
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
                    destination_no, item['name'], item['description'], item['category'], to_warehouse_id, move,
                    item['min_level'], item['max_level'], item['reorder_point'], item['unit_price'], item['unit'],
                    item['vendor_id'], item['bin'],
                ),
            )
            destination_id = created.lastrowid
        conn.execute(
            '''INSERT INTO inventory_transactions(
                 item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,reference,user_id,created_at,
                 operation_fingerprint
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                destination_id, 'TRANSFER', move, item['warehouse_id'], to_warehouse_id,
                reference or item['item_no'], actor_id, now(), fingerprint,
            ),
        )
    else:
        raise InventoryTransactionInvalid('Invalid transaction type')

    new_stock = float(item['current_stock']) + quantity
    if new_stock < -1e-9:
        raise InventoryTransactionConflict('Inventory stock cannot become negative')
    if new_stock + 1e-9 < float(item['reserved_stock']):
        raise InventoryTransactionConflict('Inventory stock cannot fall below reserved quantity')
    conn.execute('UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?', (quantity, item_id))
    cur = conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,work_order_id,reference,user_id,created_at,
             idempotency_key,operation_fingerprint
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (
            item_id, tx_type, quantity, item['warehouse_id'], to_warehouse_id, work_order_id, reference,
            actor_id, now(), idempotency_key, fingerprint,
        ),
    )
    audit(conn, actor_id, tx_type, 'Inventory', item['item_no'], item['current_stock'], new_stock)
    return _result_for_item(conn, item_id, transaction_id=cur.lastrowid)
