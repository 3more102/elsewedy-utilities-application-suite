from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.audit_store import ensure_audit_chain_lock
from app.database import db
from app.main import app  # noqa: F401 - installs production compatibility composition
from app.reorder_store import run_reorder_scan_atomic
from app.work_order_number_startup import initialize_work_order_number_support


WORKERS = 12


def _bootstrap() -> dict:
    with db() as conn:
        ensure_audit_chain_lock(conn)
        initialize_work_order_number_support(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not user:
            raise RuntimeError('reorder smoke requires seeded admin user')
        return dict(user)


def _seed_low_item(conn, suffix: str, *, stock: float = 2.0) -> int:
    site = conn.execute('SELECT id FROM sites ORDER BY id LIMIT 1').fetchone()
    if not site:
        raise RuntimeError('reorder smoke requires seeded site')
    warehouse = conn.execute(
        '''INSERT INTO warehouses(warehouse_code,name,site_id,status)
           VALUES(?,?,?,'Active')''',
        (
            f'WH-REORDER-PG-{suffix}',
            f'PostgreSQL reorder warehouse {suffix}',
            site['id'],
        ),
    )
    item = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,
             reserved_stock,min_level,max_level,reorder_point,unit_price,unit,bin
           ) VALUES(?,?,?,?,?,?,0,1,10,5,3,'ea','CI')''',
        (
            f'ITM-REORDER-PG-{suffix}',
            f'PostgreSQL reorder part {suffix}',
            'automatic reorder PostgreSQL concurrency smoke',
            'CI-REORDER',
            warehouse.lastrowid,
            stock,
        ),
    )
    return int(item.lastrowid)


def _parallel(workers: int, fn, timeout: int = 45):
    barrier = threading.Barrier(workers)
    results = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(fn(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('reorder worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'reorder concurrency worker failed: {errors!r}')
    return results


def _assert_one_requisition(item_id: int, results: list[list[str]]) -> str:
    with db() as conn:
        requisitions = conn.execute(
            '''SELECT pr.id,pr.pr_no,pr.status,pri.quantity
               FROM purchase_requisitions pr
               JOIN purchase_requisition_items pri ON pri.pr_id=pr.id
               WHERE pri.inventory_item_id=?
               ORDER BY pr.id''',
            (item_id,),
        ).fetchall()
        if len(requisitions) != 1:
            raise RuntimeError(
                f'expected one replenishment requisition for item {item_id}, '
                f'got {len(requisitions)}'
            )
        requisition = requisitions[0]
        pr_id = int(requisition['id'])
        pr_no = str(requisition['pr_no'])
        approvals = int(
            conn.execute(
                '''SELECT COUNT(*) FROM approval_requests
                   WHERE record_type='purchase_requisition' AND record_id=?''',
                (pr_id,),
            ).fetchone()[0]
        )
        workflows = int(
            conn.execute(
                '''SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='purchase_requisition' AND record_id=?
                     AND event='AUTO SUBMIT' ''',
                (pr_id,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                '''SELECT COUNT(*) FROM audit_logs
                   WHERE module='Procurement' AND action='AUTO CREATE'
                     AND record_id=?''',
                (pr_no,),
            ).fetchone()[0]
        )

    if requisition['status'] != 'Submitted':
        raise RuntimeError(f'reorder PR status changed: {requisition["status"]!r}')
    if float(requisition['quantity']) != 8.0:
        raise RuntimeError(f'reorder quantity changed: {requisition["quantity"]!r}')
    if approvals != 1 or workflows != 1 or audits != 1:
        raise RuntimeError(
            'reorder side effects duplicated: '
            f'approvals={approvals} workflows={workflows} audits={audits}'
        )
    if sum(pr_no in result for result in results) != 1:
        raise RuntimeError(f'reorder result ownership invalid for {pr_no}: {results!r}')
    return pr_no


def _single_item_race(user: dict) -> None:
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        item_id = _seed_low_item(conn, suffix)

    def scan(_index: int):
        with db() as conn:
            return _application._run_reorder_scan(conn, user['id'])

    results = _parallel(WORKERS, scan)
    _assert_one_requisition(item_id, results)


def _two_item_lock_order(user: dict) -> None:
    suffix = uuid.uuid4().hex[:8]
    with db() as conn:
        first = _seed_low_item(conn, suffix + '-A')
        second = _seed_low_item(conn, suffix + '-B')

    def scan(_index: int):
        with db() as conn:
            return _application._run_reorder_scan(conn, user['id'])

    results = _parallel(WORKERS, scan)
    _assert_one_requisition(first, results)
    _assert_one_requisition(second, results)


def _fresh_stock_recheck(user: dict) -> None:
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        item_id = _seed_low_item(conn, suffix)

    lock_held = threading.Event()
    scanner_started = threading.Event()
    errors: list[BaseException] = []
    result: list[list[str]] = []

    def restock() -> None:
        try:
            with db() as conn:
                conn.execute(
                    'UPDATE inventory_items SET current_stock=20 WHERE id=?',
                    (item_id,),
                )
                lock_held.set()
                scanner_started.wait(timeout=10)
                time.sleep(0.20)
        except BaseException as exc:
            errors.append(exc)

    def scan() -> None:
        try:
            assert lock_held.wait(timeout=10)
            scanner_started.set()
            with db() as conn:
                result.append(_application._run_reorder_scan(conn, user['id']))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=restock), threading.Thread(target=scan)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('restock/reorder race did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'restock/reorder race failed: {errors!r}')

    with db() as conn:
        item = conn.execute(
            'SELECT current_stock FROM inventory_items WHERE id=?',
            (item_id,),
        ).fetchone()
        pr_count = int(
            conn.execute(
                '''SELECT COUNT(*) FROM purchase_requisition_items pri
                   JOIN purchase_requisitions pr ON pr.id=pri.pr_id
                   WHERE pri.inventory_item_id=?''',
                (item_id,),
            ).fetchone()[0]
        )
    if float(item['current_stock']) != 20.0:
        raise RuntimeError(f'restock was not preserved: {dict(item)!r}')
    if pr_count != 0:
        raise RuntimeError('stale low-stock candidate created a PR after fresh restock')
    if result != [[]]:
        raise RuntimeError(f'restock/reorder result was not a clean skip: {result!r}')


def main() -> None:
    if _application._run_reorder_scan is not run_reorder_scan_atomic:
        raise RuntimeError('application reorder generator was not replaced')
    user = _bootstrap()
    _single_item_race(user)
    _two_item_lock_order(user)
    _fresh_stock_recheck(user)
    print(
        'reorder generation concurrency smoke: PASS '
        'workers=12 single_item_pr=1 two_item_prs=2 restock_skip=1'
    )


if __name__ == '__main__':
    main()
