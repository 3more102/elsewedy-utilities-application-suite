from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.application import DispatchTransitionIn
from app.database import db, now
from app.main import app
from app.workflow_store import WorkflowTransitionConflict, transition_dispatch_atomic


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _technician_id(conn) -> int:
    row = conn.execute(
        """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
           WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
    ).fetchone()
    assert row
    return int(row['id'])


def _seed_dispatch(conn, work_status: str, dispatch_status: str) -> tuple[int, str, dict]:
    suffix = uuid.uuid4().hex[:10]
    user = _admin(conn)
    technician_id = _technician_id(conn)
    work = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,assigned_to,
             created_at,updated_at
           ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?,?)''',
        (
            f'WO-DSP-GUARD-{suffix}',
            'Dispatch linked-state guard regression',
            work_status,
            user['id'],
            technician_id,
            now(),
            now(),
        ),
    )
    dispatch_no = f'DSP-GUARD-{suffix}'
    dispatch = conn.execute(
        '''INSERT INTO dispatch_assignments(
             dispatch_no,work_order_id,technician_user_id,dispatched_by,
             status,eta_minutes,notes,dispatched_at
           ) VALUES(?,?,?,?,?,30,'guard regression',?)''',
        (
            dispatch_no,
            int(work.lastrowid),
            technician_id,
            user['id'],
            dispatch_status,
            now(),
        ),
    )
    return int(dispatch.lastrowid), dispatch_no, user


def test_dispatch_arrive_refuses_completed_work_and_rolls_back_claim():
    with TestClient(app):
        with db() as conn:
            dispatch_id, dispatch_no, user = _seed_dispatch(conn, 'Completed', 'En Route')

        with pytest.raises(WorkflowTransitionConflict, match='work order is Completed'):
            with db() as conn:
                transition_dispatch_atomic(
                    conn,
                    dispatch_id,
                    DispatchTransitionIn(action='arrive'),
                    user,
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
        assert dispatch['status'] == 'En Route'
        assert dispatch['arrived_at'] is None
        assert audits == 0


def test_dispatch_accept_refuses_work_outside_execution_lifecycle():
    with TestClient(app):
        with db() as conn:
            dispatch_id, _, user = _seed_dispatch(conn, 'Approved', 'Dispatched')

        with pytest.raises(WorkflowTransitionConflict, match='work order is Approved'):
            with db() as conn:
                transition_dispatch_atomic(
                    conn,
                    dispatch_id,
                    DispatchTransitionIn(action='accept'),
                    user,
                )

        with db() as conn:
            dispatch = conn.execute(
                'SELECT status,accepted_at FROM dispatch_assignments WHERE id=?',
                (dispatch_id,),
            ).fetchone()
        assert dispatch['status'] == 'Dispatched'
        assert dispatch['accepted_at'] is None


def test_dispatch_complete_can_settle_after_work_is_closed():
    with TestClient(app):
        with db() as conn:
            dispatch_id, dispatch_no, user = _seed_dispatch(conn, 'Closed', 'On Site')

        with db() as conn:
            result = transition_dispatch_atomic(
                conn,
                dispatch_id,
                DispatchTransitionIn(action='complete', notes='field record settled'),
                user,
            )

        assert result == {'ok': True, 'status': 'Completed'}
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
        assert dispatch['status'] == 'Completed'
        assert dispatch['completed_at']
        assert audits == 1
