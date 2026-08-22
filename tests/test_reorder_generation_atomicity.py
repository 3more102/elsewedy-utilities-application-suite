from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db
from app.main import app
from app.reorder_store import run_reorder_scan_atomic


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_low_item(conn, suffix: str) -> int:
    site = conn.execute('SELECT id FROM sites ORDER BY id LIMIT 1').fetchone()
    assert site
    warehouse = conn.execute(
        '''INSERT INTO warehouses(warehouse_code,name,site_id,status)
           VALUES(?,?,?,'Active')''',
        (
            f'WH-REORDER-{suffix}',
            f'Reorder concurrency warehouse {suffix}',
            site['id'],
        ),
    )
    item = conn.execute(
        '''INSERT INTO inventory_items(
             item_no,name,description,category,warehouse_id,current_stock,
             reserved_stock,min_level,max_level,reorder_point,unit_price,unit,bin
           ) VALUES(?,?,?,?,?,2,0,1,10,5,3,'ea','CI')''',
        (
            f'ITM-REORDER-{suffix}',
            f'Reorder concurrency part {suffix}',
            'automatic reorder atomicity regression',
            'CI-REORDER',
            warehouse.lastrowid,
        ),
    )
    return int(item.lastrowid)


def test_shared_reorder_generator_is_replaced_by_atomic_wrapper():
    assert _application._run_reorder_scan is run_reorder_scan_atomic


def test_concurrent_reorder_scans_create_one_replenishment_side_effect_set():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = _admin(conn)
            item_id = _seed_low_item(conn, suffix)

        barrier = threading.Barrier(WORKERS)
        results: list[list[str]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(_application._run_reorder_scan(conn, user['id']))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []

        with db() as conn:
            requisitions = conn.execute(
                '''SELECT pr.id,pr.pr_no,pr.status,pri.quantity
                   FROM purchase_requisitions pr
                   JOIN purchase_requisition_items pri ON pri.pr_id=pr.id
                   WHERE pri.inventory_item_id=?
                   ORDER BY pr.id''',
                (item_id,),
            ).fetchall()
            assert len(requisitions) == 1
            requisition = requisitions[0]
            pr_id = int(requisition['id'])
            pr_no = requisition['pr_no']

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

        assert requisition['status'] == 'Submitted'
        assert float(requisition['quantity']) == 8.0
        assert approvals == 1
        assert workflows == 1
        assert audits == 1
        assert sum(pr_no in result for result in results) == 1
