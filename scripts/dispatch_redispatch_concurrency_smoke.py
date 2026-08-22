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
    ensure_dispatch_assignment_lock,
    load_dispatch_work_snapshot,
)


WORKERS = 8


def create_technician(conn, suffix: str) -> int:
    role = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()
    seed_hash = conn.execute('SELECT password_hash FROM users ORDER BY id LIMIT 1').fetchone()
    if not role or not seed_hash:
        raise RuntimeError('redispatch smoke requires seeded roles/users')
    cur = conn.execute(
        '''INSERT INTO users(
             username,password_hash,full_name,email,role_id,active,created_at
           ) VALUES(?,?,?,?,?,1,?)''',
        (
            f'pg-redispatch-tech-{suffix}',
            seed_hash['password_hash'],
            f'PG Redispatch Technician {suffix}',
            f'pg-redispatch-tech-{suffix}@example.test',
            role['id'],
            now(),
        ),
    )
    return int(cur.lastrowid)


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        ensure_audit_chain_lock(conn)
        ensure_dispatch_assignment_lock(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        if not user:
            raise RuntimeError('redispatch smoke requires seeded admin')
        user = dict(user)
        tech_id = create_technician(conn, suffix)
        work = conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,priority,status,work_type,requested_by,assigned_to,
                 created_at,updated_at
               ) VALUES(?,?,'Medium','Assigned','Corrective Maintenance',?,?,?,?)''',
            (
                f'WO-PG-RDSP-{suffix}',
                'PostgreSQL redispatch race',
                user['id'],
                tech_id,
                now(),
                now(),
            ),
        )
        work_id = int(work.lastrowid)
        old = conn.execute(
            '''INSERT INTO dispatch_assignments(
                 dispatch_no,work_order_id,technician_user_id,dispatched_by,
                 status,eta_minutes,notes,dispatched_at
               ) VALUES(?,?,?,?,'Dispatched',30,'seed redispatch',?)''',
            (f'DSP-PG-RDSP-{suffix}', work_id, tech_id, user['id'], now()),
        )
        old_id = int(old.lastrowid)
        snapshot = load_dispatch_work_snapshot(conn, work_id)
        if tuple(snapshot.get('_dispatch_active_ids', ())) != (old_id,):
            raise RuntimeError('redispatch snapshot did not capture the active generation')

    barrier = threading.Barrier(WORKERS)
    wins: list[int] = []
    conflicts: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                assign_dispatch_atomic(
                    conn,
                    snapshot,
                    DispatchIn(
                        technician_user_id=tech_id,
                        eta_minutes=20,
                        notes=f'pg redispatch {index}',
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
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('redispatch race did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'redispatch worker failed: {errors!r}')
    if len(wins) != 1 or len(conflicts) != WORKERS - 1:
        raise RuntimeError(
            f'redispatch one-winner invariant failed: wins={len(wins)} conflicts={len(conflicts)}'
        )

    with db() as conn:
        old_status = conn.execute(
            'SELECT status FROM dispatch_assignments WHERE id=?', (old_id,)
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
    if old_status != 'Cancelled' or active != 1 or events != 1:
        raise RuntimeError(
            f'redispatch side effects invalid: old={old_status} active={active} events={events}'
        )

    print('dispatch redispatch concurrency smoke: PASS winner=1 active=1')


if __name__ == '__main__':
    main()
