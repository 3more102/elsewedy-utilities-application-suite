from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Optional

from fastapi import Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import application as _application
from .auth import current_user, require_roles
from .database import db


def _rows(cur):
    return _application.rows(cur)


def _get_or_404(conn, sql, args, message):
    return _application.get_or_404(conn, sql, args, message)


def list_assets_view(conn, q: str, condition: str, status: str, site_id, sort: str, limit: int, offset: int):
    allowed = {
        'asset_no': 'a.asset_no',
        'name': 'a.name',
        'condition': 'a.condition',
        'criticality': 'a.criticality',
        'current_value': 'a.current_value',
    }
    order = allowed.get(sort, 'a.asset_no')
    sql = _application.ASSET_SELECT + ' WHERE 1=1'
    args: list[object] = []
    if q:
        sql += ' AND (a.asset_no LIKE ? OR a.name LIKE ? OR a.serial_no LIKE ? OR l.name LIKE ?)'
        like = f'%{q}%'
        args += [like] * 4
    if condition:
        sql += ' AND a.condition=?'
        args.append(condition)
    if status:
        sql += ' AND a.status=?'
        args.append(status)
    if site_id:
        sql += ' AND s.id=?'
        args.append(site_id)
    sql += f' ORDER BY {order}, a.id LIMIT ? OFFSET ?'
    args += [limit, offset]
    return _rows(conn.execute(sql, args))


def get_asset_view(conn, asset_id: int) -> dict:
    a = _get_or_404(
        conn, _application.ASSET_SELECT + ' WHERE a.id=?', (asset_id,), 'Asset not found'
    )
    a['children'] = _rows(conn.execute(
        'SELECT id,asset_no,name,condition,status FROM assets WHERE parent_asset_id=? ORDER BY asset_no',
        (asset_id,),
    ))
    a['meters'] = _rows(conn.execute('SELECT * FROM meters WHERE asset_id=?', (asset_id,)))
    a['work_history'] = _rows(conn.execute(
        'SELECT id,wo_no,title,status,priority,work_type,actual_finish,actual_cost FROM work_orders WHERE asset_id=? ORDER BY id DESC',
        (asset_id,),
    ))
    a['inspections'] = _rows(conn.execute(
        'SELECT id,inspection_no,template_name,status,result,inspected_at FROM inspections WHERE asset_id=? ORDER BY id DESC',
        (asset_id,),
    ))
    a['documents'] = _rows(conn.execute(
        'SELECT id,document_no,title,category,file_name,uploaded_at FROM documents WHERE asset_id=? ORDER BY id DESC',
        (asset_id,),
    ))
    a['outages'] = _rows(conn.execute(
        'SELECT * FROM asset_outages WHERE asset_id=? ORDER BY start_at DESC', (asset_id,)
    ))
    a['telemetry_channels'] = _rows(conn.execute(
        'SELECT * FROM telemetry_channels WHERE asset_id=? ORDER BY channel_code', (asset_id,)
    ))
    a['operational_alarms'] = _rows(conn.execute(
        """SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit
           FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id
           WHERE oa.asset_id=? ORDER BY oa.id DESC""",
        (asset_id,),
    ))
    a['cost_ledger'] = _rows(conn.execute(
        '''SELECT c.*,w.wo_no,u.full_name posted_by_name
           FROM maintenance_cost_ledger c LEFT JOIN work_orders w ON w.id=c.work_order_id
           LEFT JOIN users u ON u.id=c.posted_by WHERE c.asset_id=? ORDER BY c.id DESC''',
        (asset_id,),
    ))
    a['cost_summary'] = _rows(conn.execute(
        'SELECT cost_type,COUNT(*) entries,COALESCE(SUM(amount),0) amount FROM maintenance_cost_ledger WHERE asset_id=? GROUP BY cost_type ORDER BY amount DESC',
        (asset_id,),
    ))
    a['lifetime_maintenance_cost'] = sum(float(x['amount']) for x in a['cost_summary'])
    return a


