from __future__ import annotations

from . import application as _application


WORK_ORDER_NUMBER_LOCK_ID = 1
_legacy_next_no = _application.next_no


class WorkOrderNumberCoordinatorUnavailable(RuntimeError):
    """Raised when the shared business-number coordinator is unavailable."""


def ensure_work_order_number_lock(conn) -> None:
    """Initialize legacy compatibility and per-sequence number lock storage."""
    # Keep the original singleton table/row because existing deployment and smoke
    # tooling from PR #32 validates this bootstrap contract explicitly.
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
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS business_number_lock(
             sequence_key TEXT PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )


def _sequence_key(table, field, prefix) -> str:
    return f'{table}\x1f{field}\x1f{prefix}'


def _lock_number_sequence(conn, table, field, prefix) -> str:
    """Acquire one transaction-scoped row lock for a logical number sequence."""
    key = _sequence_key(table, field, prefix)
    conn.execute(
        '''INSERT OR IGNORE INTO business_number_lock(sequence_key,guard)
           VALUES(?,0)''',
        (key,),
    )
    locked = conn.execute(
        '''UPDATE business_number_lock
           SET guard=guard
           WHERE sequence_key=?''',
        (key,),
    )
    if int(locked.rowcount or 0) != 1:
        raise WorkOrderNumberCoordinatorUnavailable(
            f'business-number coordinator is unavailable for {table}.{field}'
        )
    return key


def _lock_work_order_number(conn) -> None:
    """Compatibility helper retained for callers/tests from the WO hardening wave."""
    _lock_number_sequence(conn, 'work_orders', 'wo_no', 'WO-')


def next_no_with_work_order_lock(conn, table, field, prefix, start=1):
    """Serialize every generated business-number sequence before read-max allocation.

    Each ``(table, field, prefix)`` has an independent lock row, so unrelated
    number families can progress concurrently. The lock is held until the
    caller's transaction commits or rolls back, which covers the subsequent
    INSERT that consumes the number returned by the historical allocator.
    """
    _lock_number_sequence(conn, table, field, prefix)
    return _legacy_next_no(conn, table, field, prefix, start)


def install_work_order_number_allocator() -> None:
    if _application.next_no is next_no_with_work_order_lock:
        return
    _application.next_no = next_no_with_work_order_lock
