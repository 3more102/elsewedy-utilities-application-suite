from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import TransitionIn
from app.database import db, now
from app.main import app
from app.workflow_store import (
    WorkflowTransitionConflict,
    transition_work_atomic,
)

CANCELLABLE_FROM = ('Draft', 'Submitted', 'Rejected', 'Approved', 'Assigned')
NON_CANCELLABLE_FROM = ('In Progress', 'Completed', 'Closed')


def admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def technician(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,u.username,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE r.code='technician' AND u.active=1
           ORDER BY u.id LIMIT 1"""
    ).fetchone()
    assert row
    return dict(row)


def seed_work(conn, status: str, priority: str = 'Medium') -> tuple[int, str]:
    user = admin(conn)
    number = f'WO-CXL-{uuid.uuid4().hex[:10]}'
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,
             created_at,updated_at
           ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?)''',
        (number, 'Cancellation regression work', status, user['id'], now(), now()),
    )
    return int(cur.lastrowid), number


def seed_asset_and_item(conn):
    user = admin(conn)
    asset_no = f'CXL-{uuid.uuid4().hex[:8]}'
    cur = conn.execute(
        """INSERT INTO assets(asset_no,name,category,criticality,condition,status,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (asset_no, 'Cancellation asset', 'QA', 'Low', 'Good', 'Operating', now(), now()),
    )
    warehouse = conn.execute('SELECT id FROM warehouses ORDER BY id LIMIT 1').fetchone()
    assert warehouse
    item_no = f'CXL-ITM-{uuid.uuid4().hex[:8]}'
    cur2 = conn.execute(
        """INSERT INTO inventory_items(item_no,name,category,unit_price,unit,
             current_stock,reserved_stock,reorder_point,max_level,warehouse_id)
           VALUES(?,?,?,?,?,10,0,2,20,?)""",
        (item_no, 'Cancellation spare', 'QA', 15.0, 'EA', int(warehouse['id'])),
    )
    return int(cur.lastrowid), int(cur2.lastrowid), user


def test_cancel_is_valid_from_open_planning_states_only():
    with TestClient(app):
        cases = []
        with db() as conn:
            for status in NON_CANCELLABLE_FROM:
                wo_id, _wo_no = seed_work(conn, status)
                cases.append((wo_id, status))
        for wo_id, status in cases:
            try:
                with db() as conn2:
                    transition_work_atomic(
                        conn2,
                        wo_id,
                        TransitionIn(action='cancel'),
                        admin(conn2),
                    )
            except WorkflowTransitionConflict as exc:
                assert 'not valid' in str(exc), (status, exc)
            else:
                raise AssertionError(f'cancel accepted from {status}')
            with db() as conn3:
                row = conn3.execute(
                    'SELECT status FROM work_orders WHERE id=?', (wo_id,)
                ).fetchone()
                assert row['status'] == status


def test_cancel_from_each_cancellable_state_reaches_terminal_cancelled():
    with TestClient(app):
        for status in CANCELLABLE_FROM:
            with db() as conn:
                wo_id, wo_no = seed_work(conn, status)
                result = transition_work_atomic(
                    conn, wo_id, TransitionIn(action='cancel'), admin(conn)
                )
                assert result == {'ok': True, 'status': 'Cancelled'}
                row = conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (wo_id,)
                ).fetchone()
                assert row['status'] == 'Cancelled'

            # Terminal: a second cancel attempt must be rejected.
            try:
                with db() as conn:
                    transition_work_atomic(
                        conn, wo_id, TransitionIn(action='cancel'), admin(conn)
                    )
            except WorkflowTransitionConflict as exc:
                assert 'not valid' in str(exc)
            else:
                raise AssertionError('cancelled work order was cancelled again')

            with db() as conn:
                events = conn.execute(
                    """SELECT event,from_status,to_status FROM workflow_events
                       WHERE record_code=? AND module='Work Management'
                       ORDER BY id DESC LIMIT 1""",
                    (wo_no,),
                ).fetchone()
                assert events['event'] == 'CANCEL'
                assert events['to_status'] == 'Cancelled'


def test_cancelling_submitted_work_settles_pending_approval():
    with TestClient(app):
        with db() as conn:
            wo_id, wo_no = seed_work(conn, 'Submitted')
            user = admin(conn)
            from app.application import create_approval

            create_approval(
                conn,
                'Work Management',
                'work_order',
                wo_id,
                wo_no,
                f'Approve {wo_no}',
                user['id'],
                assigned_role='maintenance_manager',
            )

            transition_work_atomic(
                conn, wo_id, TransitionIn(action='cancel', notes='superseded'), user
            )

            approval = conn.execute(
                "SELECT status,comments FROM approval_requests WHERE record_id=?",
                (wo_id,),
            ).fetchone()
            assert approval is not None
            assert approval['status'] == 'Rejected'


