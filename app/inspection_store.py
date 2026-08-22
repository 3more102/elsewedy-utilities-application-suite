from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException

from . import application as _application
from .audit_store import append_audit
from .auth import require_roles
from .database import db, now
from .inspection_authorization import INSPECTION_SUBMIT_ROLES


class InspectionSubmissionConflict(RuntimeError):
    """Raised when an inspection submission cannot preserve its terminal invariant."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


def _lock_inspection(conn, inspection_id: int) -> None:
    # PostgreSQL obtains a row lock until transaction end; SQLite serializes the
    # writer. The lock covers response updates, optional corrective creation,
    # completion state, notification, and audit as one business transition.
    locked = conn.execute(
        'UPDATE inspections SET status=status WHERE id=?',
        (inspection_id,),
    )
    if not _rowcount_one(locked):
        raise KeyError('Inspection not found')


def _load_inspection(conn, inspection_id: int) -> dict:
    row = conn.execute(
        'SELECT * FROM inspections WHERE id=?',
        (inspection_id,),
    ).fetchone()
    if not row:
        raise KeyError('Inspection not found')
    return dict(row)


def submit_inspection_atomic(conn, inspection_id: int, body, user: dict) -> dict:
    """Complete an inspection once and replay its committed terminal result.

    Historically the route allowed a completed inspection to be submitted again,
    which could create additional corrective work orders. Completion is treated
    as terminal here: after the first commit, retries return the persisted result
    and corrective link without repeating response, notification, work, or audit
    side effects.
    """
    _lock_inspection(conn, inspection_id)
    inspection = _load_inspection(conn, inspection_id)

    if inspection['status'] == 'Completed':
        return {
            'ok': True,
            'result': inspection['result'],
            'corrective_work_order_id': inspection['corrective_wo_id'],
        }

    failed = False
    for response in body.responses:
        value = response.get('response', 'N/A')
        failed = failed or value == 'Fail'
        conn.execute(
            '''UPDATE inspection_items
               SET response=?,reading=?,remarks=?
               WHERE id=? AND inspection_id=?''',
            (
                value,
                response.get('reading', ''),
                response.get('remarks', ''),
                response.get('id'),
                inspection_id,
            ),
        )

    corrective = None
    result = 'Fail' if failed else 'Pass'
    if failed and body.create_corrective_on_fail:
        asset = None
        if inspection['asset_id']:
            asset = conn.execute(
                'SELECT * FROM assets WHERE id=?',
                (inspection['asset_id'],),
            ).fetchone()

        # PR #32 installs a shared work-order number allocator, so this call is
        # serialized against every other WO creator while this transaction holds
        # the inspection row claim.
        number = _application.next_no(
            conn, 'work_orders', 'wo_no', 'WO-', 10026
        )
        stamp = now()
        created = conn.execute(
            '''INSERT INTO work_orders(
                 wo_no,title,description,asset_id,location_id,priority,status,
                 work_type,requested_by,target_start,target_finish,instructions,
                 created_at,updated_at
               ) VALUES(?,?,?,?,?,'High','Submitted','Corrective Maintenance',?,?,?,?,?,?)''',
            (
                number,
                f"Corrective action from {inspection['inspection_no']}",
                (
                    f"Inspection {inspection['inspection_no']} failed. "
                    'Review failed items and correct defects.'
                ),
                inspection['asset_id'],
                asset['location_id'] if asset else None,
                user['id'],
                date.today().isoformat(),
                (date.today() + timedelta(days=2)).isoformat(),
                'Review failed inspection items and implement corrective actions.',
                stamp,
                stamp,
            ),
        )
        corrective = int(created.lastrowid)
        linked = conn.execute(
            '''UPDATE inspections
               SET corrective_wo_id=?
               WHERE id=? AND corrective_wo_id IS NULL''',
            (corrective, inspection_id),
        )
        if not _rowcount_one(linked):
            raise InspectionSubmissionConflict(
                'Inspection corrective-work claim was lost'
            )
        _application.notify(
            conn,
            'Inspection failed',
            f"{inspection['inspection_no']} failed and generated {number}",
            'High',
            None,
            'planner',
            'inspections',
            inspection['inspection_no'],
        )

    completed_at = now()
    completed = conn.execute(
        '''UPDATE inspections
           SET status='Completed',result=?,inspected_at=?,remarks=?
           WHERE id=? AND status<>'Completed' ''',
        (result, completed_at, body.remarks, inspection_id),
    )
    if not _rowcount_one(completed):
        raise InspectionSubmissionConflict('Inspection completion claim was lost')

    append_audit(
        conn,
        user['id'],
        'SUBMIT',
        'Inspections',
        inspection['inspection_no'],
        'Draft',
        result,
    )
    return {
        'ok': True,
        'result': result,
        'corrective_work_order_id': corrective,
    }


def install_inspection_submission_route() -> None:
    app = _application.app
    marker = '_euas_inspection_submission_atomicity'
    if getattr(app.state, marker, False):
        return

    path = '/api/inspections/{inspection_id}/submit'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def submit_inspection_route(
        inspection_id: int,
        body: _application.InspectionSubmit,
        user=Depends(require_roles(*INSPECTION_SUBMIT_ROLES)),
    ):
        try:
            with db() as conn:
                return submit_inspection_atomic(
                    conn, inspection_id, body, user
                )
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except InspectionSubmissionConflict as exc:
            raise HTTPException(409, str(exc))

    _application.submit_inspection = submit_inspection_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
