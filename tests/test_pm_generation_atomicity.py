from __future__ import annotations

import threading
import uuid
from datetime import date

from fastapi.testclient import TestClient

from app import application as _application
from app.database import db, now
from app.main import app
from app.pm_store import generate_due_pm_atomic


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_due_plan(conn, suffix: str) -> tuple[int, str, dict]:
    user = _admin(conn)
    asset = conn.execute(
        'SELECT id FROM assets ORDER BY id LIMIT 1'
    ).fetchone()
    assert asset
    pm_no = f'PM-CAS-{suffix}'
    created = conn.execute(
        '''INSERT INTO maintenance_plans(
             pm_no,name,asset_id,trigger_type,interval_days,next_due,
             priority,job_plan,active,last_meter
           ) VALUES(?,?,?,'Calendar',30,?,'Medium',?,1,0)''',
        (
            pm_no,
            f'PM concurrency {suffix}',
            asset['id'],
            date.today().isoformat(),
            'Atomic PM generation regression',
        ),
    )
    return int(created.lastrowid), pm_no, user


def test_shared_pm_generator_is_replaced_by_atomic_wrapper():
    assert _application._generate_due_pm is generate_due_pm_atomic


def test_concurrent_due_pm_generation_creates_one_work_order_and_side_effect_set():
    suffix = uuid.uuid4().hex[:10]
    with TestClient(app):
        with db() as conn:
            # Isolate the generator from seeded plans while preserving their
            # exact active states for the rest of the regression suite.
            previous = {
                int(row['id']): int(row['active'])
                for row in conn.execute(
                    'SELECT id,active FROM maintenance_plans'
                ).fetchall()
            }
            conn.execute('UPDATE maintenance_plans SET active=0')
            plan_id, pm_no, user = _seed_due_plan(conn, suffix)

        barrier = threading.Barrier(WORKERS)
        results: list[list[str]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(
                        _application._generate_due_pm(
                            conn, user['id'], date.today()
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert sum(len(result) for result in results) == 1

        with db() as conn:
            works = conn.execute(
                'SELECT id,wo_no,status FROM work_orders WHERE pm_plan_id=?',
                (plan_id,),
            ).fetchall()
            assert len(works) == 1
            work_id = int(works[0]['id'])
            work_no = works[0]['wo_no']
            assert works[0]['status'] == 'Submitted'

            approvals = int(
                conn.execute(
                    """SELECT COUNT(*) FROM approval_requests
                       WHERE record_type='work_order' AND record_id=?""",
                    (work_id,),
                ).fetchone()[0]
            )
            workflows = int(
                conn.execute(
                    """SELECT COUNT(*) FROM workflow_events
                       WHERE record_type='work_order' AND record_id=?
                         AND event='AUTO SUBMIT'""",
                    (work_id,),
                ).fetchone()[0]
            )
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Preventive Maintenance'
                         AND action='GENERATE WO' AND record_id=?""",
                    (pm_no,),
                ).fetchone()[0]
            )
            plan = conn.execute(
                'SELECT next_due,last_generated FROM maintenance_plans WHERE id=?',
                (plan_id,),
            ).fetchone()

            assert approvals == 1
            assert workflows == 1
            assert audits == 1
            assert plan['last_generated']
            assert str(plan['next_due']) > date.today().isoformat()
            assert work_no in [value for result in results for value in result]

            # Restore pre-existing plan activation exactly; leave the unique
            # regression plan inactive so it cannot affect later tests.
            for old_id, active in previous.items():
                conn.execute(
                    'UPDATE maintenance_plans SET active=? WHERE id=?',
                    (active, old_id),
                )
            conn.execute(
                'UPDATE maintenance_plans SET active=0 WHERE id=?',
                (plan_id,),
            )
