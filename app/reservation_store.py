from __future__ import annotations

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now


EPSILON = 1e-9


class ReservationConcurrencyConflict(RuntimeError):
    """Raised when stock/reservation state no longer permits an operation."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def lock_inventory_item(conn, item_id: int):
    """Acquire the canonical per-item mutation lock and return fresh item state.

    PostgreSQL obtains a row lock through the no-op UPDATE until transaction
    completion. SQLite already serializes writers. Every reservation/issue path
    uses item -> reservation(s) -> audit as its lock order.
    """
    locked = conn.execute(
        'UPDATE inventory_items SET reserved_stock=reserved_stock WHERE id=?',
        (item_id,),
    )
    if not _rowcount_one(locked):
        raise KeyError('Inventory item not found')
    return conn.execute(
        'SELECT * FROM inventory_items WHERE id=?', (item_id,)
    ).fetchone()


def _sync_reserved_stock_locked(conn, item_id: int) -> float:
    reserved = conn.execute(
        """SELECT COALESCE(SUM(quantity-issued_quantity),0)
           FROM inventory_reservations
           WHERE inventory_item_id=?
             AND status IN ('Reserved','Partially Issued')""",
        (item_id,),
    ).fetchone()[0] or 0
    reserved = max(0.0, float(reserved))
    conn.execute(
        'UPDATE inventory_items SET reserved_stock=? WHERE id=?',
        (reserved, item_id),
    )
    return reserved


def reserve_material(conn, wo_id: int, body, user: dict) -> dict:
    work = conn.execute('SELECT * FROM work_orders WHERE id=?', (wo_id,)).fetchone()
    if not work:
        raise KeyError('Work order not found')

    item = lock_inventory_item(conn, body.item_id)
    available = float(item['current_stock']) - float(item['reserved_stock'])
    quantity = float(body.quantity)
    if available + EPSILON < quantity:
        raise ReservationConcurrencyConflict(
            f"Insufficient unreserved stock ({available:g} {item['unit']})"
        )

    number = _application.next_no(
        conn, 'inventory_reservations', 'reservation_no', 'RSV-', 20001
    )
    created = conn.execute(
        """INSERT INTO inventory_reservations(
             reservation_no,work_order_id,inventory_item_id,quantity,
             issued_quantity,status,reserved_by,reserved_at,notes
           ) VALUES(?,?,?,?,0,'Reserved',?,?,?)""",
        (
            number,
            wo_id,
            body.item_id,
            quantity,
            user['id'],
            now(),
            body.notes,
        ),
    )
    conn.execute(
        'UPDATE inventory_items SET reserved_stock=reserved_stock+? WHERE id=?',
        (quantity, body.item_id),
    )

    requirement = conn.execute(
        '''SELECT id,quantity FROM work_order_requirements
           WHERE work_order_id=? AND inventory_item_id=?''',
        (wo_id, body.item_id),
    ).fetchone()
    if requirement:
        reserved = conn.execute(
            """SELECT COALESCE(SUM(quantity-issued_quantity),0)
               FROM inventory_reservations
               WHERE work_order_id=? AND inventory_item_id=?
                 AND status IN ('Reserved','Partially Issued')""",
            (wo_id, body.item_id),
        ).fetchone()[0] or 0
        if float(reserved) + EPSILON >= float(requirement['quantity']):
            conn.execute(
                "UPDATE work_order_requirements SET status='Reserved' WHERE id=?",
                (requirement['id'],),
            )

    append_audit(
        conn,
        user['id'],
        'RESERVE MATERIAL',
        'Work Management',
        work['wo_no'],
        '',
        {
            'reservation': number,
            'item': item['item_no'],
            'quantity': quantity,
        },
    )
    _application.notify(
        conn,
        'Material reserved',
        f"{number} reserved {quantity:g} {item['unit']} of {item['item_no']} for {work['wo_no']}",
        'Info',
        work['assigned_to'],
        None,
        'work',
        work['wo_no'],
    )
    return {
        'id': int(created.lastrowid),
        'reservation_no': number,
        'readiness': _application._work_order_parts_readiness(conn, wo_id),
    }


def reserve_all_materials(conn, wo_id: int, user: dict) -> dict:
    work = conn.execute('SELECT * FROM work_orders WHERE id=?', (wo_id,)).fetchone()
    if not work:
        raise KeyError('Work order not found')

    requirements = conn.execute(
        """SELECT * FROM work_order_requirements
           WHERE work_order_id=? AND status<>'Cancelled'
           ORDER BY inventory_item_id,id""",
        (wo_id,),
    ).fetchall()

    # Lock every participating item in deterministic ID order before computing
    # availability. Concurrent reserve-all operations cannot deadlock by taking
    # the same inventory rows in different orders.
    item_ids = sorted({int(req['inventory_item_id']) for req in requirements})
    for item_id in item_ids:
        lock_inventory_item(conn, item_id)

    created: list[str] = []
    shortages: list[dict] = []
    for requirement in requirements:
        item = conn.execute(
            'SELECT * FROM inventory_items WHERE id=?',
            (requirement['inventory_item_id'],),
        ).fetchone()
        if not item:
            raise KeyError('Inventory item not found')

        issued = float(
            conn.execute(
                '''SELECT COALESCE(SUM(quantity),0) FROM work_order_materials
                   WHERE work_order_id=? AND inventory_item_id=?''',
                (wo_id, item['id']),
            ).fetchone()[0]
            or 0
        )
        already = float(
            conn.execute(
                """SELECT COALESCE(SUM(quantity-issued_quantity),0)
                   FROM inventory_reservations
                   WHERE work_order_id=? AND inventory_item_id=?
                     AND status IN ('Reserved','Partially Issued')""",
                (wo_id, item['id']),
            ).fetchone()[0]
            or 0
        )
        need = max(0.0, float(requirement['quantity']) - issued - already)
        available = max(
            0.0, float(item['current_stock']) - float(item['reserved_stock'])
        )
        if need <= EPSILON:
            continue
        if available + EPSILON < need:
            shortages.append(
                {
                    'item_no': item['item_no'],
                    'required': round(need, 3),
                    'available': round(available, 3),
                }
            )
            continue

        number = _application.next_no(
            conn, 'inventory_reservations', 'reservation_no', 'RSV-', 20001
        )
        conn.execute(
            """INSERT INTO inventory_reservations(
                 reservation_no,work_order_id,inventory_item_id,quantity,
                 issued_quantity,status,reserved_by,reserved_at,notes
               ) VALUES(?,?,?,?,0,'Reserved',?,?,?)""",
            (
                number,
                wo_id,
                item['id'],
                need,
                user['id'],
                now(),
                'Reserve all planned materials',
            ),
        )
        conn.execute(
            'UPDATE inventory_items SET reserved_stock=reserved_stock+? WHERE id=?',
            (need, item['id']),
        )
        created.append(number)

    append_audit(
        conn,
        user['id'],
        'RESERVE ALL',
        'Work Management',
        work['wo_no'],
        '',
        {'reservations': created, 'shortages': shortages},
    )
    return {
        'created': created,
        'shortages': shortages,
        'readiness': _application._work_order_parts_readiness(conn, wo_id),
    }


def release_reservation(conn, reservation_id: int, user: dict) -> dict:
    initial = conn.execute(
        'SELECT inventory_item_id FROM inventory_reservations WHERE id=?',
        (reservation_id,),
    ).fetchone()
    if not initial:
        raise KeyError('Reservation not found')

    lock_inventory_item(conn, int(initial['inventory_item_id']))
    reservation = conn.execute(
        '''SELECT r.*,w.wo_no,i.item_no
           FROM inventory_reservations r
           JOIN work_orders w ON w.id=r.work_order_id
           JOIN inventory_items i ON i.id=r.inventory_item_id
           WHERE r.id=?''',
        (reservation_id,),
    ).fetchone()
    if not reservation:
        raise KeyError('Reservation not found')
    if reservation['status'] not in ('Reserved', 'Partially Issued'):
        raise ReservationConcurrencyConflict(
            f"Reservation is {reservation['status']}"
        )

    changed = conn.execute(
        """UPDATE inventory_reservations
           SET status='Released',released_at=?
           WHERE id=? AND status IN ('Reserved','Partially Issued')
             AND issued_quantity=?""",
        (now(), reservation_id, reservation['issued_quantity']),
    )
    if not _rowcount_one(changed):
        raise ReservationConcurrencyConflict('Reservation changed concurrently')

    _sync_reserved_stock_locked(conn, reservation['inventory_item_id'])
    requirement = conn.execute(
        '''SELECT id FROM work_order_requirements
           WHERE work_order_id=? AND inventory_item_id=?''',
        (reservation['work_order_id'], reservation['inventory_item_id']),
    ).fetchone()
    if requirement:
        conn.execute(
            "UPDATE work_order_requirements SET status='Required' WHERE id=?",
            (requirement['id'],),
        )

    append_audit(
        conn,
        user['id'],
        'RELEASE RESERVATION',
        'Inventory',
        reservation['reservation_no'],
        reservation['status'],
        'Released',
    )
    return {
        'ok': True,
        'readiness': _application._work_order_parts_readiness(
            conn, reservation['work_order_id']
        ),
    }


def issue_reservation(conn, reservation_id: int, body, user: dict) -> dict:
    initial = conn.execute(
        '''SELECT r.inventory_item_id,r.work_order_id,w.assigned_to
           FROM inventory_reservations r
           JOIN work_orders w ON w.id=r.work_order_id
           WHERE r.id=?''',
        (reservation_id,),
    ).fetchone()
    if not initial:
        raise KeyError('Reservation not found')
    if user['role'] == 'technician' and initial['assigned_to'] != user['id']:
        raise HTTPException(
            403, 'Technicians can only issue materials for work assigned to them'
        )

    item = lock_inventory_item(conn, int(initial['inventory_item_id']))
    reservation = conn.execute(
        '''SELECT r.*,w.wo_no,w.asset_id,w.assigned_to,
                  i.item_no,i.name,i.unit,i.unit_price,i.current_stock
           FROM inventory_reservations r
           JOIN work_orders w ON w.id=r.work_order_id
           JOIN inventory_items i ON i.id=r.inventory_item_id
           WHERE r.id=?''',
        (reservation_id,),
    ).fetchone()
    if not reservation:
        raise KeyError('Reservation not found')
    if reservation['status'] not in ('Reserved', 'Partially Issued'):
        raise ReservationConcurrencyConflict(
            f"Reservation is {reservation['status']}"
        )

    remaining = max(
        0.0, float(reservation['quantity']) - float(reservation['issued_quantity'])
    )
    quantity = remaining if body.quantity is None else float(body.quantity)
    if quantity <= 0 or quantity > remaining + EPSILON:
        raise ReservationConcurrencyConflict(
            f'Reservation only has {remaining:g} remaining'
        )
    if float(item['current_stock']) + EPSILON < quantity:
        raise ReservationConcurrencyConflict(
            'Physical stock is below reserved quantity'
        )

    new_issued = float(reservation['issued_quantity']) + quantity
    new_status = (
        'Issued'
        if new_issued + EPSILON >= float(reservation['quantity'])
        else 'Partially Issued'
    )
    claimed = conn.execute(
        '''UPDATE inventory_reservations
           SET issued_quantity=?,status=?
           WHERE id=? AND issued_quantity=?
             AND status IN ('Reserved','Partially Issued')''',
        (
            new_issued,
            new_status,
            reservation_id,
            reservation['issued_quantity'],
        ),
    )
    if not _rowcount_one(claimed):
        raise ReservationConcurrencyConflict('Reservation changed concurrently')

    stock = conn.execute(
        '''UPDATE inventory_items
           SET current_stock=current_stock-?
           WHERE id=? AND current_stock>=?''',
        (quantity, reservation['inventory_item_id'], quantity),
    )
    if not _rowcount_one(stock):
        raise ReservationConcurrencyConflict(
            'Physical stock changed before reserved material could be issued'
        )
    _sync_reserved_stock_locked(conn, reservation['inventory_item_id'])

    cost = quantity * float(reservation['unit_price'])
    conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,work_order_id,reference,user_id,created_at
           ) VALUES(?,?,?,?,?,?,?)''',
        (
            reservation['inventory_item_id'],
            'ISSUE',
            -quantity,
            reservation['work_order_id'],
            reservation['reservation_no'],
            user['id'],
            now(),
        ),
    )
    conn.execute(
        '''INSERT INTO work_order_materials(
             work_order_id,inventory_item_id,quantity,unit_cost,issued_at,issued_by
           ) VALUES(?,?,?,?,?,?)''',
        (
            reservation['work_order_id'],
            reservation['inventory_item_id'],
            quantity,
            reservation['unit_price'],
            now(),
            user['id'],
        ),
    )
    conn.execute(
        '''UPDATE work_orders
           SET actual_cost=actual_cost+?,updated_at=? WHERE id=?''',
        (cost, now(), reservation['work_order_id']),
    )
    _application.post_cost(
        conn,
        {
            'id': reservation['work_order_id'],
            'asset_id': reservation['asset_id'],
            'wo_no': reservation['wo_no'],
        },
        'Material',
        cost,
        quantity,
        reservation['item_no'],
        user['id'],
    )

    requirement = conn.execute(
        '''SELECT id,quantity FROM work_order_requirements
           WHERE work_order_id=? AND inventory_item_id=?''',
        (reservation['work_order_id'], reservation['inventory_item_id']),
    ).fetchone()
    if requirement:
        issued = conn.execute(
            '''SELECT COALESCE(SUM(quantity),0) FROM work_order_materials
               WHERE work_order_id=? AND inventory_item_id=?''',
            (reservation['work_order_id'], reservation['inventory_item_id']),
        ).fetchone()[0] or 0
        conn.execute(
            'UPDATE work_order_requirements SET status=? WHERE id=?',
            (
                'Fulfilled'
                if float(issued) + EPSILON >= float(requirement['quantity'])
                else 'Required',
                requirement['id'],
            ),
        )

    append_audit(
        conn,
        user['id'],
        'ISSUE RESERVATION',
        'Inventory',
        reservation['reservation_no'],
        reservation['status'],
        {'status': new_status, 'issued': quantity},
    )
    return {
        'ok': True,
        'status': new_status,
        'issued_quantity': quantity,
        'readiness': _application._work_order_parts_readiness(
            conn, reservation['work_order_id']
        ),
    }


def issue_material(conn, wo_id: int, body, user: dict) -> dict:
    work = conn.execute('SELECT * FROM work_orders WHERE id=?', (wo_id,)).fetchone()
    if not work:
        raise KeyError('Work order not found')
    if user['role'] == 'technician' and work['assigned_to'] != user['id']:
        raise HTTPException(
            403, 'Technicians can only issue materials for work assigned to them'
        )

    item = lock_inventory_item(conn, body.item_id)
    own = conn.execute(
        """SELECT * FROM inventory_reservations
           WHERE work_order_id=? AND inventory_item_id=?
             AND status IN ('Reserved','Partially Issued')
           ORDER BY id""",
        (wo_id, body.item_id),
    ).fetchall()
    own_reserved = sum(
        max(0.0, float(row['quantity']) - float(row['issued_quantity']))
        for row in own
    )
    unreserved = max(
        0.0, float(item['current_stock']) - float(item['reserved_stock'])
    )
    quantity = float(body.quantity)
    accessible = own_reserved + unreserved
    if accessible + EPSILON < quantity:
        raise ReservationConcurrencyConflict(
            f"Insufficient accessible stock ({accessible:g} {item['unit']}; "
            f'{own_reserved:g} reserved for this work order)'
        )
    if float(item['current_stock']) + EPSILON < quantity:
        raise ReservationConcurrencyConflict('Physical stock is insufficient')

    remaining = quantity
    for reservation in own:
        if remaining <= EPSILON:
            break
        balance = max(
            0.0,
            float(reservation['quantity']) - float(reservation['issued_quantity']),
        )
        take = min(balance, remaining)
        if take <= EPSILON:
            continue
        new_issued = float(reservation['issued_quantity']) + take
        status = (
            'Issued'
            if new_issued + EPSILON >= float(reservation['quantity'])
            else 'Partially Issued'
        )
        changed = conn.execute(
            '''UPDATE inventory_reservations
               SET issued_quantity=?,status=?
               WHERE id=? AND issued_quantity=?
                 AND status IN ('Reserved','Partially Issued')''',
            (
                new_issued,
                status,
                reservation['id'],
                reservation['issued_quantity'],
            ),
        )
        if not _rowcount_one(changed):
            raise ReservationConcurrencyConflict('Reservation changed concurrently')
        remaining -= take

    stock = conn.execute(
        '''UPDATE inventory_items SET current_stock=current_stock-?
           WHERE id=? AND current_stock>=?''',
        (quantity, body.item_id, quantity),
    )
    if not _rowcount_one(stock):
        raise ReservationConcurrencyConflict('Physical stock changed concurrently')
    _sync_reserved_stock_locked(conn, body.item_id)

    cost = quantity * float(item['unit_price'])
    conn.execute(
        '''INSERT INTO inventory_transactions(
             item_id,tx_type,quantity,work_order_id,reference,user_id,created_at
           ) VALUES(?,?,?,?,?,?,?)''',
        (body.item_id, 'ISSUE', -quantity, wo_id, work['wo_no'], user['id'], now()),
    )
    conn.execute(
        '''INSERT INTO work_order_materials(
             work_order_id,inventory_item_id,quantity,unit_cost,issued_at,issued_by
           ) VALUES(?,?,?,?,?,?)''',
        (wo_id, body.item_id, quantity, item['unit_price'], now(), user['id']),
    )
    conn.execute(
        '''UPDATE work_orders SET actual_cost=actual_cost+?,updated_at=?
           WHERE id=?''',
        (cost, now(), wo_id),
    )
    _application.post_cost(
        conn,
        dict(work),
        'Material',
        cost,
        quantity,
        item['item_no'],
        user['id'],
    )

    requirement = conn.execute(
        '''SELECT id,quantity FROM work_order_requirements
           WHERE work_order_id=? AND inventory_item_id=?''',
        (wo_id, body.item_id),
    ).fetchone()
    if requirement:
        issued = conn.execute(
            '''SELECT COALESCE(SUM(quantity),0) FROM work_order_materials
               WHERE work_order_id=? AND inventory_item_id=?''',
            (wo_id, body.item_id),
        ).fetchone()[0] or 0
        conn.execute(
            'UPDATE work_order_requirements SET status=? WHERE id=?',
            (
                'Fulfilled'
                if float(issued) + EPSILON >= float(requirement['quantity'])
                else 'Required',
                requirement['id'],
            ),
        )

    append_audit(
        conn,
        user['id'],
        'ISSUE MATERIAL',
        'Work Management',
        work['wo_no'],
        '',
        {
            'item': item['item_no'],
            'qty': quantity,
            'cost': cost,
            'reservation_consumed': round(quantity - remaining, 3),
        },
    )
    fresh = conn.execute(
        '''SELECT current_stock,reserved_stock,reorder_point
           FROM inventory_items WHERE id=?''',
        (body.item_id,),
    ).fetchone()
    new_stock = float(fresh['current_stock'])
    if new_stock - float(fresh['reserved_stock']) <= float(fresh['reorder_point']):
        _application.notify(
            conn,
            'Inventory below reorder point',
            f"{item['item_no']} — {item['name']} has {new_stock:g} {item['unit']} remaining",
            'Warning',
            None,
            'storekeeper',
            'inventory',
            item['item_no'],
        )
    return {
        'ok': True,
        'stock': new_stock,
        'cost': cost,
        'readiness': _application._work_order_parts_readiness(conn, wo_id),
    }


def install_reservation_routes() -> None:
    app = _application.app
    marker = '_euas_reservation_concurrency_hardening'
    if getattr(app.state, marker, False):
        return

    replacements = {
        ('/api/work-orders/{wo_id}/reservations', 'POST'),
        ('/api/work-orders/{wo_id}/reserve-all', 'POST'),
        ('/api/reservations/{reservation_id}/release', 'POST'),
        ('/api/reservations/{reservation_id}/issue', 'POST'),
        ('/api/work-orders/{wo_id}/materials', 'POST'),
    }
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and method in set(getattr(route, 'methods', set()) or set())
            for path, method in replacements
        )
    ]

    reserve_roles = ('admin', 'maintenance_manager', 'planner', 'supervisor', 'storekeeper')

    @app.post('/api/work-orders/{wo_id}/reservations')
    def reserve_route(
        wo_id: int,
        body: _application.ReservationIn,
        user=Depends(require_roles(*reserve_roles)),
    ):
        try:
            with db() as conn:
                return reserve_material(conn, wo_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ReservationConcurrencyConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/work-orders/{wo_id}/reserve-all')
    def reserve_all_route(
        wo_id: int,
        user=Depends(require_roles(*reserve_roles)),
    ):
        try:
            with db() as conn:
                return reserve_all_materials(conn, wo_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ReservationConcurrencyConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/reservations/{reservation_id}/release')
    def release_route(
        reservation_id: int,
        user=Depends(require_roles(*reserve_roles)),
    ):
        try:
            with db() as conn:
                return release_reservation(conn, reservation_id, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ReservationConcurrencyConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/reservations/{reservation_id}/issue')
    def issue_reservation_route(
        reservation_id: int,
        body: _application.ReservationIssueIn,
        user=Depends(require_roles(*_application.INV_ROLES)),
    ):
        try:
            with db() as conn:
                return issue_reservation(conn, reservation_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ReservationConcurrencyConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/work-orders/{wo_id}/materials')
    def issue_material_route(
        wo_id: int,
        body: _application.MaterialIn,
        user=Depends(require_roles(*_application.INV_ROLES)),
    ):
        try:
            with db() as conn:
                return issue_material(conn, wo_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ReservationConcurrencyConflict as exc:
            raise HTTPException(409, str(exc))

    for name, handler in (
        ('reserve_work_material', reserve_route),
        ('reserve_all_work_materials', reserve_all_route),
        ('release_reservation', release_route),
        ('issue_reservation', issue_reservation_route),
        ('add_material', issue_material_route),
    ):
        setattr(_application, name, handler)

    app.openapi_schema = None
    setattr(app.state, marker, True)
