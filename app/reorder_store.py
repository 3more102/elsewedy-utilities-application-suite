from __future__ import annotations

import sys

from . import application as _application


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _lock_inventory_item(conn, item_id: int) -> dict | None:
    """Serialize reorder decisions on the canonical inventory-item row."""
    locked = conn.execute(
        'UPDATE inventory_items SET current_stock=current_stock WHERE id=?',
        (item_id,),
    )
    if not _rowcount_one(locked):
        return None
    row = conn.execute(
        '''SELECT i.*,w.site_id
           FROM inventory_items i
           JOIN warehouses w ON w.id=i.warehouse_id
           WHERE i.id=?''',
        (item_id,),
    ).fetchone()
    return dict(row) if row else None


def _active_replenishment_exists(conn, item_id: int) -> bool:
    row = conn.execute(
        '''SELECT pr.id
           FROM purchase_requisitions pr
           JOIN purchase_requisition_items x ON x.pr_id=pr.id
           WHERE x.inventory_item_id=?
             AND pr.status NOT IN ('Received','Cancelled','Rejected')
           LIMIT 1''',
        (item_id,),
    ).fetchone()
    return bool(row)


def run_reorder_scan_atomic(conn, actor_id: int):
    """Create at most one active automatic replenishment PR per low-stock item.

    The initial candidate scan is intentionally unlocked. Each candidate is then
    serialized on the inventory-item row and revalidated after the lock is held.
    This prevents concurrent automation runs from both observing the same stale
    "no active PR" state while also respecting stock updates that committed first.
    """
    created: list[str] = []
    candidates = [
        dict(row)
        for row in conn.execute(
            '''SELECT i.id
               FROM inventory_items i
               WHERE i.current_stock-i.reserved_stock<=i.reorder_point
               ORDER BY i.id'''
        ).fetchall()
    ]

    for candidate in candidates:
        item = _lock_inventory_item(conn, int(candidate['id']))
        if not item:
            continue

        available = float(item['current_stock']) - float(item['reserved_stock'])
        if available > float(item['reorder_point']):
            continue
        if _active_replenishment_exists(conn, int(item['id'])):
            continue

        qty = max(
            float(item['max_level']) - float(item['current_stock']),
            float(item['reorder_point']) - float(item['current_stock']) + 1,
            1,
        )
        number = _application.next_no(
            conn,
            'purchase_requisitions',
            'pr_no',
            'PR-',
            8001,
        )
        cur = conn.execute(
            '''INSERT INTO purchase_requisitions(
                 pr_no,title,requester_id,site_id,status,justification,
                 total_estimate,created_at
               ) VALUES(?,?,?,?,?,?,?,?)''',
            (
                number,
                f"Auto-replenishment — {item['item_no']}",
                actor_id,
                item['site_id'],
                'Submitted',
                'Automatically generated because available stock reached reorder point.',
                qty * float(item['unit_price']),
                _application.now(),
            ),
        )
        pr_id = int(cur.lastrowid)
        conn.execute(
            '''INSERT INTO purchase_requisition_items(
                 pr_id,inventory_item_id,description,quantity,estimated_unit_cost
               ) VALUES(?,?,?,?,?)''',
            (
                pr_id,
                item['id'],
                item['name'],
                qty,
                item['unit_price'],
            ),
        )
        _application.create_approval(
            conn,
            'Procurement',
            'purchase_requisition',
            pr_id,
            number,
            f'Approve {number} — Auto-replenishment',
            actor_id,
            assigned_role='procurement',
        )
        _application.workflow_event(
            conn,
            'Procurement',
            'purchase_requisition',
            pr_id,
            number,
            'AUTO SUBMIT',
            '',
            'Submitted',
            actor_id,
            'Automatic reorder',
        )
        _application.audit(
            conn,
            actor_id,
            'AUTO CREATE',
            'Procurement',
            number,
            '',
            {'item': item['item_no'], 'qty': qty},
        )
        created.append(number)
        _application.notify_once(
            conn,
            'Purchase requisition created',
            f'{number} created for {item["item_no"]}',
            'Info',
            None,
            'procurement',
            'procurement',
            number,
        )

    return created


def install_reorder_generation_atomicity() -> None:
    app = _application.app
    marker = '_euas_reorder_generation_atomicity'
    if getattr(app.state, marker, False):
        return

    _application._run_reorder_scan = run_reorder_scan_atomic

    main_module = sys.modules.get(f'{__package__}.main')
    if main_module is not None:
        setattr(main_module, '_run_reorder_scan', run_reorder_scan_atomic)

    setattr(app.state, marker, True)
