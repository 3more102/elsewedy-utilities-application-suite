from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import InspectionSubmit
from app.audit_store import ensure_audit_chain_lock
from app.database import db, now
from app.inspection_store import submit_inspection_atomic
from app.main import app  # noqa: F401 - install production compatibility composition
from app.work_order_number_startup import initialize_work_order_number_support


WORKERS = 8


def _admin(conn) -> dict:
    ensure_audit_chain_lock(conn)
    initialize_work_order_number_support(conn)
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    if not row:
        raise RuntimeError('inspection smoke requires seeded admin')
    return dict(row)


def _seed_inspection(conn, suffix: str):
    user = _admin(conn)
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    if not asset:
        raise RuntimeError('inspection smoke requires seeded asset')
    inspection_no = f'INS-PG-CAS-{suffix}'
    inspection = conn.execute(
        '''INSERT INTO inspections(
             inspection_no,template_name,asset_id,inspector_id,status,remarks,created_at
           ) VALUES(?,?,?,?,'Draft','',?)''',
        (
            inspection_no,
            f'PostgreSQL inspection race {suffix}',
            asset['id'],
            user['id'],
            now(),
        ),
    )
    item = conn.execute(
        '''INSERT INTO inspection_items(
             inspection_id,item_name,response,reading,remarks
           ) VALUES(?,?,'','','')''',
        (inspection.lastrowid, f'PostgreSQL inspected component {suffix}'),
    )
    return user, int(inspection.lastrowid), int(item.lastrowid), inspection_no


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            results.append(operation())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError(
            'inspection concurrency worker did not finish (possible deadlock)'
        )
    if errors:
        raise RuntimeError(f'inspection concurrency worker failed: {errors!r}')
    if len(results) != workers:
        raise RuntimeError(
            f'inspection race returned {len(results)} results for {workers} workers'
        )
    return results


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    with db() as conn:
        user, inspection_id, item_id, inspection_no = _seed_inspection(conn, suffix)

    fail_body = InspectionSubmit(
        responses=[
            {
                'id': item_id,
                'response': 'Fail',
                'reading': 'bad',
                'remarks': 'PostgreSQL concurrent failure',
            }
        ],
        remarks='PostgreSQL failed inspection',
        create_corrective_on_fail=True,
    )

    def submit_failure():
        with db() as conn:
            return submit_inspection_atomic(
                conn, inspection_id, fail_body, user
            )

    results = _race(submit_failure)
    if any(result.get('result') != 'Fail' for result in results):
        raise RuntimeError(f'inspection race returned inconsistent results: {results!r}')
    corrective_ids = {
        int(result['corrective_work_order_id'])
        for result in results
        if result.get('corrective_work_order_id') is not None
    }
    if len(corrective_ids) != 1:
        raise RuntimeError(
            f'inspection race returned multiple corrective ids: {corrective_ids!r}'
        )
    corrective_id = next(iter(corrective_ids))

    with db() as conn:
        inspection = conn.execute(
            '''SELECT status,result,corrective_wo_id,remarks
               FROM inspections WHERE id=?''',
            (inspection_id,),
        ).fetchone()
        item = conn.execute(
            'SELECT response,reading,remarks FROM inspection_items WHERE id=?',
            (item_id,),
        ).fetchone()
        corrective_rows = int(
            conn.execute(
                'SELECT COUNT(*) FROM work_orders WHERE title=?',
                (f'Corrective action from {inspection_no}',),
            ).fetchone()[0]
        )
        notifications = int(
            conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE title='Inspection failed'
                     AND link_module='inspections' AND link_id=?""",
                (inspection_no,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Inspections' AND action='SUBMIT'
                     AND record_id=?""",
                (inspection_no,),
            ).fetchone()[0]
        )

    if (
        inspection['status'] != 'Completed'
        or inspection['result'] != 'Fail'
        or int(inspection['corrective_wo_id']) != corrective_id
        or item['response'] != 'Fail'
        or item['reading'] != 'bad'
        or corrective_rows != 1
        or notifications != 1
        or audits != 1
    ):
        raise RuntimeError(
            'inspection failure invariant failed: '
            f'inspection={dict(inspection)!r} item={dict(item)!r} '
            f'corrective_rows={corrective_rows} notifications={notifications} '
            f'audits={audits}'
        )

    # A terminal retry with contradictory data must replay persisted evidence
    # rather than mutating responses/result or creating another corrective.
    retry = InspectionSubmit(
        responses=[
            {
                'id': item_id,
                'response': 'Pass',
                'reading': '999',
                'remarks': 'must not overwrite',
            }
        ],
        remarks='must not overwrite completed inspection',
        create_corrective_on_fail=False,
    )
    with db() as conn:
        replay = submit_inspection_atomic(conn, inspection_id, retry, user)
    if replay != {
        'ok': True,
        'result': 'Fail',
        'corrective_work_order_id': corrective_id,
    }:
        raise RuntimeError(f'terminal retry did not replay persisted result: {replay!r}')

    with db() as conn:
        inspection = conn.execute(
            'SELECT result,remarks,corrective_wo_id FROM inspections WHERE id=?',
            (inspection_id,),
        ).fetchone()
        item = conn.execute(
            'SELECT response,reading,remarks FROM inspection_items WHERE id=?',
            (item_id,),
        ).fetchone()
        corrective_rows = int(
            conn.execute(
                'SELECT COUNT(*) FROM work_orders WHERE title=?',
                (f'Corrective action from {inspection_no}',),
            ).fetchone()[0]
        )
        notifications = int(
            conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE title='Inspection failed'
                     AND link_module='inspections' AND link_id=?""",
                (inspection_no,),
            ).fetchone()[0]
        )
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Inspections' AND action='SUBMIT'
                     AND record_id=?""",
                (inspection_no,),
            ).fetchone()[0]
        )

    if (
        inspection['result'] != 'Fail'
        or inspection['remarks'] != 'PostgreSQL failed inspection'
        or int(inspection['corrective_wo_id']) != corrective_id
        or item['response'] != 'Fail'
        or item['reading'] != 'bad'
        or corrective_rows != 1
        or notifications != 1
        or audits != 1
    ):
        raise RuntimeError(
            'terminal inspection retry mutated committed evidence: '
            f'inspection={dict(inspection)!r} item={dict(item)!r} '
            f'corrective_rows={corrective_rows} notifications={notifications} '
            f'audits={audits}'
        )

    print(
        'inspection submission concurrency smoke: PASS '
        'workers=8 corrective=1 notification=1 audit=1 terminal_replay=stable'
    )


if __name__ == '__main__':
    main()
