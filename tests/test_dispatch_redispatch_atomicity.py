from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import DispatchIn
from app.database import db, now
from app.dispatch_store import (
    DispatchAssignmentConflict,
    assign_dispatch_atomic,
    load_dispatch_work_snapshot,
)
from app.main import app


WORKERS = 8


def test_assigned_work_redispatch_snapshot_has_one_winner():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = conn.execute(
                """SELECT u.id,u.full_name,r.code role FROM users u
                   JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
            ).fetchone()
            role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
            seed_hash = conn.execute('SELECT password_hash FROM users ORDER BY id LIMIT 1').fetchone()
            assert user and role and seed_hash
            user = dict(user)
            tech = conn.execute(
                '''INSERT INTO users(
                     username,password_hash,full_name,email,role_id,active,created_at
                   ) VALUES(?,?,?,?,?,1,?)''',
                (
                    f'ci-redispatch-tech-{suffix}',
                    seed_hash['password_hash'],
                    f'CI Redispatch Technician {suffix}',
                    f'ci-redispatch-tech-{suffix}@example.test',
                    role['id'],
                    now(),
                ),
            )
            tech_id = int(tech.lastrowid)
            work = conn.execute(
                '''INSERT INTO work_orders(
                     wo_no,title,priority,status,work_type,requested_by,assigned_to,
                     created_at,updated_at
                   ) VALUES(?,?,'Medium','Assigned','Corrective Maintenance',?,?,?,?)''',
                (
                    f'WO-RDSP-{suffix}',
                    'Concurrent redispatch regression',
                    user['id'],
                    tech_id,
                    now(),
                    now(),
                ),
            )
            work_id = int(work.lastrowid)
            old_dispatch = conn.execute(
                '''INSERT INTO dispatch_assignments(
                     dispatch_no,work_order_id,technician_user_id,dispatched_by,
                     status,eta_minutes,notes,dispatched_at
                   ) VALUES(?,?,?,?,'Dispatched',30,'seed redispatch',?)''',
                (f'DSP-RDSP-{suffix}', work_id, tech_id, user['id'], now()),
            )
            old_dispatch_id = int(old_dispatch.lastrowid)
            snapshot = load_dispatch_work_snapshot(conn, work_id)
            assert snapshot['_dispatch_active_ids'] == (old_dispatch_id,)

        barrier = threading.Barrier(WORKERS)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    assign_dispatch_atomic(
                        conn,
                        snapshot,
                        DispatchIn(
                            technician_user_id=tech_id,
                            eta_minutes=20,
                            notes=f'redispatch {index}',
                        ),
                        user,
                    )
                wins.append(index)
            except DispatchAssignmentConflict:
                conflicts.append(index)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(wins) == 1
        assert len(conflicts) == WORKERS - 1

        with db() as conn:
            old_status = conn.execute(
                'SELECT status FROM dispatch_assignments WHERE id=?',
                (old_dispatch_id,),
            ).fetchone()['status']
            active = int(
                conn.execute(
                    """SELECT COUNT(*) FROM dispatch_assignments
                       WHERE work_order_id=?
                         AND status IN ('Dispatched','Accepted','En Route','On Site')""",
                    (work_id,),
                ).fetchone()[0]
            )
            events = int(
                conn.execute(
                    """SELECT COUNT(*) FROM workflow_events
                       WHERE record_type='work_order' AND record_id=? AND event='DISPATCH'""",
                    (work_id,),
                ).fetchone()[0]
            )
        assert old_status == 'Cancelled'
        assert active == 1
        assert events == 1
