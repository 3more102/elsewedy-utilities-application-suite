from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import ApprovalDecisionIn, DispatchTransitionIn, TransitionIn
from app.approval_store import ApprovalTransitionConflict, decide_approval_atomic
from app.database import db, now
from app.main import app
from app.workflow_store import (
    WorkflowTransitionConflict,
    transition_dispatch_atomic,
    transition_work_atomic,
)

WORKERS = 8


def admin(conn):
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def seed_work(conn, suffix, status, assigned_to=None):
    user = admin(conn)
    number = f'WO-CAS-{suffix}'
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,assigned_to,
             created_at,updated_at
           ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?,?)''',
        (number, 'Workflow concurrency work', status, user['id'], assigned_to, now(), now()),
    )
    return int(cur.lastrowid), number, user


def race(operation, workers=WORKERS, conflict_types=(WorkflowTransitionConflict,)):
    barrier = threading.Barrier(workers)
    wins, conflicts, errors = [], [], []

    def worker(index):
        try:
            barrier.wait(timeout=10)
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
        thread.join(timeout=25)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    return wins, conflicts


def test_submit_and_complete_transitions_have_one_winner():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            work_id, work_no, user = seed_work(conn, suffix + 'S', 'Draft')

        def submit(_):
            with db() as conn:
                transition_work_atomic(conn, work_id, TransitionIn(action='submit'), user)

        wins, conflicts = race(submit)
        assert len(wins) == 1 and len(conflicts) == WORKERS - 1
        with db() as conn:
            assert conn.execute('SELECT status FROM work_orders WHERE id=?', (work_id,)).fetchone()['status'] == 'Submitted'
            assert conn.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE record_type='work_order' AND record_id=?",
                (work_id,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='SUBMIT'",
                (work_id,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE module='Work Management' AND action='SUBMIT' AND record_id=?",
                (work_no,),
            ).fetchone()[0] == 1

        with db() as conn:
            work_id, _, user = seed_work(conn, suffix + 'C', 'In Progress')

        def complete(index):
            with db() as conn:
                transition_work_atomic(
                    conn,
                    work_id,
                    TransitionIn(action='complete', notes=f'complete {index}'),
                    user,
                )

        wins, conflicts = race(complete)
        assert len(wins) == 1 and len(conflicts) == WORKERS - 1
        with db() as conn:
            work = conn.execute('SELECT status,actual_finish FROM work_orders WHERE id=?', (work_id,)).fetchone()
            assert work['status'] == 'Completed' and work['actual_finish']
            assert conn.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='COMPLETE'",
                (work_id,),
            ).fetchone()[0] == 1


def test_direct_and_unified_work_approval_share_one_atomic_winner():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            work_id, work_no, user = seed_work(conn, suffix, 'Submitted')
            approval = conn.execute(
                '''INSERT INTO approval_requests(
                     approval_no,module,record_type,record_id,record_code,title,
                     requested_by,assigned_role,status,requested_at
                   ) VALUES(?,?,?,?,?,?,?,'maintenance_manager','Pending',?)''',
                (f'APR-WO-{suffix}', 'Work Management', 'work_order', work_id, work_no,
                 f'Approve {work_no}', user['id'], now()),
            )
            approval_id = int(approval.lastrowid)
        decision = ApprovalDecisionIn(decision='approve', comments='race')

        def approve(index):
            with db() as conn:
                if index % 2:
                    decide_approval_atomic(conn, approval_id, decision, user)
                else:
                    transition_work_atomic(conn, work_id, TransitionIn(action='approve'), user)

        wins, conflicts = race(
            approve,
            conflict_types=(WorkflowTransitionConflict, ApprovalTransitionConflict),
        )
        assert len(wins) == 1 and len(conflicts) == WORKERS - 1
        with db() as conn:
            assert conn.execute('SELECT status FROM work_orders WHERE id=?', (work_id,)).fetchone()['status'] == 'Approved'
            assert conn.execute('SELECT status FROM approval_requests WHERE id=?', (approval_id,)).fetchone()['status'] == 'Approved'
            assert conn.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='APPROVE'",
                (work_id,),
            ).fetchone()[0] == 1


def test_dispatch_accept_has_one_winner():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = admin(conn)
            tech = conn.execute(
                """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
            ).fetchone()
            assert tech
            work_id, _, _ = seed_work(conn, suffix, 'Assigned', tech['id'])
            cur = conn.execute(
                '''INSERT INTO dispatch_assignments(
                     dispatch_no,work_order_id,technician_user_id,dispatched_by,
                     status,eta_minutes,notes,dispatched_at
                   ) VALUES(?,?,?,?,'Dispatched',30,'race',?)''',
                (f'DSP-CAS-{suffix}', work_id, tech['id'], user['id'], now()),
            )
            dispatch_id = int(cur.lastrowid)

        def accept(_):
            with db() as conn:
                transition_dispatch_atomic(conn, dispatch_id, DispatchTransitionIn(action='accept'), user)

        wins, conflicts = race(accept)
        assert len(wins) == 1 and len(conflicts) == WORKERS - 1
        with db() as conn:
            assert conn.execute('SELECT status FROM dispatch_assignments WHERE id=?', (dispatch_id,)).fetchone()['status'] == 'Accepted'


def test_dispatch_arrive_racing_direct_start_changes_work_once():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = admin(conn)
            tech = conn.execute(
                """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
            ).fetchone()
            assert tech
            work_id, _, _ = seed_work(conn, suffix, 'Assigned', tech['id'])
            cur = conn.execute(
                '''INSERT INTO dispatch_assignments(
                     dispatch_no,work_order_id,technician_user_id,dispatched_by,
                     status,eta_minutes,notes,dispatched_at,enroute_at
                   ) VALUES(?,?,?,?,'En Route',30,'race',?,?)''',
                (f'DSP-ARR-{suffix}', work_id, tech['id'], user['id'], now(), now()),
            )
            dispatch_id = int(cur.lastrowid)

        barrier = threading.Barrier(2)
        errors = []

        def arrive():
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    transition_dispatch_atomic(conn, dispatch_id, DispatchTransitionIn(action='arrive'), user)
            except BaseException as exc:
                errors.append(exc)

        def start():
            try:
                barrier.wait(timeout=10)
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
            thread.join(timeout=25)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        with db() as conn:
            work = conn.execute('SELECT status,actual_start FROM work_orders WHERE id=?', (work_id,)).fetchone()
            transitions = conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE record_type='work_order' AND record_id=?
                     AND from_status='Assigned' AND to_status='In Progress'""",
                (work_id,),
            ).fetchone()[0]
            dispatch_status = conn.execute('SELECT status FROM dispatch_assignments WHERE id=?', (dispatch_id,)).fetchone()['status']
        assert work['status'] == 'In Progress' and work['actual_start']
        assert transitions == 1
        assert dispatch_status == 'On Site'
