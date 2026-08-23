from __future__ import annotations

from fastapi import Depends, HTTPException

from . import application as _application
from .auth import require_roles
from .database import db


class OutageTransitionConflict(RuntimeError):
    """Raised when another transaction closed the outage first."""


def _rowcount_one(cursor) -> bool:
    return int(cursor.rowcount or 0) == 1


OUTAGE_CLOSE_ROLES = (
    'admin',
    'asset_manager',
    'maintenance_manager',
    'planner',
    'supervisor',
    'technician',
)


def close_outage_atomic(conn, outage_id: int, body, user: dict) -> dict:
    """Close one outage generation exactly once.

    The terminal claim uses a ``status='Open'`` predicate so concurrent or
    repeated closes cannot duplicate evidence or overwrite the recorded end.
    The asset-status restore runs after the claim inside the same transaction:
    a concurrent outage opening either commits first (the count sees it and
    the asset keeps its unavailable status) or blocks on this transaction's
    asset-row update and reapplies its own constraint afterwards.
    """
    outage = conn.execute(
        'SELECT o.*,a.asset_no FROM asset_outages o JOIN assets a ON a.id=o.asset_id WHERE o.id=?',
        (outage_id,),
    ).fetchone()
    if not outage:
        raise KeyError('Outage not found')
    outage = dict(outage)
    if outage['status'] != 'Open':
        raise OutageTransitionConflict('Outage is already closed')

    end_at = body.end_at or _application.now()
    if _application._dt(end_at) <= _application._dt(outage['start_at']):
        raise HTTPException(400, 'Outage end must be after start')

    impact = body.impact if body.impact is not None else outage['impact']
    claimed = conn.execute(
        '''UPDATE asset_outages
           SET status='Closed',end_at=?,impact=?,updated_at=?
           WHERE id=? AND status='Open' ''',
        (end_at, impact, _application.now(), outage_id),
    )
    if not _rowcount_one(claimed):
        raise OutageTransitionConflict('Outage is already closed')

    other = conn.execute(
        "SELECT COUNT(*) FROM asset_outages WHERE asset_id=? AND status='Open' AND id<>?",
        (outage['asset_id'], outage_id),
    ).fetchone()[0]
    if not other:
        conn.execute(
            "UPDATE assets SET status='Operating',updated_at=? WHERE id=?",
            (_application.now(), outage['asset_id']),
        )

    hours = _application._outage_overlap_hours(
        outage['start_at'],
        end_at,
        _application._dt(outage['start_at']),
        _application._dt(end_at),
    )
    _application.audit(
        conn,
        user['id'],
        'CLOSE OUTAGE',
        'Operations',
        outage['outage_no'],
        'Open',
        {'status': 'Closed', 'duration_hours': round(hours, 2)},
    )
    _application.emit_event(
        conn,
        'asset.outage.closed',
        'asset',
        outage['asset_id'],
        {
            'outage_no': outage['outage_no'],
            'asset_no': outage['asset_no'],
            'end_at': end_at,
            'duration_hours': round(hours, 2),
        },
    )
    return {'ok': True, 'status': 'Closed', 'duration_hours': round(hours, 2)}


def install_outage_routes() -> None:
    """Own the outage transition surface inside the operations domain."""
    app = _application.app
    marker = '_euas_outage_transition'
    if getattr(app.state, marker, False):
        return

    path = '/api/outages/{outage_id}/close'
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and 'POST' in set(getattr(route, 'methods', set()) or set())
        )
    ]

    @app.post(path)
    def close_outage_route(
        outage_id: int,
        body: _application.OutageCloseIn,
        user=Depends(require_roles(*OUTAGE_CLOSE_ROLES)),
    ):
        try:
            with db() as conn:
                return close_outage_atomic(conn, outage_id, body, user)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except OutageTransitionConflict as exc:
            raise HTTPException(409, str(exc))

    _application.close_outage = close_outage_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
