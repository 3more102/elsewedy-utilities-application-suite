from __future__ import annotations

from datetime import datetime

from apps.authorization import user_has_permission
from core.database import DB_BACKEND, now


class DelegationError(ValueError):
    pass


DOMAIN_PERMISSION = {
    'work_order': 'work.transition',
    'purchase_requisition': 'procurement.write',
    'alarm_shelf': 'alarms.operate',
    'rcm_strategy': 'reliability.rcm.approve',
}


def _stamp(value: str | None = None) -> str:
    return value or now()


def _scope_matches(row: dict, approval: dict) -> bool:
    module = row.get('module') or '*'
    record_type = row.get('record_type') or '*'
    resource_id = int(row.get('resource_id') or 0)
    return (
        module in ('*', approval.get('module')) and
        record_type in ('*', approval.get('record_type')) and
        (resource_id == 0 or resource_id == int(approval.get('record_id') or 0))
    )


def authority_valid(conn, user_id: int, approval: dict) -> bool:
    if not user_has_permission(conn, user_id, 'approvals.decide'):
        return False
    required = DOMAIN_PERMISSION.get(approval.get('record_type'))
    return not required or user_has_permission(conn, user_id, required)


def active_delegation(conn, approval: dict, user_id: int, *, lock: bool = False, at: str | None = None) -> dict | None:
    delegator = approval.get('assigned_user_id')
    if not delegator:
        return None
    stamp = _stamp(at)
    sql = '''SELECT * FROM approval_delegations
             WHERE delegator_user_id=? AND delegate_user_id=? AND active=1
               AND revoked_at IS NULL AND start_at<=? AND end_at>=?
             ORDER BY id DESC'''
    if lock and DB_BACKEND == 'postgresql':
        sql += ' FOR UPDATE'
    for raw in conn.execute(sql, (delegator, user_id, stamp, stamp)).fetchall():
        row = dict(raw)
        if not _scope_matches(row, approval):
            continue
        if lock and DB_BACKEND != 'postgresql':
            # Escalate SQLite to a writer transaction before authority is consumed.
            conn.execute('UPDATE approval_delegations SET active=active WHERE id=? AND active=1', (row['id'],))
        if not authority_valid(conn, int(delegator), approval):
            return None
        if not authority_valid(conn, int(user_id), approval):
            return None
        return row
    return None


def create_delegation(
    conn,
    *,
    delegator_user_id: int,
    delegate_user_id: int,
    module: str,
    start_at: str,
    end_at: str,
    created_by: int,
    record_type: str = '*',
    resource_id: int = 0,
    reason: str = '',
) -> dict:
    if delegator_user_id == delegate_user_id:
        raise DelegationError('You cannot delegate approvals to yourself')
    if not user_has_permission(conn, delegator_user_id, 'approvals.decide'):
        raise DelegationError('Delegator lacks approval authority')
    if not user_has_permission(conn, delegate_user_id, 'approvals.decide'):
        raise DelegationError('Delegate lacks approval authority')

    # Delegation is intentionally one hop only: a delegate cannot redelegate authority.
    nested = conn.execute(
        '''SELECT id FROM approval_delegations
           WHERE delegate_user_id=? AND active=1 AND revoked_at IS NULL
             AND start_at<? AND end_at>? LIMIT 1''',
        (delegator_user_id, end_at, start_at),
    ).fetchone()
    if nested:
        raise DelegationError('Nested approval delegation is not allowed')

    duplicate = conn.execute(
        '''SELECT id FROM approval_delegations
           WHERE delegator_user_id=? AND delegate_user_id=? AND module=? AND record_type=? AND resource_id=?
             AND active=1 AND revoked_at IS NULL AND start_at<? AND end_at>? LIMIT 1''',
        (delegator_user_id, delegate_user_id, module or '*', record_type or '*', int(resource_id or 0), end_at, start_at),
    ).fetchone()
    if duplicate:
        raise DelegationError('An overlapping delegation already exists for this scope')

    cur = conn.execute(
        '''INSERT INTO approval_delegations(
           delegator_user_id,delegate_user_id,module,record_type,resource_id,start_at,end_at,active,created_by,created_at,reason
        ) VALUES(?,?,?,?,?,?,?,1,?,?,?)''',
        (
            delegator_user_id, delegate_user_id, module or '*', record_type or '*', int(resource_id or 0),
            start_at, end_at, created_by, now(), reason or '',
        ),
    )
    return dict(conn.execute('SELECT * FROM approval_delegations WHERE id=?', (cur.lastrowid,)).fetchone())


def revoke_delegation(conn, delegation_id: int, actor_user_id: int) -> dict | None:
    row = conn.execute('SELECT * FROM approval_delegations WHERE id=?', (delegation_id,)).fetchone()
    if not row:
        return None
    row = dict(row)
    if not row.get('active') or row.get('revoked_at'):
        return row
    stamp = now()
    conn.execute(
        '''UPDATE approval_delegations
           SET active=0,revoked_at=?,revoked_by=?
           WHERE id=? AND active=1 AND revoked_at IS NULL''',
        (stamp, actor_user_id, delegation_id),
    )
    row.update(active=0, revoked_at=stamp, revoked_by=actor_user_id)
    return row
