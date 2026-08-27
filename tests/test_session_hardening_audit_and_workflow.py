"""Session hardening tests: audit chain integrity and work-order CAS edge cases.

Covers the specific gaps identified in the production audit:
- Audit chain: concurrent writers, no fork, anchor match, tamper, idempotent init
- Work-order CAS: approve race, dispatch vs cancel, complete vs hold, stale reject
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit_store import append_audit, ensure_audit_chain_lock
from app.database import audit_digest, db, now
from app.main import app, verify_audit_chain
from app.workflow_store import (
    WorkflowTransitionConflict,
    transition_work_atomic,
)

from app.application import (
    TransitionIn,
)

THREADS = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_work(conn, suffix, status, assigned_to=None, supervisor_id=None):
    user = _admin(conn)
    number = f'WO-SH-{suffix}'
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,assigned_to,
             supervisor_id,created_at,updated_at
           ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?,?,?)''',
        (number, 'Session hardening work', status, user['id'],
         assigned_to, supervisor_id, now(), now()),
    )
    return int(cur.lastrowid), number, user


def _count_audit_for(conn, module, action, record_id) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE module=? AND action=? AND record_id=?",
        (module, action, record_id),
    ).fetchone()[0])


def _assert_linear_chain(conn) -> int:
    rows = conn.execute(
        '''SELECT id,user_id,action,module,record_id,old_value,new_value,
                  created_at,prev_hash,audit_hash
           FROM audit_logs ORDER BY id'''
    ).fetchall()
    previous = ''
    for row in rows:
        assert (row['prev_hash'] or '') == previous
        expected = audit_digest(
            previous, row['user_id'], row['action'], row['module'],
            row['record_id'], row['old_value'], row['new_value'], row['created_at'],
        )
        assert row['audit_hash'] == expected
        previous = row['audit_hash']
    return len(rows)


# ===================================================================
# 1. AUDIT CHAIN TESTS
# ===================================================================

class TestAuditChainHardening:

    def test_normal_append_through_application_audit(self):
        """application.audit() delegates to canonical append_audit()."""
        module = 'SH-AuditNormal'
        with TestClient(app):
            with db() as conn:
                user_id = _admin(conn)['id']
                before = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0])
                digest = append_audit(conn, user_id, 'TEST_ACTION', module, 'rec-1', '', '{"x":1}')
                after = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0])
                verified = verify_audit_chain(conn)

            assert after == before + 1
            assert isinstance(digest, str) and len(digest) == 64
            assert verified['valid'] is True

    def test_two_concurrent_audit_writers_produce_one_linear_chain(self):
        """Eight concurrent append_audit calls produce exactly one non-forked chain."""
        module = 'SH-AuditConcurrent'
        barrier = threading.Barrier(THREADS)
        errors: list[BaseException] = []

        with TestClient(app):
            with db() as conn:
                user_id = _admin(conn)['id']
                total_before = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs'
                ).fetchone()[0])

            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        append_audit(
                            conn, user_id, 'CONCURRENT', module,
                            str(index), {'i': index}, {'i': index + 1},
                        )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []

            with db() as conn:
                tagged = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0])
                total_after = _assert_linear_chain(conn)

            assert tagged == THREADS
            assert total_after == total_before + THREADS

    def test_no_chain_fork_under_concurrency(self):
        """No two records share the same prev_hash (no fork)."""
        module = 'SH-AuditFork'
        barrier = threading.Barrier(THREADS)
        errors: list[BaseException] = []

        with TestClient(app):
            with db() as conn:
                user_id = _admin(conn)['id']

            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        append_audit(conn, user_id, 'FORK_TEST', module, str(index), '', '')
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []

            with db() as conn:
                prev_hashes = [row[0] for row in conn.execute(
                    "SELECT prev_hash FROM audit_logs WHERE module=? ORDER BY id",
                    (module,),
                ).fetchall()]
                # No two consecutive records should share a prev_hash fork
                # (each record's prev_hash is the previous record's audit_hash)
                for i in range(1, len(prev_hashes)):
                    # prev_hash[i] should match audit_hash[i-1]
                    prev_audit = conn.execute(
                        "SELECT audit_hash FROM audit_logs WHERE module=? ORDER BY id LIMIT 1 OFFSET ?",
                        (module, i - 1),
                    ).fetchone()
                    assert prev_hashes[i] == prev_audit[0]

    def test_anchor_matches_final_record(self):
        """The audit_chain_anchor head_hash matches the last record's hash."""
        module = 'SH-AuditAnchor'
        with TestClient(app):
            with db() as conn:
                user_id = _admin(conn)['id']
                for i in range(5):
                    append_audit(conn, user_id, f'ANCHOR_{i}', module, str(i), '', '')
                last = conn.execute(
                    "SELECT audit_hash FROM audit_logs WHERE module=? ORDER BY id DESC LIMIT 1",
                    (module,),
                ).fetchone()
                anchor = conn.execute(
                    'SELECT head_hash,record_count FROM audit_chain_anchor WHERE id=1',
                ).fetchone()
                total = int(conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0])
                verified = verify_audit_chain(conn)

            assert anchor is not None
            assert anchor[0] == last[0]
            assert anchor[1] == total
            assert verified['valid'] is True

    def test_tamper_detection_after_mutation(self):
        """Modifying an old_value in the chain is detected by verification."""
        module = 'SH-AuditTamper'
        with TestClient(app):
            with db() as conn:
                user_id = _admin(conn)['id']
                append_audit(conn, user_id, 'TAMPER', module, '1', '', '{"ok":true}')
                append_audit(conn, user_id, 'TAMPER', module, '2', '', '{"ok":true}')
                append_audit(conn, user_id, 'TAMPER', module, '3', '', '{"ok":true}')
                # Capture the middle record (the one we'll tamper)
                middle = conn.execute(
                    "SELECT id,old_value FROM audit_logs WHERE module=? ORDER BY id LIMIT 1 OFFSET 1",
                    (module,),
                ).fetchone()
                tamper_id = middle[0]
                original_old_value = middle[1]

            # Tamper: mutate old_value of the middle record
            with db() as conn:
                conn.execute(
                    "UPDATE audit_logs SET old_value='TAMPERED' WHERE id=?",
                    (tamper_id,),
                )

            with db() as conn:
                result = verify_audit_chain(conn)
            assert result['valid'] is False
            assert result['first_invalid_id'] is not None

            # Restore the chain so downstream tests remain valid
            with db() as conn:
                conn.execute(
                    "UPDATE audit_logs SET old_value=? WHERE id=?",
                    (original_old_value, tamper_id),
                )

    def test_initialization_idempotent_under_concurrency(self):
        """Multiple concurrent ensure_audit_chain_lock calls are idempotent."""
        with TestClient(app):
            barrier = threading.Barrier(THREADS)
            errors: list[BaseException] = []

            def worker():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        ensure_audit_chain_lock(conn)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            with db() as conn:
                row = conn.execute('SELECT id FROM audit_chain_lock WHERE id=1').fetchone()
                anchor = conn.execute('SELECT id FROM audit_chain_anchor WHERE id=1').fetchone()
            assert row is not None
            assert anchor is not None


