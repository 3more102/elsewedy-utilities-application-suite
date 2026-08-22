from __future__ import annotations

import sys
import threading
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import application as _application
from app.audit_store import ensure_audit_chain_lock
from app.database import db
from app.main import app  # import triggers the production compatibility composition
from app.pm_startup import initialize_pm_generation_support
from app.pm_store import generate_due_pm_atomic
from app.work_order_number_startup import initialize_work_order_number_support


WORKERS = 8


def _bootstrap_race() -> None:
    with db() as conn:
        conn.execute('DROP TABLE IF EXISTS pm_generation_lock')

    barrier = threading.Barrier(WORKERS)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                initialize_pm_generation_support(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('PM bootstrap worker did not finish (possible DDL deadlock)')
    if errors:
        raise RuntimeError(f'PM bootstrap race failed: {errors!r}')
    with db() as conn:
        rows = conn.execute(
            'SELECT id,guard FROM pm_generation_lock'
        ).fetchall()
    if len(rows) != 1 or int(rows[0]['id']) != 1:
        raise RuntimeError(f'PM bootstrap produced invalid coordinator rows: {rows!r}')


def _admin_and_asset():
    with db() as conn:
        ensure_audit_chain_lock(conn)
        initialize_pm_generation_support(conn)
        # The branch-level production composition now protects every WO-number
        # allocation with a shared coordinator. Standalone smokes do not enter
        # FastAPI lifespan, so reproduce that initialization explicitly here.
        initialize_work_order_number_support(conn)
        user = conn.execute(
            """SELECT u.id,u.full_name,r.code role FROM users u
               JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
        ).fetchone()
        asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
        if not user or not asset:
            raise RuntimeError('PM smoke requires seeded admin and asset')
        return dict(user), int(asset['id'])


def main() -> None:
    _bootstrap_race()
    if _application._generate_due_pm is not generate_due_pm_atomic:
        raise RuntimeError('application PM generator was not replaced by atomic wrapper')

    user, asset_id = _admin_and_asset()
    suffix = uuid.uuid4().hex[:10]
    pm_no = f'PM-PG-CAS-{suffix}'
    target = date.today()

    with db() as conn:
        previous = [
            (int(row['id']), int(row['active']))
            for row in conn.execute(
                'SELECT id,active FROM maintenance_plans ORDER BY id'
            ).fetchall()
        ]
        conn.execute('UPDATE maintenance_plans SET active=0')
        created = conn.execute(
            '''INSERT INTO maintenance_plans(
                 pm_no,name,asset_id,trigger_type,interval_days,next_due,
                 priority,job_plan,active,last_meter
               ) VALUES(?,?,?,'Calendar',30,?,'Medium',?,1,0)''',
            (
                pm_no,
                f'PostgreSQL PM concurrency {suffix}',
                asset_id,
                target.isoformat(),
                'PostgreSQL PM generation race',
            ),
        )
        plan_id = int(created.lastrowid)

    barrier = threading.Barrier(WORKERS)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def generate() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                results.append(
                    _application._generate_due_pm(conn, user['id'], target)
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=generate) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('PM generation worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'PM generation race failed: {errors!r}')
    if sum(len(result) for result in results) != 1:
        raise RuntimeError(f'expected one generated work order, got {results!r}')

    with db() as conn:
        works = conn.execute(
            'SELECT id,wo_no,status FROM work_orders WHERE pm_plan_id=?',
            (plan_id,),
        ).fetchall()
        if len(works) != 1:
            raise RuntimeError(f'PM race created {len(works)} work orders')
        work_id = int(works[0]['id'])
        work_no = str(works[0]['wo_no'])
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

        for old_id, active in previous:
            conn.execute(
                'UPDATE maintenance_plans SET active=? WHERE id=?',
                (active, old_id),
            )
        conn.execute('UPDATE maintenance_plans SET active=0 WHERE id=?', (plan_id,))

    if works[0]['status'] != 'Submitted':
        raise RuntimeError(f'generated PM work has status {works[0]["status"]!r}')
    if approvals != 1 or workflows != 1 or audits != 1:
        raise RuntimeError(
            'PM side effects duplicated: '
            f'approvals={approvals} workflows={workflows} audits={audits}'
        )
    if not plan['last_generated'] or str(plan['next_due']) <= target.isoformat():
        raise RuntimeError(f'PM plan was not advanced exactly once: {dict(plan)!r}')
    if work_no not in [value for result in results for value in result]:
        raise RuntimeError('generated work result does not match persisted PM work order')

    print(
        'PM generation concurrency smoke: PASS '
        'bootstrap=8 generators=8 generated=1 approvals=1 workflow=1 audit=1'
    )


if __name__ == '__main__':
    main()
