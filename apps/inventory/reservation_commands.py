from __future__ import annotations

from apps.audit import audit
from apps.maintenance import post_cost
from apps.notifications import notify
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no

from .reservations import sync_reserved_stock, work_order_parts_readiness


class ReservationCommandError(RuntimeError):
    status_code = 409


class ReservationNotFound(ReservationCommandError):
    status_code = 404


class ReservationForbidden(ReservationCommandError):
    status_code = 403


class ReservationConflict(ReservationCommandError):
    status_code = 409


def _begin_write(conn) -> None:
    if DB_BACKEND == 'sqlite' and not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def _lock_row(conn, sql: str, args=()):
    suffix = ' FOR UPDATE' if DB_BACKEND == 'postgresql' else ''
    return conn.execute(sql + suffix, args).fetchone()


def reserve_material(conn, work_order_id: int, item_id: int, quantity: float, actor_id: int, notes: str = '') -> dict:
    _begin_write(conn)
    work = _lock_row(conn, 'SELECT * FROM work_orders WHERE id=?', (work_order_id,))
    if not work:
        raise ReservationNotFound('Work order not found')
    item = _lock_row(conn, 'SELECT * FROM inventory_items WHERE id=?', (item_id,))
    if not item:
        raise ReservationNotFound('Inventory item not found')
    quantity = float(quantity)
    if quantity <= 0:
        raise ReservationConflict('Reservation quantity must be greater than zero')
    # Rebuild the cache while the item is serialized so availability is derived from the ledger.
    reserved = sync_reserved_stock(conn, item_id)
    available = float(item['current_stock']) - reserved
    if available + 1e-9 < quantity:
        raise ReservationConflict(f'Insufficient unreserved stock ({available:g} {item["unit"]})')
    number = next_no(conn, 'inventory_reservations', 'reservation_no', 'RSV-', 20001)
    cur = conn.execute(
        """INSERT INTO inventory_reservations(
             reservation_no,work_order_id,inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at,notes
           ) VALUES(?,?,?,?,0,'Reserved',?,?,?)""",
        (number, work_order_id, item_id, quantity, actor_id, now(), notes),
    )
    sync_reserved_stock(conn, item_id)
    requirement = conn.execute(
        'SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',
        (work_order_id, item_id),
    ).fetchone()
    if requirement:
        work_reserved = conn.execute(
            """SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
               WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')""",
            (work_order_id, item_id),
        ).fetchone()[0] or 0
        if float(work_reserved) + 1e-9 >= float(requirement['quantity']):
            conn.execute("UPDATE work_order_requirements SET status='Reserved' WHERE id=?", (requirement['id'],))
    audit(
        conn, actor_id, 'RESERVE MATERIAL', 'Work Management', work['wo_no'], '',
        {'reservation': number, 'item': item['item_no'], 'quantity': quantity},
    )
    notify(
        conn, 'Material reserved',
        f'{number} reserved {quantity:g} {item["unit"]} of {item["item_no"]} for {work["wo_no"]}',
        'Info', work['assigned_to'], None, 'work', work['wo_no'],
    )
    return {'id': cur.lastrowid, 'reservation_no': number, 'readiness': work_order_parts_readiness(conn, work_order_id)}


def reserve_all_materials(conn, work_order_id: int, actor_id: int) -> dict:
    _begin_write(conn)
    work = _lock_row(conn, 'SELECT * FROM work_orders WHERE id=?', (work_order_id,))
    if not work:
        raise ReservationNotFound('Work order not found')
    created: list[str] = []
    shortages: list[dict] = []
    requirements = conn.execute(
        "SELECT * FROM work_order_requirements WHERE work_order_id=? AND status<>'Cancelled' ORDER BY inventory_item_id",
        (work_order_id,),
    ).fetchall()
    for requirement in requirements:
        item = _lock_row(conn, 'SELECT * FROM inventory_items WHERE id=?', (requirement['inventory_item_id'],))
        if not item:
            raise ReservationNotFound('Inventory item not found')
        issued = float(conn.execute(
            'SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',
            (work_order_id, item['id']),
        ).fetchone()[0] or 0)
        already = float(conn.execute(
            """SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations
               WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')""",
            (work_order_id, item['id']),
        ).fetchone()[0] or 0)
        cached = sync_reserved_stock(conn, item['id'])
        need = max(0.0, float(requirement['quantity']) - issued - already)
        available = max(0.0, float(item['current_stock']) - cached)
        if need <= 0:
            continue
        if available + 1e-9 < need:
            shortages.append({'item_no': item['item_no'], 'required': round(need, 3), 'available': round(available, 3)})
            continue
        number = next_no(conn, 'inventory_reservations', 'reservation_no', 'RSV-', 20001)
        conn.execute(
            """INSERT INTO inventory_reservations(
                 reservation_no,work_order_id,inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at,notes
               ) VALUES(?,?,?,?,0,'Reserved',?,?,?)""",
            (number, work_order_id, item['id'], need, actor_id, now(), 'Reserve all planned materials'),
        )
        sync_reserved_stock(conn, item['id'])
        created.append(number)
    audit(conn, actor_id, 'RESERVE ALL', 'Work Management', work['wo_no'], '', {'reservations': created, 'shortages': shortages})
    return {'created': created, 'shortages': shortages, 'readiness': work_order_parts_readiness(conn, work_order_id)}