def asset_timeline_view(conn, asset_id: int) -> dict:
    a = _get_or_404(
        conn,
        'SELECT id,asset_no,name FROM assets WHERE id=?',
        (asset_id,),
        'Asset not found',
    )
    events = []
    for w in _rows(conn.execute(
        'SELECT id,wo_no,title,status,priority,created_at,actual_finish,actual_cost FROM work_orders WHERE asset_id=?',
        (asset_id,),
    )):
        events.append({'at': w['created_at'], 'type': 'Work Order', 'code': w['wo_no'], 'title': w['title'], 'detail': f"Created · {w['priority']} · {w['status']}", 'module': 'work', 'id': w['id']})
        if w['actual_finish']:
            events.append({'at': w['actual_finish'], 'type': 'Maintenance', 'code': w['wo_no'], 'title': w['title'], 'detail': f"Completed · cost {float(w['actual_cost']):.2f}", 'module': 'work', 'id': w['id']})
    for i in _rows(conn.execute(
        'SELECT id,inspection_no,template_name,status,result,created_at,inspected_at FROM inspections WHERE asset_id=?',
        (asset_id,),
    )):
        events.append({'at': i['inspected_at'] or i['created_at'], 'type': 'Inspection', 'code': i['inspection_no'], 'title': i['template_name'], 'detail': f"{i['result'] or i['status']}", 'module': 'inspections', 'id': i['id']})
    for m in _rows(conn.execute(
        '''SELECT mr.id,m.meter_code,m.unit,mr.reading,mr.reading_at,u.full_name
           FROM meter_readings mr JOIN meters m ON m.id=mr.meter_id JOIN users u ON u.id=mr.entered_by
           WHERE m.asset_id=?''',
        (asset_id,),
    )):
        events.append({'at': m['reading_at'], 'type': 'Meter', 'code': m['meter_code'], 'title': f"Meter reading {m['reading']} {m['unit']}", 'detail': m['full_name'], 'module': 'assets', 'id': asset_id})
    for d in _rows(conn.execute(
        'SELECT id,document_no,title,category,uploaded_at FROM documents WHERE asset_id=?',
        (asset_id,),
    )):
        events.append({'at': d['uploaded_at'], 'type': 'Document', 'code': d['document_no'], 'title': d['title'], 'detail': d['category'], 'module': 'documents', 'id': d['id']})
    for c in _rows(conn.execute(
        'SELECT id,entry_no,cost_type,amount,reference,posted_at FROM maintenance_cost_ledger WHERE asset_id=?',
        (asset_id,),
    )):
        events.append({'at': c['posted_at'], 'type': 'Cost', 'code': c['entry_no'], 'title': c['cost_type'], 'detail': f"{float(c['amount']):.2f} · {c['reference']}", 'module': 'analytics', 'id': c['id']})
    for aev in _rows(conn.execute(
        '''SELECT oa.id,oa.alarm_no,oa.severity,oa.status,oa.message,oa.opened_at,tc.channel_code
           FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id
           WHERE oa.asset_id=?''',
        (asset_id,),
    )):
        events.append({'at': aev['opened_at'], 'type': 'Operational Alarm', 'code': aev['alarm_no'], 'title': aev['message'], 'detail': f"{aev['severity']} · {aev['status']} · {aev['channel_code']}", 'module': 'telemetry', 'id': aev['id']})
    events.sort(key=lambda x: x['at'] or '', reverse=True)
    return {'asset': a, 'events': events}


