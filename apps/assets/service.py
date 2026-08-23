from __future__ import annotations

from core.database import now
from apps.audit import audit
from apps.events import emit_event
from core.shared import next_no


class AssetNotFound(LookupError):
    pass


class AssetDeleteBlocked(RuntimeError):
    pass


def create_asset(conn, values: dict, actor_id: int) -> dict:
    payload = dict(values)
    asset_no = payload.get('asset_no') or next_no(conn, 'assets', 'asset_no', 'AST-', 1000)
    payload['asset_no'] = asset_no
    columns = list(payload)
    placeholders = ','.join('?' * len(columns))
    stamp = now()
    cur = conn.execute(
        f"INSERT INTO assets({','.join(columns)},created_at,updated_at) VALUES({placeholders},?,?)",
        (*[payload[column] for column in columns], stamp, stamp),
    )
    audit(conn, actor_id, 'CREATE', 'Assets', asset_no, '', payload)
    emit_event(
        conn,
        'asset.created',
        'asset',
        asset_no,
        {'asset_id': cur.lastrowid, 'asset_no': asset_no, 'actor_id': actor_id},
    )
    return {'id': cur.lastrowid, 'asset_no': asset_no}


def update_asset(conn, asset_id: int, changes: dict, actor_id: int) -> dict:
    row = conn.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
    if not row:
        raise AssetNotFound('Asset not found')
    old = dict(row)
    if changes:
        conn.execute(
            'UPDATE assets SET '
            + ','.join(f'{key}=?' for key in changes)
            + ',updated_at=? WHERE id=?',
            (*changes.values(), now(), asset_id),
        )
        audit(conn, actor_id, 'UPDATE', 'Assets', old['asset_no'], old, changes)
    return {'ok': True}


def delete_asset(conn, asset_id: int, actor_id: int) -> dict:
    row = conn.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
    if not row:
        raise AssetNotFound('Asset not found')
    old = dict(row)
    references = int(
        conn.execute('SELECT COUNT(*) FROM work_orders WHERE asset_id=?', (asset_id,)).fetchone()[0]
    ) + int(
        conn.execute('SELECT COUNT(*) FROM assets WHERE parent_asset_id=?', (asset_id,)).fetchone()[0]
    )
    if references:
        raise AssetDeleteBlocked('Asset has linked history or child assets; retire it instead of deleting it')
    conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
    audit(conn, actor_id, 'DELETE', 'Assets', old['asset_no'], old, '')
    return {'ok': True}
