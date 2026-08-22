from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import MaterialIn, ReservationIn, ReservationIssueIn
from app.database import db, now
from app.main import app
from app.reservation_store import (
    ReservationConcurrencyConflict,
    issue_material,
    issue_reservation,
    release_reservation,
    reserve_material,
)


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_item_and_work(conn, suffix: str, stock: float = 5.0):
    user = _admin(conn)
    warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
    assert warehouse
    item = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,category,warehouse_id,current_stock,reserved_stock,
             min_level,max_level,reorder_point,unit_price,unit
           ) VALUES(?,?,?,?,?,0,0,100,0,1,'ea')''',
        (
            f'ITM-RSV-{suffix}',
            'Reservation concurrency item',
            'CI',
            warehouse['id'],
            stock,
        ),
    )
    work = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,created_at,updated_at
           ) VALUES(?,?,'Medium','Assigned','Corrective Maintenance',?,?,?)''',
        (
            f'WO-RSV-{suffix}',
            'Reservation concurrency work',
            user['id'],
            now(),
            now(),
        ),
    )
    return user, int(item.lastrowid), int(work.lastrowid)


def test_concurrent_reservations_cannot_overreserve_stock():
    suffix = uuid.uuid4().hex[:10]
    stock = 5.0
    with TestClient(app):
        with db() as conn:
            user, item_id, work_id = _seed_item_and_work(conn, suffix, stock)

        barrier = threading.Barrier(WORKERS)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    reserve_material(
                        conn,
                        work_id,
                        ReservationIn(item_id=item_id, quantity=1, notes='race'),
                        user,
                    )
                wins.append(index)
            except ReservationConcurrencyConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == int(stock)
        assert len(conflicts) == WORKERS - int(stock)

        with db() as conn:
            item = conn.execute(
                'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
                (item_id,),
            ).fetchone()
            active = float(
                conn.execute(
                    """SELECT COALESCE(SUM(quantity-issued_quantity),0)
                       FROM inventory_reservations
                       WHERE inventory_item_id=?
                         AND status IN ('Reserved','Partially Issued')""",
                    (item_id,),
                ).fetchone()[0]
                or 0
            )

        assert float(item['current_stock']) == stock
        assert float(item['reserved_stock']) == stock
        assert active == stock
        assert 0 <= float(item['reserved_stock']) <= float(item['current_stock'])


def test_concurrent_issue_of_one_reservation_never_exceeds_reserved_quantity():
    suffix = uuid.uuid4().hex[:10]
    with TestClient(app):
        with db() as conn:
            user, item_id, work_id = _seed_item_and_work(conn, suffix, 10)
            reservation = reserve_material(
                conn,
                work_id,
                ReservationIn(item_id=item_id, quantity=3, notes='issue race'),
                user,
            )
            reservation_id = int(reservation['id'])

        barrier = threading.Barrier(WORKERS)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    issue_reservation(
                        conn,
                        reservation_id,
                        ReservationIssueIn(quantity=1),
                        user,
                    )
                wins.append(index)
            except ReservationConcurrencyConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 3
        assert len(conflicts) == WORKERS - 3

        with db() as conn:
            reservation = conn.execute(
                'SELECT * FROM inventory_reservations WHERE id=?', (reservation_id,)
            ).fetchone()
            item = conn.execute(
                'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
                (item_id,),
            ).fetchone()
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE reference=? AND tx_type='ISSUE'""",
                    (reservation['reservation_no'],),
                ).fetchone()[0]
            )
            material_sum = float(
                conn.execute(
                    '''SELECT COALESCE(SUM(quantity),0) FROM work_order_materials
                       WHERE work_order_id=? AND inventory_item_id=?''',
                    (work_id, item_id),
                ).fetchone()[0]
                or 0
            )

        assert float(reservation['issued_quantity']) == 3
        assert reservation['status'] == 'Issued'
        assert float(item['current_stock']) == 7
        assert float(item['reserved_stock']) == 0
        assert tx_count == 3
        assert material_sum == 3


def test_release_racing_full_issue_has_one_coherent_terminal_outcome():
    suffix = uuid.uuid4().hex[:10]
    with TestClient(app):
        with db() as conn:
            user, item_id, work_id = _seed_item_and_work(conn, suffix, 5)
            reservation_id = int(
                reserve_material(
                    conn,
                    work_id,
                    ReservationIn(item_id=item_id, quantity=2, notes='release race'),
                    user,
                )['id']
            )

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def issue_worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    issue_reservation(
                        conn,
                        reservation_id,
                        ReservationIssueIn(quantity=2),
                        user,
                    )
                outcomes.append('issued')
            except ReservationConcurrencyConflict:
                outcomes.append('issue_conflict')
            except BaseException as exc:
                errors.append(exc)

        def release_worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    release_reservation(conn, reservation_id, user)
                outcomes.append('released')
            except ReservationConcurrencyConflict:
                outcomes.append('release_conflict')
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=issue_worker), threading.Thread(target=release_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert errors == []
        assert not any(thread.is_alive() for thread in threads)
        assert sum(x in ('issued', 'released') for x in outcomes) == 1

        with db() as conn:
            reservation = conn.execute(
                'SELECT * FROM inventory_reservations WHERE id=?', (reservation_id,)
            ).fetchone()
            item = conn.execute(
                'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
                (item_id,),
            ).fetchone()

        if reservation['status'] == 'Issued':
            assert float(reservation['issued_quantity']) == 2
            assert float(item['current_stock']) == 3
        else:
            assert reservation['status'] == 'Released'
            assert float(reservation['issued_quantity']) == 0
            assert float(item['current_stock']) == 5
        assert float(item['reserved_stock']) == 0


def test_concurrent_direct_material_issue_uses_fresh_accessible_stock():
    suffix = uuid.uuid4().hex[:10]
    with TestClient(app):
        with db() as conn:
            user, item_id, work_id = _seed_item_and_work(conn, suffix, 5)

        barrier = threading.Barrier(2)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    issue_material(
                        conn,
                        work_id,
                        MaterialIn(item_id=item_id, quantity=4),
                        user,
                    )
                wins.append(index)
            except ReservationConcurrencyConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert errors == []
        assert len(wins) == 1
        assert len(conflicts) == 1
        with db() as conn:
            item = conn.execute(
                'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
                (item_id,),
            ).fetchone()
            tx_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM inventory_transactions
                       WHERE item_id=? AND work_order_id=? AND tx_type='ISSUE'""",
                    (item_id, work_id),
                ).fetchone()[0]
            )
        assert float(item['current_stock']) == 1
        assert float(item['reserved_stock']) == 0
        assert tx_count == 1
