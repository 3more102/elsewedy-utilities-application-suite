from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import InspectionSubmit
from app.authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY
from app.database import db, now
from app.inspection_authorization import INSPECTION_SUBMIT_ROLES
from app.inspection_store import submit_inspection_atomic
from app.main import app


WORKERS = 8


def _admin(conn) -> dict:
    row = conn.execute(
        """SELECT u.id,u.full_name,r.code role FROM users u
           JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
    ).fetchone()
    assert row
    return dict(row)


def _seed_inspection(conn, suffix: str) -> tuple[dict, int, int, str]:
    user = _admin(conn)
    asset = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()
    assert asset
    inspection_no = f'INS-CAS-{suffix}'
    inspection = conn.execute(
        '''INSERT INTO inspections(
             inspection_no,template_name,asset_id,inspector_id,status,remarks,created_at
           ) VALUES(?,?,?,?, 'Draft','',?)''',
        (
            inspection_no,
            f'Atomic inspection {suffix}',
            asset['id'],
            user['id'],
            now(),
        ),
    )
    item = conn.execute(
        '''INSERT INTO inspection_items(
             inspection_id,item_name,response,reading,remarks
           ) VALUES(?,?,'','','')''',
        (inspection.lastrowid, f'Inspect component {suffix}'),
    )
    return user, int(inspection.lastrowid), int(item.lastrowid), inspection_no


def _race(operation, workers: int = WORKERS):
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=25)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == workers
    return results


def test_concurrent_failed_submission_creates_one_corrective_and_side_effect_set():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, inspection_id, item_id, inspection_no = _seed_inspection(conn, suffix)

        body = InspectionSubmit(
            responses=[
                {
                    'id': item_id,
                    'response': 'Fail',
                    'reading': 'bad',
                    'remarks': 'concurrent failure',
                }
            ],
            remarks='failed inspection',
            create_corrective_on_fail=True,
        )

        def submit() -> dict:
            with db() as conn:
                return submit_inspection_atomic(
                    conn, inspection_id, body, user
                )

        results = _race(submit)
        assert all(result['ok'] is True for result in results)
        assert all(result['result'] == 'Fail' for result in results)
        corrective_ids = {
            int(result['corrective_work_order_id'])
            for result in results
            if result['corrective_work_order_id'] is not None
        }
        assert len(corrective_ids) == 1
        corrective_id = next(iter(corrective_ids))

        with db() as conn:
            inspection = conn.execute(
                '''SELECT status,result,corrective_wo_id,remarks
                   FROM inspections WHERE id=?''',
                (inspection_id,),
            ).fetchone()
            item = conn.execute(
                '''SELECT response,reading,remarks
                   FROM inspection_items WHERE id=?''',
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

        assert inspection['status'] == 'Completed'
        assert inspection['result'] == 'Fail'
        assert int(inspection['corrective_wo_id']) == corrective_id
        assert item['response'] == 'Fail'
        assert item['reading'] == 'bad'
        assert item['remarks'] == 'concurrent failure'
        assert corrective_rows == 1
        assert notifications == 1
        assert audits == 1


def test_completed_inspection_retry_replays_without_mutating_terminal_evidence():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, inspection_id, item_id, inspection_no = _seed_inspection(conn, suffix)

        fail_body = InspectionSubmit(
            responses=[{'id': item_id, 'response': 'Fail', 'reading': '1'}],
            remarks='first completion',
            create_corrective_on_fail=True,
        )
        with db() as conn:
            first = submit_inspection_atomic(
                conn, inspection_id, fail_body, user
            )
        assert first['result'] == 'Fail'
        corrective_id = int(first['corrective_work_order_id'])

        pass_retry = InspectionSubmit(
            responses=[
                {
                    'id': item_id,
                    'response': 'Pass',
                    'reading': '999',
                    'remarks': 'must not overwrite',
                }
            ],
            remarks='must not overwrite terminal inspection',
            create_corrective_on_fail=False,
        )
        with db() as conn:
            replay = submit_inspection_atomic(
                conn, inspection_id, pass_retry, user
            )

        assert replay == {
            'ok': True,
            'result': 'Fail',
            'corrective_work_order_id': corrective_id,
        }
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

        assert inspection['result'] == 'Fail'
        assert inspection['remarks'] == 'first completion'
        assert int(inspection['corrective_wo_id']) == corrective_id
        assert item['response'] == 'Fail'
        assert item['reading'] == '1'
        assert corrective_rows == 1
        assert notifications == 1
        assert audits == 1


def test_concurrent_pass_submission_completes_once_without_corrective():
    with TestClient(app):
        suffix = uuid.uuid4().hex[:10]
        with db() as conn:
            user, inspection_id, item_id, inspection_no = _seed_inspection(conn, suffix)

        body = InspectionSubmit(
            responses=[{'id': item_id, 'response': 'Pass'}],
            remarks='passed inspection',
            create_corrective_on_fail=True,
        )

        def submit() -> dict:
            with db() as conn:
                return submit_inspection_atomic(
                    conn, inspection_id, body, user
                )

        results = _race(submit)
        assert all(
            result == {
                'ok': True,
                'result': 'Pass',
                'corrective_work_order_id': None,
            }
            for result in results
        )
        with db() as conn:
            inspection = conn.execute(
                'SELECT status,result,corrective_wo_id FROM inspections WHERE id=?',
                (inspection_id,),
            ).fetchone()
            audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Inspections' AND action='SUBMIT'
                         AND record_id=?""",
                    (inspection_no,),
                ).fetchone()[0]
            )
        assert inspection['status'] == 'Completed'
        assert inspection['result'] == 'Pass'
        assert inspection['corrective_wo_id'] is None
        assert audits == 1


def test_inspection_submit_capability_matches_historical_role_ceiling_and_route_is_unique():
    assert PERMISSION_CATALOG['inspections.submit'][1] == INSPECTION_SUBMIT_ROLES
    assert ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/inspections/{inspection_id}/submit')
    ] == 'inspections.submit'
    routes = [
        route
        for route in app.router.routes
        if getattr(route, 'path', None) == '/api/inspections/{inspection_id}/submit'
        and 'POST' in set(getattr(route, 'methods', set()) or set())
    ]
    assert len(routes) == 1
