from __future__ import annotations

from threading import Barrier, Thread

from app.auth import hash_password
from apps.inspections import InspectionConflict, InspectionInvalid, create_inspection, submit_inspection
from core.database import db, init_db


def _actor(conn) -> dict:
    row = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    return {'id': int(row['id'])}


def _responses(conn, inspection_id: int, *, fail: bool = True) -> list[dict]:
    items = conn.execute('SELECT id FROM inspection_items WHERE inspection_id=? ORDER BY id', (inspection_id,)).fetchall()
    return [
        {'id': item['id'], 'response': 'Fail' if fail and index == 0 else 'Pass', 'reading': '', 'remarks': ''}
        for index, item in enumerate(items)
    ]


def test_concurrent_failed_submission_creates_one_corrective_work_order():
    init_db(hash_password)
    with db() as conn:
        actor_id = _actor(conn)['id']
        asset_id = conn.execute('SELECT id FROM assets ORDER BY id LIMIT 1').fetchone()['id']
        created = create_inspection(conn, {'template_name': 'Concurrent inspection', 'asset_id': asset_id, 'items': ['A', 'B']}, actor_id)
        responses = _responses(conn, created['id'])
        inspection_no = created['inspection_no']

    barrier = Barrier(2)
    outcomes: list[str] = []
    errors: list[Exception] = []

    def submit_once():
        try:
            barrier.wait()
            with db() as conn:
                submit_inspection(conn, created['id'], {'responses': responses, 'remarks': 'race', 'create_corrective_on_fail': True}, _actor(conn)['id'])
                outcomes.append('ok')
        except InspectionConflict:
            outcomes.append('conflict')
        except Exception as exc:
            errors.append(exc)

    threads = [Thread(target=submit_once), Thread(target=submit_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert sorted(outcomes) == ['conflict', 'ok']
    with db() as conn:
        inspection = conn.execute('SELECT status,result,corrective_wo_id FROM inspections WHERE id=?', (created['id'],)).fetchone()
        assert inspection['status'] == 'Completed' and inspection['result'] == 'Fail' and inspection['corrective_wo_id']
        corrective_count = conn.execute(
            "SELECT COUNT(*) FROM work_orders WHERE title=?",
            (f'Corrective action from {inspection_no}',),
        ).fetchone()[0]
        failed_events = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE event_type='inspection.failed' AND aggregate_id=?",
            (inspection_no,),
        ).fetchone()[0]
        assert corrective_count == 1
        assert failed_events == 1


def test_inspection_submission_requires_all_configured_items():
    init_db(hash_password)
    with db() as conn:
        actor_id = _actor(conn)['id']
        created = create_inspection(conn, {'template_name': 'Required questions', 'items': ['A', 'B']}, actor_id)
        responses = _responses(conn, created['id'], fail=False)[:1]
        try:
            submit_inspection(conn, created['id'], {'responses': responses, 'create_corrective_on_fail': False}, actor_id)
        except InspectionInvalid as exc:
            assert 'cover every configured item' in str(exc)
        else:
            raise AssertionError('partial inspection submission must be rejected')
        assert conn.execute('SELECT status FROM inspections WHERE id=?', (created['id'],)).fetchone()['status'] == 'Draft'
