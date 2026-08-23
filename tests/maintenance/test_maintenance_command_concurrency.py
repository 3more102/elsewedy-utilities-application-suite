from __future__ import annotations

from threading import Barrier, Thread

from app.auth import hash_password
from apps.maintenance import DispatchConflict, InvalidWorkTransition, WorkOrderConflict, create_dispatch, create_work_order, transition_work_order
from core.database import db, init_db, now


def _user(conn, username: str) -> dict:
    row = conn.execute(
        '''SELECT u.id,u.username,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username=?''',
        (username,),
    ).fetchone()
    assert row
    return dict(row)


def _create_technician(conn, username: str) -> dict:
    role_id = conn.execute("SELECT id FROM roles WHERE code='technician'").fetchone()['id']
    conn.execute(
        '''INSERT OR IGNORE INTO users(username,password_hash,full_name,email,role_id,department,phone,active,created_at)
           VALUES(?,?,?,?,?,'Maintenance','',1,?)''',
        (username, hash_password('test-only-password'), username, f'{username}@example.test', role_id, now()),
    )
    return _user(conn, username)


def _approved_work(conn, admin: dict, *, title: str) -> dict:
    created = create_work_order(conn, {'title': title, 'priority': 'High'}, admin)
    transition_work_order(conn, created['id'], 'submit', admin)
    transition_work_order(conn, created['id'], 'approve', admin)
    return created


def test_two_stale_start_commands_cannot_both_advance_same_work_order():
    init_db(hash_password)
    with db() as conn:
        admin = _user(conn, 'omar')
        tech = _user(conn, 'tech1')
        work = _approved_work(conn, admin, title='Concurrent maintenance start regression')
        conn.execute('UPDATE work_orders SET assigned_to=? WHERE id=?', (tech['id'], work['id']))
        transition_work_order(conn, work['id'], 'assign', admin)

    barrier = Barrier(2)
    outcomes: list[str] = []
    errors: list[Exception] = []

    def start_once() -> None:
        try:
            barrier.wait()
            with db() as conn:
                actor = _user(conn, 'omar')
                transition_work_order(conn, work['id'], 'start', actor)
                outcomes.append('started')
        except (InvalidWorkTransition, WorkOrderConflict):
            outcomes.append('conflict')
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [Thread(target=start_once), Thread(target=start_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert sorted(outcomes) == ['conflict', 'started']
    with db() as conn:
        row = conn.execute('SELECT status,actual_start FROM work_orders WHERE id=?', (work['id'],)).fetchone()
        assert row['status'] == 'In Progress' and row['actual_start']
        events = conn.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE record_type='work_order' AND record_id=? AND event='START'",
            (work['id'],),
        ).fetchone()[0]
        assert events == 1


def test_two_dispatchers_cannot_both_assign_same_work_order():
    init_db(hash_password)
    suffix = str(abs(hash(__name__)) % 100000)
    with db() as conn:
        admin = _user(conn, 'omar')
        tech_a = _create_technician(conn, f'race-tech-a-{suffix}')
        tech_b = _create_technician(conn, f'race-tech-b-{suffix}')
        work = _approved_work(conn, admin, title='Concurrent dispatch ownership regression')

    barrier = Barrier(2)
    successes: list[int] = []
    conflicts: list[str] = []
    errors: list[Exception] = []

    def dispatch_once(technician_id: int) -> None:
        try:
            barrier.wait()
            with db() as conn:
                actor = _user(conn, 'omar')
                result = create_dispatch(conn, work['id'], technician_id, actor, notes='race')
                successes.append(result['id'])
        except DispatchConflict as exc:
            conflicts.append(str(exc))
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [Thread(target=dispatch_once, args=(tech_a['id'],)), Thread(target=dispatch_once, args=(tech_b['id'],))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(successes) == 1 and len(conflicts) == 1
    with db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM dispatch_assignments WHERE work_order_id=? AND status IN ('Dispatched','Accepted','En Route','On Site')",
            (work['id'],),
        ).fetchone()[0]
        current = conn.execute('SELECT status,assigned_to FROM work_orders WHERE id=?', (work['id'],)).fetchone()
        assert active == 1
        assert current['status'] == 'Assigned'
        assert current['assigned_to'] in (tech_a['id'], tech_b['id'])