def test_cancel_releases_reserved_material_back_to_stock():
    with TestClient(app):
        with db() as conn:
            asset_id, item_id, user = seed_asset_and_item(conn)
            wo_id, wo_no = seed_work(conn, 'Approved')
            conn.execute(
                'UPDATE work_orders SET asset_id=? WHERE id=?', (asset_id, wo_id)
            )
            conn.execute(
                """INSERT INTO work_order_requirements(
                     work_order_id,inventory_item_id,quantity,status)
                   VALUES(?,?,3,'Reserved')""",
                (wo_id, item_id),
            )
            conn.execute(
                """INSERT INTO inventory_reservations(
                     reservation_no,work_order_id,inventory_item_id,quantity,
                     issued_quantity,status,reserved_by,reserved_at,notes)
                   VALUES(?,?,?,?,0,'Reserved',?,?,?)""",
                (
                    f'RSV-CXL-{uuid.uuid4().hex[:8]}',
                    wo_id,
                    item_id,
                    3.0,
                    user['id'],
                    now(),
                    'cancellation regression',
                ),
            )
            conn.execute(
                'UPDATE inventory_items SET reserved_stock=3 WHERE id=?', (item_id,)
            )

            released = transition_work_atomic(
                conn, wo_id, TransitionIn(action='cancel'), user
            )
            assert released == {'ok': True, 'status': 'Cancelled'}

            reservation = conn.execute(
                'SELECT status,released_at FROM inventory_reservations WHERE work_order_id=?',
                (wo_id,),
            ).fetchone()
            assert reservation['status'] == 'Released'
            stock = conn.execute(
                'SELECT current_stock,reserved_stock FROM inventory_items WHERE id=?',
                (item_id,),
            ).fetchone()
            assert float(stock['current_stock']) == 10.0
            assert float(stock['reserved_stock']) == 0.0
            requirement = conn.execute(
                'SELECT status FROM work_order_requirements WHERE work_order_id=?',
                (wo_id,),
            ).fetchone()
            assert requirement['status'] == 'Required'


def test_cancel_blocked_while_active_dispatch_exists():
    with TestClient(app):
        with db() as conn:
            wo_id, _wo_no = seed_work(conn, 'Assigned')
            tech = technician(conn)
            user = admin(conn)
            conn.execute(
                'UPDATE work_orders SET assigned_to=? WHERE id=?', (tech['id'], wo_id)
            )
            dispatch_no = f'DSP-CXL-{uuid.uuid4().hex[:8]}'
            conn.execute(
                """INSERT INTO dispatch_assignments(
                     dispatch_no,work_order_id,technician_user_id,dispatched_by,status,
                     dispatched_at,notes)
                   VALUES(?,?,?,?, 'Dispatched',?,?)""",
                (dispatch_no, wo_id, tech['id'], user['id'], now(), 'cancellation guard'),
            )
            dispatch_id = int(
                conn.execute(
                    'SELECT id FROM dispatch_assignments WHERE dispatch_no=?',
                    (dispatch_no,),
                ).fetchone()['id']
            )

        try:
            with db() as conn:
                transition_work_atomic(
                    conn, wo_id, TransitionIn(action='cancel'), admin(conn)
                )
        except WorkflowTransitionConflict as exc:
            assert 'active dispatch' in str(exc)
        else:
            raise AssertionError('cancel succeeded despite active dispatch')

        with db() as conn:
            row = conn.execute(
                'SELECT status FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()
            assert row['status'] == 'Assigned'

        # After the coordinator cancels the dispatch, cancellation proceeds.
        from app.workflow_store import transition_dispatch_atomic
        from app.application import DispatchTransitionIn

        with db() as conn:
            transition_dispatch_atomic(
                conn,
                dispatch_id,
                DispatchTransitionIn(action='cancel'),
                admin(conn),
            )
        with db() as conn:
            transition_work_atomic(
                conn, wo_id, TransitionIn(action='cancel'), admin(conn)
            )
        with db() as conn:
            assert (
                conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (wo_id,)
                ).fetchone()['status']
                == 'Cancelled'
            )


def test_technician_role_cannot_cancel():
    with TestClient(app):
        with db() as conn:
            wo_id, _wo_no = seed_work(conn, 'Draft')
            tech = technician(conn)
        from fastapi import HTTPException

        try:
            with db() as conn2:
                transition_work_atomic(
                    conn2,
                    wo_id,
                    TransitionIn(action='cancel'),
                    {
                        'id': tech['id'],
                        'full_name': tech['full_name'],
                        'username': tech['username'],
                        'role': 'technician',
                    },
                )
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError('technician was allowed to cancel work')


def test_simultaneous_cancels_have_exactly_one_winner():
    workers = 8
    with TestClient(app):
        with db() as conn:
            wo_id, _wo_no = seed_work(conn, 'Submitted')
            user = admin(conn)

        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(workers)

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    transition_work_atomic(
                        conn, wo_id, TransitionIn(action='cancel'), user
                    )
                wins.append(index)
            except WorkflowTransitionConflict:
                conflicts.append(index)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 1 and len(conflicts) == workers - 1

        with db() as conn:
            audits = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='CANCEL'"
                " AND record_id=(SELECT wo_no FROM work_orders WHERE id=?)",
                (wo_id,),
            ).fetchone()[0]
            assert audits == 1
