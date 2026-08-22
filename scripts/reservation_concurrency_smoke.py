from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import MaterialIn, ReservationIn, ReservationIssueIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.reservation_store import (
    ReservationConcurrencyConflict,
    issue_material,
    issue_reservation,
    release_reservation,
    reserve_material,
)


WORKERS = 8


def race(operation, workers=WORKERS):
    barrier = threading.Barrier(workers)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            operation(index)
            wins.append(index)
        except ReservationConcurrencyConflict:
            conflicts.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('reservation concurrency worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'reservation concurrency worker failed: {errors!r}')
    return wins, conflicts


def seed_item_work(suffix: str, stock: float):
    with db() as conn:
        ensure_audit_chain_lock(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
        if not user or not warehouse:
            raise RuntimeError('reservation smoke requires seeded admin and warehouse')
        item = conn.execute(
            '''INSERT INTO inventory_items(
                 item_no,name,category,warehouse_id,current_stock,reserved_stock,
                 min_level,max_level,reorder_point,unit_price,unit
               ) VALUES(?,?,?,?,?,0,0,100,0,1,'ea')''',
            (
                f'ITM-PG-RSV-{suffix}',
                'PostgreSQL reservation race item',
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
                f'WO-PG-RSV-{suffix}',
                'PostgreSQL reservation race work',
                user['id'],
                now(),
                now(),
            ),
        )
        return dict(user), int(item.lastrowid), int(work.lastrowid)


def main() -> None:
    suffix = uuid.uuid4().hex[:10]

    # Eight writers compete for five units. Exactly five may reserve one unit.
    user, item_id, work_id = seed_item_work(suffix + 'A', 5)

    def reserve_once(index: int) -> None:
        with db() as conn:
            reserve_material(
                conn,
                work_id,
                ReservationIn(item_id=item_id, quantity=1, notes=f'worker {index}'),
                user,
            )

    wins, conflicts = race(reserve_once)
    if len(wins) != 5 or len(conflicts) != WORKERS - 5:
        raise RuntimeError(
            f'over-reservation race expected 5 wins; got wins={len(wins)} conflicts={len(conflicts)}'
        )
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
    if float(item['current_stock']) != 5 or float(item['reserved_stock']) != 5 or active != 5:
        raise RuntimeError(
            f'over-reservation invariant failed: stock={item["current_stock"]} '
            f'reserved={item["reserved_stock"]} active={active}'
        )

    # Eight writers issue a three-unit reservation one unit at a time. Only
    # three commits may succeed and physical stock must drop exactly three.
    user, item_id, work_id = seed_item_work(suffix + 'B', 10)
    with db() as conn:
        reservation_id = int(
            reserve_material(
                conn,
                work_id,
                ReservationIn(item_id=item_id, quantity=3, notes='issue race'),
                user,
            )['id']
        )

    def issue_once(index: int) -> None:
        with db() as conn:
            issue_reservation(
                conn,
                reservation_id,
                ReservationIssueIn(quantity=1),
                user,
            )

    wins, conflicts = race(issue_once)
    if len(wins) != 3 or len(conflicts) != WORKERS - 3:
        raise RuntimeError(
            f'reservation issue expected 3 wins; wins={len(wins)} conflicts={len(conflicts)}'
        )
    with db() as conn:
        reservation = conn.execute(
            'SELECT * FROM inventory_reservations WHERE id=?', (reservation_id,)
        ).fetchone()
        item = conn.execute(
            'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
            (item_id,),
        ).fetchone()
        issued_sum = float(
            conn.execute(
                '''SELECT COALESCE(SUM(quantity),0) FROM work_order_materials
                   WHERE work_order_id=? AND inventory_item_id=?''',
                (work_id, item_id),
            ).fetchone()[0]
            or 0
        )
    if (
        float(reservation['issued_quantity']) != 3
        or reservation['status'] != 'Issued'
        or float(item['current_stock']) != 7
        or float(item['reserved_stock']) != 0
        or issued_sum != 3
    ):
        raise RuntimeError(
            'reservation issue invariant failed: '
            f'reservation={dict(reservation)!r} item={dict(item)!r} issued_sum={issued_sum}'
        )

    # Full issue racing release must have one coherent terminal winner.
    user, item_id, work_id = seed_item_work(suffix + 'C', 5)
    with db() as conn:
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

    def issue_full() -> None:
        try:
            barrier.wait(timeout=15)
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

    def release_full() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                release_reservation(conn, reservation_id, user)
            outcomes.append('released')
        except ReservationConcurrencyConflict:
            outcomes.append('release_conflict')
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=issue_full), threading.Thread(target=release_full)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('release/issue race deadlocked')
    if errors or sum(x in ('issued', 'released') for x in outcomes) != 1:
        raise RuntimeError(f'release/issue race invalid: outcomes={outcomes} errors={errors!r}')
    with db() as conn:
        reservation = conn.execute(
            'SELECT * FROM inventory_reservations WHERE id=?', (reservation_id,)
        ).fetchone()
        item = conn.execute(
            'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
            (item_id,),
        ).fetchone()
    if float(item['reserved_stock']) != 0:
        raise RuntimeError(f'release/issue left reserved stock: {dict(item)!r}')
    if reservation['status'] == 'Issued':
        if float(item['current_stock']) != 3 or float(reservation['issued_quantity']) != 2:
            raise RuntimeError('issued winner produced inconsistent stock/reservation')
    elif reservation['status'] == 'Released':
        if float(item['current_stock']) != 5 or float(reservation['issued_quantity']) != 0:
            raise RuntimeError('release winner produced inconsistent stock/reservation')
    else:
        raise RuntimeError(f'unexpected terminal reservation status {reservation["status"]}')

    # Direct material issue must also serialize on the item row and use fresh
    # accessible stock rather than a stale current/reserved snapshot.
    user, item_id, work_id = seed_item_work(suffix + 'D', 5)

    def direct_issue(index: int) -> None:
        with db() as conn:
            issue_material(
                conn,
                work_id,
                MaterialIn(item_id=item_id, quantity=4),
                user,
            )

    wins, conflicts = race(direct_issue, workers=2)
    if len(wins) != 1 or len(conflicts) != 1:
        raise RuntimeError(
            f'direct material issue expected one winner; wins={wins} conflicts={conflicts}'
        )
    with db() as conn:
        item = conn.execute(
            'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
            (item_id,),
        ).fetchone()
        transactions = int(
            conn.execute(
                """SELECT COUNT(*) FROM inventory_transactions
                   WHERE item_id=? AND work_order_id=? AND tx_type='ISSUE'""",
                (item_id, work_id),
            ).fetchone()[0]
        )
    if float(item['current_stock']) != 1 or float(item['reserved_stock']) != 0 or transactions != 1:
        raise RuntimeError(
            f'direct issue invariant failed: item={dict(item)!r} transactions={transactions}'
        )

    print(
        'reservation concurrency smoke: PASS '
        'overreserve=blocked issue_limit=enforced release_issue=coherent direct_issue=serialized'
    )


if __name__ == '__main__':
    main()
