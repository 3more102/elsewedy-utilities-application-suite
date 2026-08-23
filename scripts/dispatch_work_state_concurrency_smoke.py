from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import DispatchTransitionIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.main import app  # noqa: F401 - installs production hardening composition
from app.workflow_store import WorkflowTransitionConflict, transition_dispatch_atomic


WORKERS = 12


def _admin() -> dict:
    with db() as conn:
        ensure_audit_chain_lock(conn)
        row = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not row:
            raise RuntimeError('dispatch-work smoke requires seeded admin')
        return dict(row)


def _technician_id() -> int:
    with db() as conn:
        row = conn.execute(
            """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
               WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
        ).fetchone()
        if not row:
            raise RuntimeError('dispatch-work smoke requires seeded technician')
        return int(row['id'])


def _seed(work_status: str, dispatch_status: str, user: dict, technician_id: int):
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        work = conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,priority,status,work_type,requested_by,assigned_to,
                 created_at,updated_at
               ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?,?)''',
            (
                f'WO-PG-DSP-GUARD-{suffix}',
                'PostgreSQL dispatch/work state guard',
                work_status,
                user['id'],
                technician_id,
                now(),
                now(),
            ),
        )
        dispatch_no = f'DSP-PG-GUARD-{suffix}'
        dispatch = conn.execute(
            '''INSERT INTO dispatch_assignments(
                 dispatch_no,work_order_id,technician_user_id,dispatched_by,
                 status,eta_minutes,notes,dispatched_at
               ) VALUES(?,?,?,?,?,30,'postgres guard',?)''',
            (
                dispatch_no,
                int(work.lastrowid),
                technician_id,
                user['id'],
                dispatch_status,
                now(),
            ),
        )
        return int(dispatch.lastrowid), dispatch_no


def _parallel(fn):
    barrier = threading.Barrier(WORKERS)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            fn(index)
            wins.append(index)
        except WorkflowTransitionConflict:
            conflicts.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('dispatch/work-state worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'dispatch/work-state worker failed: {errors!r}')
    return wins, conflicts


def _terminal_work_rejects_arrival(user: dict, technician_id: int) -> None:
    dispatch_id, dispatch_no = _seed('Completed', 'En Route', user, technician_id)

    def arrive(_index: int) -> None:
        with db() as conn:
            transition_dispatch_atomic(
                conn,
                dispatch_id,
                DispatchTransitionIn(action='arrive'),
                user,
            )

    wins, conflicts = _parallel(arrive)
    if wins or len(conflicts) != WORKERS:
        raise RuntimeError(
            f'terminal work accepted stale arrival: wins={wins!r} conflicts={len(conflicts)}'
        )
    with db() as conn:
        dispatch = conn.execute(
            'SELECT status,arrived_at FROM dispatch_assignments WHERE id=?',
            (dispatch_id,),
        ).fetchone()
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Field Service' AND action='DISPATCH ARRIVE'
                     AND record_id=?""",
                (dispatch_no,),
            ).fetchone()[0]
        )
    if dispatch['status'] != 'En Route' or dispatch['arrived_at'] is not None or audits != 0:
        raise RuntimeError(
            f'stale arrival was not transactionally rolled back: {dict(dispatch)!r} audits={audits}'
        )


def _closed_work_allows_one_dispatch_settlement(user: dict, technician_id: int) -> None:
    dispatch_id, dispatch_no = _seed('Closed', 'On Site', user, technician_id)

    def complete(_index: int) -> None:
        with db() as conn:
            transition_dispatch_atomic(
                conn,
                dispatch_id,
                DispatchTransitionIn(action='complete', notes='settle field record'),
                user,
            )

    wins, conflicts = _parallel(complete)
    if len(wins) != 1 or len(conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'closed-work dispatch settlement expected one winner: wins={wins!r} conflicts={len(conflicts)}'
        )
    with db() as conn:
        dispatch = conn.execute(
            'SELECT status,completed_at FROM dispatch_assignments WHERE id=?',
            (dispatch_id,),
        ).fetchone()
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Field Service' AND action='DISPATCH COMPLETE'
                     AND record_id=?""",
                (dispatch_no,),
            ).fetchone()[0]
        )
    if dispatch['status'] != 'Completed' or not dispatch['completed_at'] or audits != 1:
        raise RuntimeError(
            f'dispatch settlement state invalid: {dict(dispatch)!r} audits={audits}'
        )


def main() -> None:
    user = _admin()
    technician_id = _technician_id()
    _terminal_work_rejects_arrival(user, technician_id)
    _closed_work_allows_one_dispatch_settlement(user, technician_id)
    print(
        'dispatch/work state concurrency smoke: PASS '
        'terminal_arrival_rejected=12 rollback=clean closed_settlement_winners=1 workers=12'
    )


if __name__ == '__main__':
    main()
