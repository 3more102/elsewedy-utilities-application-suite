from __future__ import annotations

import hashlib
import json

from fastapi import Depends, Header, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


class InventoryTransferConflict(RuntimeError):
    """Raised when a transfer can no longer satisfy its stock/state invariants."""


class InventoryTransferIdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused with a different transfer."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def ensure_transfer_support(conn) -> None:
    """Create transfer coordination/idempotency storage after base schema init."""
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS inventory_item_creation_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO inventory_item_creation_lock(id,guard) VALUES(1,0)'
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS inventory_transfer_idempotency(
             user_id INTEGER NOT NULL,
             idempotency_key TEXT NOT NULL,
             payload_hash TEXT NOT NULL,
             source_item_id INTEGER NOT NULL,
             destination_item_id INTEGER,
             source_transaction_id INTEGER,
             destination_transaction_id INTEGER,
             response_stock REAL,
             created_at TEXT NOT NULL,
             PRIMARY KEY(user_id,idempotency_key)
           )'''
    )


def _lock_creation_coordinator(conn) -> None:
    locked = conn.execute(
        'UPDATE inventory_item_creation_lock SET guard=guard WHERE id=1'
    )
    if not _rowcount_one(locked):
        raise RuntimeError('inventory item creation coordinator is unavailable')


def _lock_item_rows(conn, item_ids) -> None:
    """Acquire inventory row locks in global numeric-ID order.

    The no-op UPDATE obtains a PostgreSQL row lock and participates in SQLite's
    serialized write behavior. Sorting is independent of transfer direction and
    therefore prevents A->B / B->A lock-order inversion.
    """
    for item_id in sorted({int(value) for value in item_ids}):
        locked = conn.execute(
            'UPDATE inventory_items SET reserved_stock=reserved_stock WHERE id=?',
            (item_id,),
        )
        if not _rowcount_one(locked):
            raise KeyError('Item not found')


def _item(conn, item_id: int) -> dict:
    row = conn.execute(
        'SELECT * FROM inventory_items WHERE id=?', (item_id,)
    ).fetchone()
    if not row:
        raise KeyError('Item not found')
    return dict(row)


def _destination(conn, source: dict, warehouse_id: int):
    # Historical matching is warehouse + name + category. Existing databases do
    # not enforce uniqueness on that tuple, so select the oldest matching row
    # deterministically rather than silently changing the business key here.
    row = conn.execute(
        '''SELECT * FROM inventory_items
           WHERE warehouse_id=? AND name=? AND category=?
           ORDER BY id LIMIT 1''',
        (warehouse_id, source['name'], source['category']),
    ).fetchone()
    return dict(row) if row else None


def _guarded_debit_locked(conn, source: dict, amount: float) -> float:
    available = float(source['current_stock']) - float(source['reserved_stock'])
    if available < amount:
        raise InventoryTransferConflict('insufficient_unreserved_stock')
    updated = conn.execute(
        '''UPDATE inventory_items
           SET current_stock=current_stock-?
           WHERE id=? AND current_stock-reserved_stock>=?''',
        (amount, source['id'], amount),
    )
    if not _rowcount_one(updated):
        raise InventoryTransferConflict('insufficient_unreserved_stock')
    row = conn.execute(
        'SELECT current_stock FROM inventory_items WHERE id=?', (source['id'],)
    ).fetchone()
    return float(row['current_stock'])


def _payload_hash(item_id: int, body) -> str:
    payload = {
        'source_item_id': int(item_id),
        'tx_type': 'TRANSFER',
        'quantity': abs(float(body.quantity)),
        'to_warehouse_id': int(body.to_warehouse_id),
        'work_order_id': body.work_order_id,
        'reference': str(body.reference or ''),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip()
    if not key:
        raise ValueError('Idempotency-Key must not be empty')
    if len(key) > 200:
        raise ValueError('Idempotency-Key must be 200 characters or fewer')
    return key


def _claim_idempotency(
    conn,
    user_id: int,
    key: str | None,
    payload_hash: str,
    source_item_id: int,
):
    if key is None:
        return None
    inserted = conn.execute(
        '''INSERT OR IGNORE INTO inventory_transfer_idempotency(
             user_id,idempotency_key,payload_hash,source_item_id,created_at
           ) VALUES(?,?,?,?,?)''',
        (user_id, key, payload_hash, source_item_id, now()),
    )
    if int(inserted.rowcount or 0) == 1:
        return None

    existing = conn.execute(
        '''SELECT payload_hash,response_stock,destination_item_id
           FROM inventory_transfer_idempotency
           WHERE user_id=? AND idempotency_key=?''',
        (user_id, key),
    ).fetchone()
    if not existing:
        raise RuntimeError('idempotency claim disappeared after conflict')
    if existing['payload_hash'] != payload_hash:
        raise InventoryTransferIdempotencyConflict(
            'Idempotency-Key was already used for a different transfer'
        )
    if existing['response_stock'] is None:
        raise RuntimeError('committed idempotency record is incomplete')
    return {
        'ok': True,
        'current_stock': float(existing['response_stock']),
    }


def _complete_idempotency(
    conn,
    user_id: int,
    key: str | None,
    destination_item_id: int,
    source_transaction_id: int,
    destination_transaction_id: int,
    response_stock: float,
) -> None:
    if key is None:
        return
    updated = conn.execute(
        '''UPDATE inventory_transfer_idempotency
           SET destination_item_id=?,source_transaction_id=?,
               destination_transaction_id=?,response_stock=?
           WHERE user_id=? AND idempotency_key=?''',
        (
            destination_item_id,
            source_transaction_id,
            destination_transaction_id,
            response_stock,
            user_id,
            key,
        ),
    )
    if not _rowcount_one(updated):
        raise RuntimeError('idempotency completion row is missing')


def _insert_destination(conn, source: dict, warehouse_id: int, amount: float) -> dict:
    number = _application.next_no(
        conn, 'inventory_items', 'item_no', 'ITM-', 1000
    )
    created = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,
             reserved_stock,min_level,max_level,reorder_point,unit_price,unit,
             vendor_id,bin
           ) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)''',
        (
            number,
            source['name'],
            source['description'],
            source['category'],
            warehouse_id,
            amount,
            source['min_level'],
            source['max_level'],
            source['reorder_point'],
            source['unit_price'],
            source['unit'],
            source['vendor_id'],
            source['bin'],
        ),
    )
    return _item(conn, int(created.lastrowid))


