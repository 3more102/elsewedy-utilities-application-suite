from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import InventoryTxIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.transfer_store import (
    InventoryTransferConflict,
    InventoryTransferIdempotencyConflict,
    ensure_transfer_support,
    transfer_inventory_atomic,
)


WORKERS = 8


def admin_user() -> dict:
    with db() as conn:
        ensure_audit_chain_lock(conn)
        ensure_transfer_support(conn)
        row = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not row:
            raise RuntimeError('transfer smoke requires seeded admin')
        return dict(row)


def warehouses(conn, suffix: str, count: int) -> list[int]:
    site = conn.execute('SELECT id FROM sites ORDER BY id LIMIT 1').fetchone()
    if not site:
        raise RuntimeError('transfer smoke requires seeded site')
    result = []
    for index in range(count):
        row = conn.execute(
            '''INSERT INTO warehouses(warehouse_code,name,site_id,status)
               VALUES(?,?,?,'Active')''',
            (
                f'WH-PG-TX-{suffix}-{index}',
                f'PG Transfer Warehouse {suffix}-{index}',
                site['id'],
            ),
        )
        result.append(int(row.lastrowid))
    return result


def item(conn, suffix: str, warehouse_id: int, stock: float) -> int:
    row = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,
             reserved_stock,min_level,max_level,reorder_point,unit_price,unit,bin
           ) VALUES(?,?,?,?,?,?,0,0,100,0,1,'ea','CI')''',
        (
            f'ITM-PG-TX-{suffix}',
            'PostgreSQL Transfer Part',
            'transfer concurrency smoke',
            'CI-PG-TRANSFER',
            warehouse_id,
            stock,
        ),
    )
    return int(row.lastrowid)


def stock(conn, item_id: int) -> float:
    return float(
        conn.execute(
            'SELECT current_stock FROM inventory_items WHERE id=?', (item_id,)
        ).fetchone()['current_stock']
    )


def run_race(operation, workers: int, conflict_types=()):
    barrier = threading.Barrier(workers)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            operation(index)
            wins.append(index)
        except conflict_types:
            conflicts.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('transfer concurrency worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'transfer concurrency worker failed: {errors!r}')
    return wins, conflicts


def opposite_direction_round(user: dict, suffix: str, round_no: int) -> None:
    with db() as conn:
        wh_a, wh_b = warehouses(conn, f'{suffix}-OP-{round_no}', 2)
        a = item(conn, f'{suffix}-OP-{round_no}-A', wh_a, 10)
        b = item(conn, f'{suffix}-OP-{round_no}-B', wh_b, 10)

    reference = f'pg-opposite-{suffix}-{round_no}'

    def move(index: int) -> None:
        source, target, quantity = (
            (a, wh_b, 3.0) if index == 0 else (b, wh_a, 4.0)
        )
        with db() as conn:
            transfer_inventory_atomic(
                conn,
                source,
                InventoryTxIn(
                    tx_type='TRANSFER',
                    quantity=quantity,
                    to_warehouse_id=target,
                    reference=reference,
                ),
                user,
            )

    wins, conflicts = run_race(move, 2)
    if len(wins) != 2 or conflicts:
        raise RuntimeError(
            f'opposite transfer round {round_no} did not complete twice: '
            f'wins={wins} conflicts={conflicts}'
        )
    with db() as conn:
        sa, sb = stock(conn, a), stock(conn, b)
        tx_rows = int(
            conn.execute(
                """SELECT COUNT(*) FROM inventory_transactions
                   WHERE reference=? AND tx_type='TRANSFER'""",
                (reference,),
            ).fetchone()[0]
        )
    if sa != 11 or sb != 9 or sa + sb != 20 or tx_rows != 4:
        raise RuntimeError(
            f'opposite transfer conservation failed: a={sa} b={sb} rows={tx_rows}'
        )


def main() -> None:
    user = admin_user()
    suffix = uuid.uuid4().hex[:10]

    # Repeat opposite-direction contention so A->B and B->A repeatedly overlap
    # on the exact same two rows. Canonical ID ordering must prevent deadlock.
    for round_no in range(6):
        opposite_direction_round(user, suffix, round_no)

    # Aggregate outbound demand exceeds source stock: at most one 8-unit move.
    with db() as conn:
        wh_a, wh_b, wh_c = warehouses(conn, f'{suffix}-OD', 3)
        source = item(conn, f'{suffix}-OD-S', wh_a, 10)
        dest_b = item(conn, f'{suffix}-OD-B', wh_b, 0)
        dest_c = item(conn, f'{suffix}-OD-C', wh_c, 0)
    reference = f'pg-overdemand-{suffix}'

    def overdemand(index: int) -> None:
        target = wh_b if index == 0 else wh_c
        with db() as conn:
            transfer_inventory_atomic(
                conn,
                source,
                InventoryTxIn(
                    tx_type='TRANSFER',
                    quantity=8,
                    to_warehouse_id=target,
                    reference=reference,
                ),
                user,
            )

    wins, conflicts = run_race(
        overdemand, 2, conflict_types=(InventoryTransferConflict,)
    )
    if len(wins) != 1 or len(conflicts) != 1:
        raise RuntimeError(
            f'overdemand winner invariant failed: wins={wins} conflicts={conflicts}'
        )
    with db() as conn:
        source_stock = stock(conn, source)
        total = source_stock + stock(conn, dest_b) + stock(conn, dest_c)
    if source_stock != 2 or source_stock < 0 or total != 10:
        raise RuntimeError(
            f'overdemand conservation failed: source={source_stock} total={total}'
        )

    # Concurrent first transfers into an empty destination warehouse must create
    # one logical counterpart row and all successful credits must land there.
    with db() as conn:
        wh_source, wh_dest = warehouses(conn, f'{suffix}-MISS', 2)
        source = item(conn, f'{suffix}-MISS-S', wh_source, 20)
        source_row = conn.execute(
            'SELECT name,category FROM inventory_items WHERE id=?', (source,)
        ).fetchone()
    reference = f'pg-missing-{suffix}'

    def missing(_: int) -> None:
        with db() as conn:
            transfer_inventory_atomic(
                conn,
                source,
                InventoryTxIn(
                    tx_type='TRANSFER',
                    quantity=2,
                    to_warehouse_id=wh_dest,
                    reference=reference,
                ),
                user,
            )

    wins, conflicts = run_race(missing, WORKERS)
    if len(wins) != WORKERS or conflicts:
        raise RuntimeError(
            f'missing-destination transfers failed: wins={len(wins)} conflicts={conflicts}'
        )
    with db() as conn:
        destinations = conn.execute(
            '''SELECT id,current_stock FROM inventory_items
               WHERE warehouse_id=? AND name=? AND category=?''',
            (wh_dest, source_row['name'], source_row['category']),
        ).fetchall()
        source_stock = stock(conn, source)
    if len(destinations) != 1:
        raise RuntimeError(f'missing destination duplicated rows: {destinations!r}')
    destination_stock = float(destinations[0]['current_stock'])
    if source_stock != 4 or destination_stock != 16 or source_stock + destination_stock != 20:
        raise RuntimeError(
            f'missing-destination conservation failed: source={source_stock} dest={destination_stock}'
        )

    # Same idempotency key submitted concurrently: all callers obtain the same
    # result but only one debit/credit pair and one audit mutation are committed.
    with db() as conn:
        wh_source, wh_dest = warehouses(conn, f'{suffix}-IDEM', 2)
        source = item(conn, f'{suffix}-IDEM-S', wh_source, 10)
        destination = item(conn, f'{suffix}-IDEM-D', wh_dest, 0)
        source_no = conn.execute(
            'SELECT item_no FROM inventory_items WHERE id=?', (source,)
        ).fetchone()['item_no']
    key = f'pg-transfer-key-{suffix}'
    reference = f'pg-idem-{suffix}'
    results: list[dict] = []

    def idem(_: int) -> None:
        with db() as conn:
            results.append(
                transfer_inventory_atomic(
                    conn,
                    source,
                    InventoryTxIn(
                        tx_type='TRANSFER',
                        quantity=3,
                        to_warehouse_id=wh_dest,
                        reference=reference,
                    ),
                    user,
                    key,
                )
            )

    wins, conflicts = run_race(idem, WORKERS)
    if len(wins) != WORKERS or conflicts:
        raise RuntimeError('idempotent replay callers did not all complete')
    if any(result != {'ok': True, 'current_stock': 7.0} for result in results):
        raise RuntimeError(f'idempotent replay returned inconsistent results: {results!r}')
    with db() as conn:
        source_stock = stock(conn, source)
        destination_stock = stock(conn, destination)
        tx_rows = int(
            conn.execute(
                """SELECT COUNT(*) FROM inventory_transactions
                   WHERE reference=? AND tx_type='TRANSFER'""",
                (reference,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Inventory' AND action='TRANSFER' AND record_id=?""",
                (source_no,),
            ).fetchone()[0]
        )
    if source_stock != 7 or destination_stock != 3 or tx_rows != 2 or audits != 1:
        raise RuntimeError(
            'idempotency duplicated side effects: '
            f'source={source_stock} dest={destination_stock} tx={tx_rows} audit={audits}'
        )

    # The same actor/key with a different payload must be rejected without stock
    # mutation or duplicate evidence.
    try:
        with db() as conn:
            transfer_inventory_atomic(
                conn,
                source,
                InventoryTxIn(
                    tx_type='TRANSFER',
                    quantity=4,
                    to_warehouse_id=wh_dest,
                    reference=reference,
                ),
                user,
                key,
            )
    except InventoryTransferIdempotencyConflict:
        pass
    else:
        raise RuntimeError('conflicting idempotency payload unexpectedly succeeded')
    with db() as conn:
        if stock(conn, source) != 7 or stock(conn, destination) != 3:
            raise RuntimeError('conflicting idempotency payload changed stock')

    print(
        'inventory transfer concurrency smoke: PASS '
        'opposite=6 overdemand=1 missing_destination=1 idempotency=1'
    )


if __name__ == '__main__':
    main()
