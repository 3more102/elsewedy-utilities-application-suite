from __future__ import annotations

from . import application as _application


WORK_ORDER_NUMBER_LOCK_ID = 1
_legacy_next_no = _application.next_no


class WorkOrderNumberCoordinatorUnavailable(RuntimeError):
    """Raised when the WO-number coordinator has not been initialized."""


def ensure_work_order_number_lock(conn) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS work_order_number_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO work_order_number_lock(id,guard) VALUES(?,0)',
        (WORK_ORDER_NUMBER_LOCK_ID,),
    )


def _lock_work_order_number(conn) -> None:
    locked = conn.execute(
        'UPDATE work_order_number_lock SET guard=guard WHERE id=?',
        (WORK_ORDER_NUMBER_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise WorkOrderNumberCoordinatorUnavailable(
            'work-order number coordinator is not initialized'
        )


def next_no_with_work_order_lock(conn, table, field, prefix, start=1):
    """Serialize every work-order number allocation across all creation paths.

    The lock is held by the caller transaction until commit/rollback, so the
    following INSERT of the allocated unique ``wo_no`` completes before another
    work-order creator can derive the next value. Other business-number families
    retain the historical allocator unchanged.
    """
    if table == 'work_orders' and field == 'wo_no':
        _lock_work_order_number(conn)
    return _legacy_next_no(conn, table, field, prefix, start)


def install_work_order_number_allocator() -> None:
    if _application.next_no is next_no_with_work_order_lock:
        return
    _application.next_no = next_no_with_work_order_lock