def create_inventory_atomic(conn, body, user: dict) -> dict:
    """Serialize the only non-transfer creator that shares the ITM- sequence."""
    _lock_creation_coordinator(conn)
    number = _application.next_no(
        conn, 'inventory_items', 'item_no', 'ITM-', 1000
    )
    values = body.model_dump()
    columns = list(values)
    created = conn.execute(
        f"INSERT INTO inventory_items(item_no,{','.join(columns)}) "
        f"VALUES(?,{','.join('?' * len(columns))})",
        (number, *values.values()),
    )
    append_audit(
        conn, user['id'], 'CREATE', 'Inventory', number, '', values
    )
    return {'id': int(created.lastrowid), 'item_no': number}


def transfer_inventory_atomic(
    conn,
    item_id: int,
    body,
    user: dict,
    idempotency_key: str | None = None,
) -> dict:
    source_snapshot = _item(conn, item_id)
    if not body.to_warehouse_id:
        raise ValueError('Destination warehouse required')
    if int(body.to_warehouse_id) == int(source_snapshot['warehouse_id']):
        raise ValueError('Destination warehouse must be different')

    amount = abs(float(body.quantity))
    if amount <= 0:
        raise ValueError('Transfer quantity must be greater than zero')

    key = _normalize_idempotency_key(idempotency_key)
    payload_hash = _payload_hash(item_id, body)
    replay = _claim_idempotency(
        conn, user['id'], key, payload_hash, item_id
    )
    if replay is not None:
        return replay

    destination = _destination(
        conn, source_snapshot, int(body.to_warehouse_id)
    )

    if destination is not None:
        # Canonical order is based only on stable inventory-item IDs, never on
        # business direction. Opposite transfers therefore acquire A/B in the
        # same order.
        _lock_item_rows(conn, (item_id, destination['id']))
        source = _item(conn, item_id)
        destination = _item(conn, int(destination['id']))
        old_stock = float(source['current_stock'])
        new_stock = _guarded_debit_locked(conn, source, amount)
        conn.execute(
            'UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',
            (amount, destination['id']),
        )
    else:
        # A nonexistent row cannot be row-locked. Serialize destination discovery
        # and ITM- allocation first, then re-query. The existing-row transfer path
        # never waits for this coordinator, so holding it while waiting on source
        # rows cannot form a coordinator/row-lock cycle.
        _lock_creation_coordinator(conn)
        source_snapshot = _item(conn, item_id)
        destination = _destination(
            conn, source_snapshot, int(body.to_warehouse_id)
        )
        if destination is not None:
            _lock_item_rows(conn, (item_id, destination['id']))
            source = _item(conn, item_id)
            destination = _item(conn, int(destination['id']))
            old_stock = float(source['current_stock'])
            new_stock = _guarded_debit_locked(conn, source, amount)
            conn.execute(
                'UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',
                (amount, destination['id']),
            )
        else:
            _lock_item_rows(conn, (item_id,))
            source = _item(conn, item_id)
            old_stock = float(source['current_stock'])
            new_stock = _guarded_debit_locked(conn, source, amount)
            destination = _insert_destination(
                conn, source, int(body.to_warehouse_id), amount
            )

    destination_tx = conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,
             reference,user_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?)''',
        (
            destination['id'],
            'TRANSFER',
            amount,
            source['warehouse_id'],
            body.to_warehouse_id,
            body.reference or source['item_no'],
            user['id'],
            now(),
        ),
    )
    source_tx = conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,
             work_order_id,reference,user_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            item_id,
            'TRANSFER',
            -amount,
            source['warehouse_id'],
            body.to_warehouse_id,
            body.work_order_id,
            body.reference,
            user['id'],
            now(),
        ),
    )

    append_audit(
        conn,
        user['id'],
        'TRANSFER',
        'Inventory',
        source['item_no'],
        old_stock,
        new_stock,
    )
    if new_stock - float(source['reserved_stock']) <= float(source['reorder_point']):
        _application.notify(
            conn,
            'Inventory below reorder point',
            f"{source['item_no']} — {source['name']} is below reorder point",
            'Warning',
            None,
            'storekeeper',
            'inventory',
            source['item_no'],
        )

    _complete_idempotency(
        conn,
        user['id'],
        key,
        int(destination['id']),
        int(source_tx.lastrowid),
        int(destination_tx.lastrowid),
        new_stock,
    )
    return {'ok': True, 'current_stock': new_stock}


def install_inventory_transfer_routes() -> None:
    """Replace final inventory routes after app.main has finished composition."""
    app = _application.app
    marker = '_euas_inventory_transfer_atomicity'
    if getattr(app.state, marker, False):
        return

    create_path = '/api/inventory'
    transaction_path = '/api/inventory/{item_id}/transaction'
    transaction_routes = [
        route
        for route in app.router.routes
        if getattr(route, 'path', None) == transaction_path
        and 'POST' in set(getattr(route, 'methods', set()) or set())
    ]
    if len(transaction_routes) != 1:
        raise RuntimeError(
            f'expected one final inventory transaction route, found {len(transaction_routes)}'
        )
    legacy_transaction_endpoint = transaction_routes[0].endpoint

    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) in (create_path, transaction_path)
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(create_path)
    def create_inventory_route(
        body: _application.InventoryIn,
        user=Depends(
            require_roles('admin', 'storekeeper', 'maintenance_manager')
        ),
    ):
        with db() as conn:
            return create_inventory_atomic(conn, body, user)

    @app.post(transaction_path)
    def inventory_transaction_route(
        item_id: int,
        body: _application.InventoryTxIn,
        idempotency_key: str | None = Header(
            default=None, alias='Idempotency-Key'
        ),
        user=Depends(require_roles(*_application.INV_ROLES)),
    ):
        if body.tx_type.upper() != 'TRANSFER':
            # Keep the previously hardened ISSUE/RETURN/RECEIPT/ADJUSTMENT path
            # byte-for-byte in authority by delegating to its final endpoint.
            return legacy_transaction_endpoint(item_id=item_id, body=body, user=user)
        try:
            with db() as conn:
                return transfer_inventory_atomic(
                    conn, item_id, body, user, idempotency_key
                )
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except InventoryTransferIdempotencyConflict as exc:
            raise HTTPException(409, str(exc))
        except InventoryTransferConflict as exc:
            if str(exc) == 'insufficient_unreserved_stock':
                raise HTTPException(409, 'Insufficient unreserved stock')
            raise HTTPException(409, str(exc))

    app.openapi_schema = None
    setattr(app.state, marker, True)