def build_asset_dossier(conn, asset_id: int) -> dict:
    a = _get_or_404(
        conn, _application.ASSET_SELECT + ' WHERE a.id=?', (asset_id,), 'Asset not found'
    )
    return {
        'asset': a,
        'children': _rows(conn.execute('SELECT asset_no,name,condition,status FROM assets WHERE parent_asset_id=? ORDER BY asset_no', (asset_id,))),
        'work_orders': _rows(conn.execute('SELECT wo_no,title,status,priority,work_type,actual_start,actual_finish,actual_hours,actual_cost FROM work_orders WHERE asset_id=? ORDER BY id DESC', (asset_id,))),
        'inspections': _rows(conn.execute('SELECT inspection_no,template_name,status,result,inspected_at,remarks FROM inspections WHERE asset_id=? ORDER BY id DESC', (asset_id,))),
        'documents': _rows(conn.execute('SELECT document_no,title,category,file_name,uploaded_at FROM documents WHERE asset_id=? ORDER BY id DESC', (asset_id,))),
        'costs': _rows(conn.execute('SELECT entry_no,cost_type,amount,quantity,reference,posted_at FROM maintenance_cost_ledger WHERE asset_id=? ORDER BY id DESC', (asset_id,))),
        'meter_readings': _rows(conn.execute('''SELECT m.meter_code,m.meter_type,m.unit,mr.reading,mr.reading_at FROM meter_readings mr JOIN meters m ON m.id=mr.meter_id WHERE m.asset_id=? ORDER BY mr.id DESC''', (asset_id,))),
    }