# ===================================================================
# 2. WORK-ORDER TRANSITION CAS TESTS
# ===================================================================

class TestWorkOrderCASHardening:

    def test_two_simultaneous_approve_attempts_one_wins(self):
        """Two concurrent approvals from the same Submitted WO produce one winner."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'Submitted')
                # Create an approval request for this work order
                conn.execute(
                    '''INSERT INTO approval_requests(
                         approval_no,module,record_type,record_id,record_code,title,
                         requested_by,assigned_role,status,requested_at
                       ) VALUES(?,?,?,?,?,?,?,'maintenance_manager','Pending',?)''',
                    (f'APR-SH-{suffix}', 'Work Management', 'work_order', work_id,
                     work_no, f'Approve {work_no}', user['id'], now()),
                )

            barrier = threading.Barrier(2)
            wins, conflicts = [], []
            errors = []

            def approve_a():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='approve'), user,
                        )
                    wins.append('a')
                except WorkflowTransitionConflict:
                    conflicts.append('a')
                except BaseException as exc:
                    errors.append(exc)

            def approve_b():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='approve'), user,
                        )
                    wins.append('b')
                except WorkflowTransitionConflict:
                    conflicts.append('b')
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=approve_a), threading.Thread(target=approve_b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            assert len(wins) == 1
            assert len(conflicts) == 1
            with db() as conn:
                status = conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (work_id,),
                ).fetchone()['status']
                audit_count = _count_audit_for(conn, 'Work Management', 'APPROVE', work_no)
            assert status == 'Approved'
            assert audit_count == 1

    def test_dispatch_vs_cancel_race_one_wins(self):
        """Cancel racing with dispatch produces one coherent outcome."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                tech = conn.execute(
                    """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
                       WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
                ).fetchone()
                assert tech
                work_id, work_no, user = _seed_work(
                    conn, suffix, 'Assigned', assigned_to=tech['id'],
                )
                # Create dispatch assignment
                conn.execute(
                    '''INSERT INTO dispatch_assignments(
                         dispatch_no,work_order_id,technician_user_id,dispatched_by,
                         status,dispatched_at
                       ) VALUES(?,?,?,?,'Dispatched',?)''',
                    (f'DSP-SH-{suffix}', work_id, tech['id'], user['id'], now()),
                )

            barrier = threading.Barrier(2)
            outcomes = []
            errors = []

            def cancel():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='cancel'), user,
                        )
                    outcomes.append('cancelled')
                except WorkflowTransitionConflict:
                    outcomes.append('cancel_conflict')
                except BaseException as exc:
                    errors.append(exc)

            def accept_dispatch():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        from app.workflow_store import transition_dispatch_atomic
                        from app.application import DispatchTransitionIn
                        dispatch = conn.execute(
                            "SELECT id FROM dispatch_assignments WHERE dispatch_no=?",
                            (f'DSP-SH-{suffix}',),
                        ).fetchone()
                        transition_dispatch_atomic(
                            conn, dispatch['id'],
                            DispatchTransitionIn(action='accept'), user,
                        )
                    outcomes.append('dispatched')
                except WorkflowTransitionConflict:
                    outcomes.append('dispatch_conflict')
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=cancel), threading.Thread(target=accept_dispatch)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            # Exactly one of the two operations should succeed
            assert sum(x in ('cancelled', 'dispatched') for x in outcomes) == 1
            with db() as conn:
                final = conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (work_id,),
                ).fetchone()['status']
                assert final in ('Cancelled', 'In Progress', 'Assigned')

    def test_complete_vs_hold_race_legal_setup(self):
        """Complete racing with pause from In Progress: exactly one wins."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'In Progress')

            barrier = threading.Barrier(2)
            wins, conflicts = [], []
            errors = []

            def complete():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id,
                            TransitionIn(action='complete', notes='done'),
                            user,
                        )
                    wins.append('complete')
                except WorkflowTransitionConflict:
                    conflicts.append('complete')
                except BaseException as exc:
                    errors.append(exc)

            def pause():
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='pause'), user,
                        )
                    wins.append('pause')
                except WorkflowTransitionConflict:
                    conflicts.append('pause')
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=complete), threading.Thread(target=pause)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            assert len(wins) == 1
            assert len(conflicts) == 1
            with db() as conn:
                final_status = conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (work_id,),
                ).fetchone()['status']
            assert final_status in ('Completed', 'Assigned')

    def test_stale_transition_rejected(self):
        """Transition from wrong status is rejected with 409."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'Draft')
            with pytest.raises(WorkflowTransitionConflict):
                with db() as conn:
                    transition_work_atomic(
                        conn, work_id, TransitionIn(action='complete'), user,
                    )

    def test_one_winning_mutation_only(self):
        """Eight concurrent submits from Draft produce exactly one mutation."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'Draft')

            barrier = threading.Barrier(THREADS)
            wins, conflicts = [], []
            errors = []

            def submit(_):
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='submit'), user,
                        )
                    wins.append(1)
                except WorkflowTransitionConflict:
                    conflicts.append(1)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=submit, args=(i,)) for i in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            assert len(wins) == 1
            assert len(conflicts) == THREADS - 1
            with db() as conn:
                assert conn.execute(
                    'SELECT status FROM work_orders WHERE id=?', (work_id,),
                ).fetchone()['status'] == 'Submitted'
                audit_count = _count_audit_for(conn, 'Work Management', 'SUBMIT', work_no)
                workflow_count = int(conn.execute(
                    "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='SUBMIT'",
                    (work_id,),
                ).fetchone()[0])
            assert audit_count == 1
            assert workflow_count == 1

    def test_audit_generated_only_for_successful_mutation(self):
        """Losing concurrent transitions do not generate audit records."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'Draft')

            barrier = threading.Barrier(THREADS)
            wins, conflicts = [], []
            errors = []

            def submit(_):
                try:
                    barrier.wait(timeout=10)
                    with db() as conn:
                        transition_work_atomic(
                            conn, work_id, TransitionIn(action='submit'), user,
                        )
                    wins.append(1)
                except WorkflowTransitionConflict:
                    conflicts.append(1)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=submit, args=(i,)) for i in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not any(t.is_alive() for t in threads)
            assert errors == []
            with db() as conn:
                audit_count = _count_audit_for(conn, 'Work Management', 'SUBMIT', work_no)
                workflow_count = int(conn.execute(
                    "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='SUBMIT'",
                    (work_id,),
                ).fetchone()[0])
                approval_count = int(conn.execute(
                    "SELECT COUNT(*) FROM approval_requests WHERE record_type='work_order' AND record_id=?",
                    (work_id,),
                ).fetchone()[0])
            # Exactly one of each side-effect type from the winning transition
            assert audit_count == 1
            assert workflow_count == 1
            assert approval_count == 1

    def test_complete_generates_audit_record(self):
        """Complete transition generates exactly one audit record."""
        with TestClient(app):
            suffix = uuid.uuid4().hex[:10]
            with db() as conn:
                work_id, work_no, user = _seed_work(conn, suffix, 'In Progress')
                before_total = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs'
                ).fetchone()[0])
            with db() as conn:
                transition_work_atomic(
                    conn, work_id,
                    TransitionIn(action='complete', notes='test completion'),
                    user,
                )
                audit_count = _count_audit_for(conn, 'Work Management', 'COMPLETE', work_no)
                after_total = int(conn.execute(
                    'SELECT COUNT(*) FROM audit_logs'
                ).fetchone()[0])
            assert audit_count == 1
            assert after_total == before_total + 1
