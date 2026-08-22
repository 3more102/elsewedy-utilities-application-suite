from __future__ import annotations

from . import application as _application


PM_GENERATION_LOCK_ID = 1
_legacy_generate_due_pm = _application._generate_due_pm


class PMGenerationCoordinatorUnavailable(RuntimeError):
    """Raised when preventive-maintenance generation was not initialized."""


def ensure_pm_generation_lock(conn) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS pm_generation_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO pm_generation_lock(id,guard) VALUES(?,0)',
        (PM_GENERATION_LOCK_ID,),
    )


def generate_due_pm_atomic(conn, actor_id: int, target):
    """Serialize the complete due-plan scan/check/create cycle.

    The historical generator performs a check for an existing nonterminal work
    order and then creates a new work order/approval/audit in the same caller
    transaction. Serializing before that scan makes the check meaningful under
    PostgreSQL read-committed concurrency without changing business semantics.
    """
    locked = conn.execute(
        'UPDATE pm_generation_lock SET guard=guard WHERE id=?',
        (PM_GENERATION_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise PMGenerationCoordinatorUnavailable(
            'preventive maintenance generation coordinator is unavailable'
        )
    return _legacy_generate_due_pm(conn, actor_id, target)


def install_pm_generator() -> None:
    _application._generate_due_pm = generate_due_pm_atomic
