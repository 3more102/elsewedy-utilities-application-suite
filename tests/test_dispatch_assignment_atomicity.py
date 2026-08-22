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


def admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def technician_id(conn) -> int:
    row = conn.execute(
        """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
           WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
    ).fetchone()
    assert row
    return int(row['id'])


def seed_work(conn, suffix: str, requester_id: int, status: str = 'Approved') -> int:
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,created_at,updated_at
           ) VALUES(?,?,'Medium',?,'Corrective Maintenance',?,?,?)''',
        (f'WO-DSP-{suffix}', 'Dispatch assignment race', status, requester_id, now(), now()),
    )
    return int(cur.lastrowid)


def run_race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            operation(index)
            wins.append(index)
        except DispatchAssignmentConflict:
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


def test_same_technician_cannot_be_concurrently_dispatched_to_multiple_work_orders():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = admin(conn)
            tech_id = technician_id(conn)
            work_ids = [seed_work(conn, f'{suffix}-{i}', user['id']) for i in range(WORKERS)]
            snapshots = [load_dispatch_work_snapshot(conn, work_id) for work_id in work_ids]

        def assign(index: int) -> None:
            with db() as conn:
                assign_dispatch_atomic(
                    conn,
                    snapshots[index],
                    DispatchIn(technician_user_id=tech_id, eta_minutes=30, notes='race'),
                    user,
                )

        wins, conflicts = run_race(assign)
        assert len(wins) == 1
        assert len(conflicts) == WORKERS - 1

        with db() as conn:
            active = int(
                conn.execute(
                    """SELECT COUNT(*) FROM dispatch_assignments
                       WHERE technician_user_id=?
                         AND work_order_id IN ({})
                         AND status IN ('Dispatched','Accepted','En Route','On Site')""".format(
                        ','.join('?' for _ in work_ids)
                    ),
                    (tech_id, *work_ids),
                ).fetchone()[0]
            )
            assigned = int(
                conn.execute(
                    "SELECT COUNT(*) FROM work_orders WHERE id IN ({}) AND assigned_to=?".format(
                        ','.join('?' for _ in work_ids)
                    ),
                    (*work_ids, tech_id),
                ).fetchone()[0]
            )
        assert active == 1
        assert assigned == 1


def test_same_work_snapshot_has_exactly_one_assignment_winner():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = admin(conn)
            tech_id = technician_id(conn)
            work_id = seed_work(conn, suffix, user['id'])
            snapshot = load_dispatch_work_snapshot(conn, work_id)

        def assign(_: int) -> None:
            with db() as conn:
                assign_dispatch_atomic(
                    conn,
                    snapshot,
                    DispatchIn(technician_user_id=tech_id, eta_minutes=15, notes='same-work'),
                    user,
                )

        wins, conflicts = run_race(assign)
        assert len(wins) == 1
        assert len(conflicts) == WORKERS - 1

        with db() as conn:
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
        assert active == 1
        assert events == 1


def test_stale_work_snapshot_cannot_dispatch_terminal_work():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user = admin(conn)
            tech_id = technician_id(conn)
            work_id = seed_work(conn, suffix, user['id'])
            snapshot = load_dispatch_work_snapshot(conn, work_id)
            conn.execute(
                "UPDATE work_orders SET status='Closed',updated_at=? WHERE id=?",
                (now(), work_id),
            )

        with db() as conn:
            try:
                assign_dispatch_atomic(
                    conn,
                    snapshot,
                    DispatchIn(technician_user_id=tech_id, notes='stale'),
                    user,
                )
            except DispatchAssignmentConflict:
                pass
            else:
                raise AssertionError('stale terminal work snapshot unexpectedly dispatched')

        with db() as conn:
            count = int(
                conn.execute(
                    'SELECT COUNT(*) FROM dispatch_assignments WHERE work_order_id=?',
                    (work_id,),
                ).fetchone()[0]
            )
        assert count == 0


def test_independent_assignments_get_unique_dispatch_numbers():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        workers = 4
        with db() as conn:
            user = admin(conn)
            role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
            seed_hash = conn.execute('SELECT password_hash FROM users ORDER BY id LIMIT 1').fetchone()
            assert role and seed_hash
            tech_ids = []
            work_ids = []
            snapshots = []
            for index in range(workers):
                tech = conn.execute(
                    '''INSERT INTO users(
                         username,password_hash,full_name,email,role_id,active,created_at
                       ) VALUES(?,?,?,?,?,1,?)''',
                    (
                        f'ci-tech-{suffix}-{index}',
                        seed_hash['password_hash'],
                        f'CI Technician {index}',
                        f'ci-tech-{suffix}-{index}@example.test',
                        role['id'],
                        now(),
                    ),
                )
                tech_ids.append(int(tech.lastrowid))
                work_id = seed_work(conn, f'{suffix}-N-{index}', user['id'])
                work_ids.append(work_id)
                snapshots.append(load_dispatch_work_snapshot(conn, work_id))

        def assign(index: int) -> None:
            with db() as conn:
                assign_dispatch_atomic(
                    conn,
                    snapshots[index],
                    DispatchIn(technician_user_id=tech_ids[index], notes='number-race'),
                    user,
                )

        wins, conflicts = run_race(assign, workers=workers)
        assert len(wins) == workers
        assert conflicts == []

        with db() as conn:
            rows = conn.execute(
                "SELECT dispatch_no FROM dispatch_assignments WHERE work_order_id IN ({})".format(
                    ','.join('?' for _ in work_ids)
                ),
                tuple(work_ids),
            ).fetchall()
        numbers = [row['dispatch_no'] for row in rows]
        assert len(numbers) == workers
        assert len(set(numbers)) == workers
