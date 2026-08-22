from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import DispatchIn
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.dispatch_store import (
    DispatchAssignmentConflict,
    assign_dispatch_atomic,
    load_dispatch_work_snapshot,
)


WORKERS = 8


def run_race(operation, workers=WORKERS):
    barrier = threading.Barrier(workers)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
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
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('dispatch assignment race did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'dispatch assignment worker failed: {errors!r}')
    return wins, conflicts


def seed_work(conn, suffix: str, requester_id: int) -> tuple[int, dict]:
    cur = conn.execute(
        '''INSERT INTO work_orders(
             wo_no,title,priority,status,work_type,requested_by,created_at,updated_at
           ) VALUES(?,?,'Medium','Approved','Corrective Maintenance',?,?,?)''',
        (f'WO-PG-DSP-{suffix}', 'PostgreSQL dispatch race', requester_id, now(), now()),
    )
    work_id = int(cur.lastrowid)
    return work_id, load_dispatch_work_snapshot(conn, work_id)


def create_technician(conn, suffix: str) -> int:
    role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
    seed_hash = conn.execute('SELECT password_hash FROM users ORDER BY id LIMIT 1').fetchone()
    if not role or not seed_hash:
        raise RuntimeError('dispatch smoke requires seeded roles/users')
    cur = conn.execute(
        '''INSERT INTO users(
             username,password_hash,full_name,email,role_id,active,created_at
           ) VALUES(?,?,?,?,?,1,?)''',
        (
            f'pg-dispatch-tech-{suffix}',
            seed_hash['password_hash'],
            f'PG Dispatch Tech {suffix}',
            f'pg-dispatch-tech-{suffix}@example.test',
            role['id'],
            now(),
        ),
    )
    return int(cur.lastrowid)


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        ensure_audit_chain_lock(conn)
        admin = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        tech = conn.execute(
            """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
               WHERE r.code='technician' AND u.active=1 ORDER BY u.id LIMIT 1"""
        ).fetchone()
        if not admin or not tech:
            raise RuntimeError('dispatch smoke requires seeded admin and technician')
        user = dict(admin)
        tech_id = int(tech['id'])

        # One technician, many eligible work orders: exactly one assignment wins.
        work_ids = []
        snapshots = []
        for index in range(WORKERS):
            work_id, snapshot = seed_work(conn, f'{suffix}-B-{index}', user['id'])
            work_ids.append(work_id)
            snapshots.append(snapshot)

    def same_tech(index: int) -> None:
        with db() as conn:
            assign_dispatch_atomic(
                conn,
                snapshots[index],
                DispatchIn(technician_user_id=tech_id, notes='busy-race'),
                user,
            )

    wins, conflicts = run_race(same_tech)
    if len(wins) != 1 or len(conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'technician busy race invalid: wins={len(wins)} conflicts={len(conflicts)}'
        )
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
    if active != 1:
        raise RuntimeError(f'technician overbooked: active_dispatches={active}')

    # Same historical work snapshot: the compare-and-swap claim has one winner.
    with db() as conn:
        second_tech = create_technician(conn, suffix + '-S')
        work_id, snapshot = seed_work(conn, suffix + '-SAME', user['id'])

    def same_work(_: int) -> None:
        with db() as conn:
            assign_dispatch_atomic(
                conn,
                snapshot,
                DispatchIn(technician_user_id=second_tech, notes='same-work-race'),
                user,
            )

    wins, conflicts = run_race(same_work)
    if len(wins) != 1 or len(conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'same-work race invalid: wins={len(wins)} conflicts={len(conflicts)}'
        )
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
    if active != 1 or events != 1:
        raise RuntimeError(
            f'same-work side effects duplicated: active={active} events={events}'
        )

    # Independent assignments may all succeed, but DSP numbers must remain unique.
    independent_workers = 4
    independent_techs = []
    independent_works = []
    independent_snapshots = []
    with db() as conn:
        for index in range(independent_workers):
            independent_techs.append(create_technician(conn, f'{suffix}-N-{index}'))
            work_id, snapshot = seed_work(conn, f'{suffix}-N-{index}', user['id'])
            independent_works.append(work_id)
            independent_snapshots.append(snapshot)

    def independent(index: int) -> None:
        with db() as conn:
            assign_dispatch_atomic(
                conn,
                independent_snapshots[index],
                DispatchIn(technician_user_id=independent_techs[index], notes='number-race'),
                user,
            )

    wins, conflicts = run_race(independent, workers=independent_workers)
    if len(wins) != independent_workers or conflicts:
        raise RuntimeError(
            f'independent assignments unexpectedly conflicted: wins={wins} conflicts={conflicts}'
        )
    with db() as conn:
        rows = conn.execute(
            "SELECT dispatch_no FROM dispatch_assignments WHERE work_order_id IN ({})".format(
                ','.join('?' for _ in independent_works)
            ),
            tuple(independent_works),
        ).fetchall()
    numbers = [row['dispatch_no'] for row in rows]
    if len(numbers) != independent_workers or len(set(numbers)) != independent_workers:
        raise RuntimeError(f'dispatch number allocation collided: {numbers!r}')

    print(
        'dispatch assignment concurrency smoke: PASS '
        f'busy_winner=1 same_work_winner=1 independent={independent_workers}'
    )


if __name__ == '__main__':
    main()