def generate_dossier(conn, asset_id: int, user: dict) -> dict:
    payload = build_asset_dossier(conn, asset_id)
    a = payload['asset']
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    no = _application.next_no(conn, 'report_snapshots', 'report_no', 'RPT-', 10001)
    cur = conn.execute(
        '''INSERT INTO report_snapshots(
             report_no,report_type,scope_type,scope_id,title,snapshot_json,
             content_hash,generated_by,generated_at
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            no,
            'Asset Dossier',
            'asset',
            a['asset_no'],
            f"{a['asset_no']} — {a['name']} Asset Dossier",
            serialized,
            digest,
            user['id'],
            _application.now(),
        ),
    )
    _application.audit(
        conn,
        user['id'],
        'GENERATE REPORT',
        'Reports',
        no,
        '',
        {'scope': a['asset_no'], 'sha256': digest},
    )
    return {
        'id': cur.lastrowid,
        'report_no': no,
        'title': f"{a['asset_no']} — {a['name']} Asset Dossier",
        'content_hash': digest,
    }


def create_asset_record(conn, body, user: dict) -> dict:
    asset_no = body.asset_no or _application.next_no(conn, 'assets', 'asset_no', 'AST-', 1000)
    vals = body.model_dump()
    vals['asset_no'] = asset_no
    cols = list(vals)
    qs = ','.join('?' * len(cols))
    cur = conn.execute(
        f"INSERT INTO assets({','.join(cols)},created_at,updated_at) VALUES({qs},?,?)",
        (*[vals[c] for c in cols], _application.now(), _application.now()),
    )
    _application.audit(conn, user['id'], 'CREATE', 'Assets', asset_no, '', vals)
    return {'id': cur.lastrowid, 'asset_no': asset_no}


def update_asset_record(conn, asset_id: int, body, user: dict) -> dict:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    old = _get_or_404(
        conn, 'SELECT * FROM assets WHERE id=?', (asset_id,), 'Asset not found'
    )
    if changes:
        conn.execute(
            'UPDATE assets SET '
            + ','.join(f'{k}=?' for k in changes)
            + ',updated_at=? WHERE id=?',
            (*changes.values(), _application.now(), asset_id),
        )
        _application.audit(
            conn, user['id'], 'UPDATE', 'Assets', old['asset_no'], old, changes
        )
    return {'ok': True}


def delete_asset_record(conn, asset_id: int, user: dict) -> dict:
    old = _get_or_404(
        conn, 'SELECT * FROM assets WHERE id=?', (asset_id,), 'Asset not found'
    )
    refs = conn.execute(
        'SELECT COUNT(*) FROM work_orders WHERE asset_id=?', (asset_id,)
    ).fetchone()[0] + conn.execute(
        'SELECT COUNT(*) FROM assets WHERE parent_asset_id=?', (asset_id,)
    ).fetchone()[0]
    if refs:
        raise HTTPException(
            409, 'Asset has linked history or child assets; retire it instead of deleting it'
        )
    conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
    _application.audit(conn, user['id'], 'DELETE', 'Assets', old['asset_no'], old, '')
    return {'ok': True}


def export_assets_csv(conn) -> StreamingResponse:
    data = _rows(conn.execute(_application.ASSET_SELECT + ' ORDER BY a.asset_no'))
    out = io.StringIO()
    fields = [
        'asset_no', 'name', 'category', 'manufacturer', 'model', 'serial_no',
        'criticality', 'condition', 'status', 'site_name', 'location_name',
        'department', 'responsible_person', 'current_value', 'next_maintenance',
    ]
    w = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(data)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename=EUAS_assets.csv'},
    )


def install_asset_routes() -> None:
    """Own the asset registry API surface inside the asset domain."""
    app = _application.app
    marker = '_euas_asset_routes'
    if getattr(app.state, marker, False):
        return

    removals = [
        ('/api/assets', {'GET'}),
        ('/api/assets', {'POST'}),
        ('/api/assets/{asset_id}', {'GET'}),
        ('/api/assets/{asset_id}', {'PATCH'}),
        ('/api/assets/{asset_id}', {'DELETE'}),
        ('/api/assets/{asset_id}/timeline', {'GET'}),
        ('/api/assets/{asset_id}/dossier', {'POST'}),
        ('/api/assets-export.csv', {'GET'}),
    ]
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not any(
            getattr(route, 'path', None) == path
            and methods.intersection(set(getattr(route, 'methods', set()) or set()))
            for path, methods in removals
        )
    ]

    @app.get('/api/assets')
    def list_assets_route(
        q: str = '',
        condition: str = '',
        status: str = '',
        site_id: Optional[int] = None,
        sort: str = 'asset_no',
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        user=Depends(current_user),
    ):
        with db() as conn:
            return list_assets_view(conn, q, condition, status, site_id, sort, limit, offset)

    @app.get('/api/assets-export.csv')
    def export_assets_route(user=Depends(current_user)):
        with db() as conn:
            return export_assets_csv(conn)

    @app.post('/api/assets')
    def create_asset_route(
        body: _application.AssetIn,
        user=Depends(require_roles(*_application.WRITE_ROLES)),
    ):
        with db() as conn:
            return create_asset_record(conn, body, user)

    @app.get('/api/assets/{asset_id}')
    def get_asset_route(asset_id: int, user=Depends(current_user)):
        with db() as conn:
            return get_asset_view(conn, asset_id)

    @app.patch('/api/assets/{asset_id}')
    def update_asset_route(
        asset_id: int,
        body: _application.AssetPatch,
        user=Depends(require_roles(*_application.WRITE_ROLES)),
    ):
        with db() as conn:
            return update_asset_record(conn, asset_id, body, user)

    @app.delete('/api/assets/{asset_id}')
    def delete_asset_route(
        asset_id: int,
        user=Depends(require_roles('admin', 'asset_manager')),
    ):
        with db() as conn:
            return delete_asset_record(conn, asset_id, user)

    @app.get('/api/assets/{asset_id}/timeline')
    def asset_timeline_route(asset_id: int, user=Depends(current_user)):
        with db() as conn:
            return asset_timeline_view(conn, asset_id)

    @app.post('/api/assets/{asset_id}/dossier')
    def generate_dossier_route(asset_id: int, user=Depends(current_user)):
        with db() as conn:
            return generate_dossier(conn, asset_id, user)

    _application.list_assets = list_assets_route
    _application.get_asset = get_asset_route
    _application.asset_timeline = asset_timeline_route
    _application.generate_asset_dossier = generate_dossier_route
    _application.create_asset = create_asset_route
    _application.update_asset = update_asset_route
    _application.delete_asset = delete_asset_route
    _application.export_assets = export_assets_route
    app.openapi_schema = None
    setattr(app.state, marker, True)
