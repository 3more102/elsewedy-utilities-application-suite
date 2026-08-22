from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import ApprovalDecisionIn, DispatchTransitionIn, TransitionIn
from app.approval_store import ApprovalTransitionConflict, decide_approval_atomic
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.workflow_store import (
    WorkflowTransitionConflict,
    transition_dispatch_atomic,
    transition_work_atomic,
)


WORKERS = 8


def admin_user() -> dict:
    with db() as conn:
        ensure_audit_chain_lock(conn)
        row = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not row:
            raise RuntimeError('workflow smoke requires seeded admin')
        return dict(row)


def seed_work(suffix: str, status: str, user: dict, assigned_to=None):
    with db() as conn:
        number = f'WO-PG-CAS-{suffix}'
        created = conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,priority,status,work_type,requested_by,assigned_to,
                 created_at,updated_at
               ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?,?,?)''',
            (
                number,
                'PostgreSQL workflow race',
                status,
                user['id'],
                assigned_to,
                now(),
                now(),
            ),
        )
        return int(created.lastrowid), number


def run_one_winner(operation, conflicts=(WorkflowTransitionConflict,)):
    barrier = threading.Barrier(WORKERS)
    wins: list[int] = []
    rejected: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            operation(index)
            wins.append(index)
        except conflicts:
            rejected.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('workflow concurrency worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'workflow concurrency worker failed: {errors!r}')
    if len(wins) != 1 or len(rejected) != WORKERS - 1:
        raise RuntimeError(
            f'expected one workflow winner; wins={len(wins)} conflicts={len(rejected)}'
        )


def main() -> None:
    user = admin_user()
    suffix = uuid.uuid4().hex[:10]

    # Draft -> Submitted must create one transition and one approval request.
    work_id, work_no = seed_work(suffix + 'S', 'Draft', user)

    def submit_once(index: int) -> None:
        with db() as conn:
            transition_work_atomic(conn, work_id, TransitionIn(action='submit'), user)

    run_one_winner(submit_once)
    with db() as conn:
        status = conn.execute(
            'SELECT status FROM work_orders WHERE id=?', (work_id,)
        ).fetchone()['status']
        approvals = int(
            conn.execute(
                """SELECT COUNT(*) FROM approval_requests
                   WHERE record_type='work_order' AND record_id=?""",
                (work_id,),
            ).fetchone()[0]
        )
        events = int(
            conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='work_order' AND record_id=? AND event='SUBMIT'""",
                (work_id,),
            ).fetchone()[0]
        )
    if status != 'Submitted' or approvals != 1 or events != 1:
        raise RuntimeError(
            f'work submit race invalid: status={status} approvals={approvals} events={events}'
        )

    # Direct work approval and unified approval center share business-row first
    # locking. This detects both duplicate decisions and lock-order deadlocks.
    work_id, work_no = seed_work(suffix + 'A', 'Submitted', user)
    with db() as conn:
        approval = conn.execute(
            '''INSERT INTO approval_requests(
                 approval_no,module,record_type,record_id,record_code,title,
                 requested_by,assigned_role,status,requested_at
               ) VALUES(?,?,?,?,?,?,?,'maintenance_manager','Pending',?)''',
            (
                f'APR-PG-WO-{suffix}',
                'Work Management',
                'work_order',
                work_id,
                work_no,
                f'Approve {work_no}',
                user['id'],
                now(),
            ),
        )
        approval_id = int(approval.lastrowid)
    decision = ApprovalDecisionIn(decision='approve', comments='postgres race')

    def approve_once(index: int) -> None:
        with db() as conn:
            if index % 2:
                decide_approval_atomic(conn, approval_id, decision, user)
            else:
                transition_work_atomic(conn, work_id, TransitionIn(action='approve'), user)

    run_one_winner(
        approve_once,
        conflicts=(WorkflowTransitionConflict, ApprovalTransitionConflict),
    )
    with db() as conn:
        work_status = conn.execute(
            'SELECT status FROM work_orders WHERE id=?', (work_id,)
        ).fetchone()['status']
        approval_status = conn.execute(
            'SELECT status FROM approval_requests WHERE id=?', (approval_id,)
        ).fetchone()['status']
        events = int(
            conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='work_order' AND record_id=? AND event='APPROVE'""",
                (work_id,),
            ).fetchone()[0]
        )
    if work_status != 'Approved' or approval_status != 'Approved' or events != 1:
        raise RuntimeError(
            'work approval race invalid: '
            f'work={work_status} approval={approval_status} events={events}'
        )

    # Multiple completion attempts from one historical state have one winner.
    work_id, _ = seed_work(suffix + 'C', 'In Progress', user)

    def complete_once(index: int) -> None:
        with db() as conn:
            transition_work_atomic(
                conn,
                work_id,
                TransitionIn(action='complete', notes=f'worker {index}'),
                user,
            )

    run_one_winner(complete_once)
    with db() as conn:
        work = conn.execute(
            'SELECT status,actual_finish FROM work_orders WHERE id=?', (work_id,)
        ).fetchone()
    if work['status'] != 'Completed' or not work['actual_finish']:
        raise RuntimeError(f'work completion race invalid: {dict(work)!r}')

    # Dispatch accept transition has exactly one state winner.
    with db() as conn:
        tech = conn.execute(
            """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
               WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
        ).fetchone()
        if not tech:
            raise RuntimeError('workflow smoke requires seeded technician')
        tech_id = int(tech['id'])
    work_id, _ = seed_work(suffix + 'D', 'Assigned', user, tech_id)
    with db() as conn:
        dispatch = conn.execute(
            '''INSERT INTO dispatch_assignments(
                 dispatch_no,work_order_id,technician_user_id,dispatched_by,
                 status,eta_minutes,notes,dispatched_at
               ) VALUES(?,?,?,?, 'Dispatched',30,'race',?)''',
            (f'DSP-PG-{suffix}', work_id, tech_id, user['id'], now()),
        )
        dispatch_id = int(dispatch.lastrowid)

    def accept_once(index: int) -> None:
        with db() as conn:
            transition_dispatch_atomic(
                conn, dispatch_id, DispatchTransitionIn(action='accept'), user
            )

    run_one_winner(accept_once)

    # Dispatch arrive and direct work start can both be valid endpoint calls,
    # but only one may perform Assigned -> In Progress and emit that workflow event.
    work_id, _ = seed_work(suffix + 'R', 'Assigned', user, tech_id)
    with db() as conn:
        dispatch = conn.execute(
            '''INSERT INTO dispatch_assignments(
                 dispatch_no,work_order_id,technician_user_id,dispatched_by,
                 status,eta_minutes,notes,dispatched_at,enroute_at
               ) VALUES(?,?,?,?, 'En Route',30,'race',?,?)''',
            (
                f'DSP-PG-ARR-{suffix}',
                work_id,
                tech_id,
                user['id'],
                now(),
                now(),
            ),
        )
        dispatch_id = int(dispatch.lastrowid)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def arrive() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                transition_dispatch_atomic(
                    conn, dispatch_id, DispatchTransitionIn(action='arrive'), user
                )
        except BaseException as exc:
            errors.append(exc)

    def start() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                transition_work_atomic(conn, work_id, TransitionIn(action='start'), user)
        except WorkflowTransitionConflict:
            pass
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=arrive), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('dispatch-arrive/work-start race deadlocked')
    if errors:
        raise RuntimeError(f'dispatch-arrive/work-start race failed: {errors!r}')
    with db() as conn:
        work = conn.execute(
            'SELECT status,actual_start FROM work_orders WHERE id=?', (work_id,)
        ).fetchone()
        transitions = int(
            conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='work_order' AND record_id=?
                     AND from_status='Assigned' AND to_status='In Progress'""",
                (work_id,),
            ).fetchone()[0]
        )
        dispatch_status = conn.execute(
            'SELECT status FROM dispatch_assignments WHERE id=?', (dispatch_id,)
        ).fetchone()['status']
    if (
        work['status'] != 'In Progress'
        or not work['actual_start']
        or transitions != 1
        or dispatch_status != 'On Site'
    ):
        raise RuntimeError(
            'arrive/start race invalid: '
            f'work={dict(work)!r} transitions={transitions} dispatch={dispatch_status}'
        )

    print(
        'workflow concurrency smoke: PASS '
        'submit=1 approval=1 complete=1 dispatch=1 arrive_start=linear'
    )


if __name__ == '__main__':
    main()
