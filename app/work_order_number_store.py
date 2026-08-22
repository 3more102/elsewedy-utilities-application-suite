from __future__ import annotations

from . import application as _application


WORK_ORDER_NUMBER_LOCK_ID = 1
_legacy_next_no = _application.next_no


class WorkOrderNumberCoordinatorUnavailable(RuntimeError):
    """Raised when the shared business-number coordinator is unavailable."""


def ensure_work_order_number_lock(conn) -> None:
    """Initialize the global allocation gate and per-sequence registry."""
    # Preserve the PR #32 table/row contract and generalize its singleton into
    # the global generated-number allocation gate. Existing bootstrap scripts and
    # deployments therefore remain compatible.
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
    # This table is a registry of logical sequences observed by the allocator.
    # The singleton gate above supplies the transaction lock so callers that
    # allocate multiple number families cannot deadlock by taking sequence rows
    # in opposite orders.
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS business_number_lock(
             sequence_key TEXT PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )


def _sequence_key(table, field, prefix) -> str:
    return f'{table}\x1f{field}\x1f{prefix}'


def _lock_global_number_coordinator(conn) -> None:
    locked = conn.execute(
        'UPDATE work_order_number_lock SET guard=guard WHERE id=?',
        (WORK_ORDER_NUMBER_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise WorkOrderNumberCoordinatorUnavailable(
            'business-number coordinator is not initialized'
        )


def _lock_number_sequence(conn, table, field, prefix) -> str:
    """Enter the global gate and register the logical number sequence."""
    _lock_global_number_coordinator(conn)
    key = _sequence_key(table, field, prefix)
    conn.execute(
        '''INSERT OR IGNORE INTO business_number_lock(sequence_key,guard)
           VALUES(?,0)''',
        (key,),
    )
    return key


def _lock_work_order_number(conn) -> None:
    """Compatibility helper retained for callers/tests from the WO hardening wave."""
    _lock_number_sequence(conn, 'work_orders', 'wo_no', 'WO-')


def next_no_with_work_order_lock(conn, table, field, prefix, start=1):
    """Serialize every generated business number before read-max allocation.

    One transaction-scoped global gate avoids both duplicate read-max results and
    cross-sequence lock inversion when a business transaction allocates multiple
    families (for example WO- followed by APR-). Per-sequence registry rows make
    coverage observable without introducing a second lock order.
    """
    _lock_number_sequence(conn, table, field, prefix)
    return _legacy_next_no(conn, table, field, prefix, start)


def install_work_order_number_allocator() -> None:
    if _application.next_no is next_no_with_work_order_lock:
        return
    _application.next_no = next_no_with_work_order_lock