def release_material_reservation(conn, reservation_id: int, actor_id: int) -> dict:
    _begin_write(conn)
    row = _lock_row(
        conn,
        '''SELECT r.*,w.wo_no,i.item_no FROM inventory_reservations r
           JOIN work_orders w ON w.id=r.work_order_id JOIN inventory_items i ON i.id=r.inventory_item_id
           WHERE r.id=?''',
        (reservation_id,),
    )
    if not row:
        raise ReservationNotFound('Reservation not found')
    reservation = dict(row)
    if reservation['status'] not in ('Reserved', 'Partially Issued'):
        raise ReservationConflict(f"Reservation is {reservation['status']}")
    cur = conn.execute(
        "UPDATE inventory_reservations SET status='Released',released_at=? WHERE id=? AND status=?",
        (now(), reservation_id, reservation['status']),
    )
    if cur.rowcount != 1:
        raise ReservationConflict('Reservation changed concurrently; reload and retry')
    sync_reserved_stock(conn, reservation['inventory_item_id'])
    requirement = conn.execute(
        'SELECT id FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',
        (reservation['work_order_id'], reservation['inventory_item_id']),
    ).fetchone()
    if requirement:
        conn.execute("UPDATE work_order_requirements SET status='Required' WHERE id=?", (requirement['id'],))
    audit(conn, actor_id, 'RELEASE RESERVATION', 'Inventory', reservation['reservation_no'], reservation['status'], 'Released')
    return {'ok': True, 'readiness': work_order_parts_readiness(conn, reservation['work_order_id'])}


def issue_material_reservation(conn, reservation_id: int, quantity: float | None, actor: dict) -> dict:
    _begin_write(conn)
    row = _lock_row(
        conn,
        '''SELECT r.*,w.wo_no,w.asset_id,w.assigned_to,i.item_no,i.name,i.unit,i.unit_price,i.current_stock
           FROM inventory_reservations r JOIN work_orders w ON w.id=r.work_order_id
           JOIN inventory_items i ON i.id=r.inventory_item_id WHERE r.id=?''',
        (reservation_id,),
    )
    if not row:
        raise ReservationNotFound('Reservation not found')
    reservation = dict(row)
    if actor['role'] == 'technician' and reservation.get('assigned_to') != actor['id']:
        raise ReservationForbidden('Technicians can only issue materials for work assigned to them')
    if reservation['status'] not in ('Reserved', 'Partially Issued'):
        raise ReservationConflict(f"Reservation is {reservation['status']}")
    remaining = max(0.0, float(reservation['quantity']) - float(reservation['issued_quantity']))
    qty = remaining if quantity is None else float(quantity)
    if qty <= 0:
        raise ReservationConflict('Issue quantity must be greater than zero')
    if qty > remaining + 1e-9:
        raise ReservationConflict(f'Reservation only has {remaining:g} remaining')
    item = _lock_row(conn, 'SELECT * FROM inventory_items WHERE id=?', (reservation['inventory_item_id'],))
    if float(item['current_stock']) + 1e-9 < qty:
        raise ReservationConflict('Physical stock is below reserved quantity')
    new_issued = float(reservation['issued_quantity']) + qty
    new_status = 'Issued' if new_issued + 1e-9 >= float(reservation['quantity']) else 'Partially Issued'
    claimed = conn.execute(
        'UPDATE inventory_reservations SET issued_quantity=?,status=? WHERE id=? AND status=? AND issued_quantity=?',
        (new_issued, new_status, reservation_id, reservation['status'], reservation['issued_quantity']),
    )
    if claimed.rowcount != 1:
        raise ReservationConflict('Reservation changed concurrently; issue was not applied')
    stock = conn.execute(
        'UPDATE inventory_items SET current_stock=current_stock-? WHERE id=? AND current_stock>=?',
        (qty, reservation['inventory_item_id'], qty),
    )
    if stock.rowcount != 1:
        raise ReservationConflict('Physical stock changed concurrently; issue was not applied')
    sync_reserved_stock(conn, reservation['inventory_item_id'])
    conn.execute(
        '''INSERT INTO inventory_transactions(item_id,tx_type,quantity,work_order_id,reference,user_id,created_at)
           VALUES(?,?,?,?,?,?,?)''',
        (reservation['inventory_item_id'], 'ISSUE', -qty, reservation['work_order_id'], reservation['reservation_no'], actor['id'], now()),
    )
    conn.execute(
        '''INSERT INTO work_order_materials(work_order_id,inventory_item_id,quantity,unit_cost,issued_at,issued_by)
           VALUES(?,?,?,?,?,?)''',
        (reservation['work_order_id'], reservation['inventory_item_id'], qty, reservation['unit_price'], now(), actor['id']),
    )
    cost = qty * float(reservation['unit_price'])
    conn.execute(
        'UPDATE work_orders SET actual_cost=actual_cost+?,updated_at=? WHERE id=?',
        (cost, now(), reservation['work_order_id']),
    )
    post_cost(
        conn,
        {'id': reservation['work_order_id'], 'asset_id': reservation.get('asset_id'), 'wo_no': reservation['wo_no']},
        'Material', cost, qty, reservation['item_no'], actor['id'],
    )
    requirement = conn.execute(
        'SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',
        (reservation['work_order_id'], reservation['inventory_item_id']),
    ).fetchone()
    if requirement:
        issued = conn.execute(
            'SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',
            (reservation['work_order_id'], reservation['inventory_item_id']),
        ).fetchone()[0] or 0
        conn.execute(
            'UPDATE work_order_requirements SET status=? WHERE id=?',
            ('Fulfilled' if float(issued) + 1e-9 >= float(requirement['quantity']) else 'Required', requirement['id']),
        )
    audit(
        conn, actor['id'], 'ISSUE RESERVATION', 'Inventory', reservation['reservation_no'], reservation['status'],
        {'status': new_status, 'issued': qty},
    )
    return {
        'ok': True, 'status': new_status, 'issued_quantity': qty,
        'readiness': work_order_parts_readiness(conn, reservation['work_order_id']),
    }
