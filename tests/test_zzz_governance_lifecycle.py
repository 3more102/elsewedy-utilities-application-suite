import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import app

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_governance_cost_timeline_reports_and_backup_registry():
    with TestClient(app) as client:
        admin=auth(client)
        integrity=client.get('/api/audit/integrity',headers=admin)
        assert integrity.status_code==200,integrity.text
        assert integrity.json()['valid'] is True

        tr=next(x for x in client.get('/api/assets',headers=admin).json() if x['asset_no']=='TR-001')
        ref=client.get('/api/reference',headers=admin).json()
        tech=next(x for x in ref['users'] if x['username']=='tech1')
        item=next(x for x in client.get('/api/inventory',headers=admin).json() if x['current_stock']-x['reserved_stock']>=1)

        before=client.get(f"/api/assets/{tr['id']}",headers=admin).json()['lifetime_maintenance_cost']
        wo=client.post('/api/work-orders',headers=admin,json={
            'title':'Governance cost ledger regression','asset_id':tr['id'],'priority':'Medium','assigned_to':tech['id']
        })
        assert wo.status_code==200,wo.text
        wid=wo.json()['id']
        labor=client.post(f'/api/work-orders/{wid}/labor',headers=admin,json={'hours':1.5,'labor_rate':40,'notes':'governance regression'})
        material=client.post(f'/api/work-orders/{wid}/materials',headers=admin,json={'item_id':item['id'],'quantity':1})
        assert labor.status_code==200,labor.text
        assert material.status_code==200,material.text

        detail=client.get(f"/api/assets/{tr['id']}",headers=admin).json()
        assert detail['lifetime_maintenance_cost']>before
        ledger_types={x['cost_type'] for x in detail['cost_ledger']}
        assert {'Labor','Material'} <= ledger_types

        timeline=client.get(f"/api/assets/{tr['id']}/timeline",headers=admin)
        assert timeline.status_code==200,timeline.text
        event_types={x['type'] for x in timeline.json()['events']}
        assert {'Work Order','Cost','Inspection','Document'} <= event_types

        dossier=client.post(f"/api/assets/{tr['id']}/dossier",headers=admin)
        assert dossier.status_code==200,dossier.text
        report=dossier.json()
        assert report['report_no'].startswith('RPT-') and len(report['content_hash'])==64
        listed=client.get('/api/reports/snapshots',headers=admin,params={'scope_type':'asset','scope_id':'TR-001'}).json()
        snap=next(x for x in listed if x['id']==report['id'])
        assert snap['content_hash']==report['content_hash']
        snapshot=client.get(f"/api/reports/snapshots/{report['id']}",headers=admin).json()
        assert snapshot['snapshot']['asset']['asset_no']=='TR-001'
        verified=client.get(f"/api/reports/snapshots/{report['id']}/verify",headers=admin).json()
        assert verified['valid'] is True and verified['stored_hash']==report['content_hash']
        html=client.get(f"/api/reports/snapshots/{report['id']}/html",headers=admin)
        assert html.status_code==200 and report['content_hash'] in html.text and 'TR-001' in html.text

        policies=client.get('/api/governance/retention',headers=admin)
        assert policies.status_code==200,policies.text
        audit_policy=next(x for x in policies.json() if x['data_class']=='Audit Trail')
        changed=client.patch(f"/api/governance/retention/{audit_policy['id']}",headers=admin,json={'retention_days':2920,'active':True})
        assert changed.status_code==200,changed.text
        refreshed=client.get('/api/governance/retention',headers=admin).json()
        assert next(x for x in refreshed if x['id']==audit_policy['id'])['retention_days']==2920
        preview=client.get('/api/governance/retention/preview',headers=admin)
        assert preview.status_code==200 and all('eligible_records' in x and 'cutoff' in x for x in preview.json())

        cost_export=client.get('/api/exports/cost-ledger.csv',headers=admin)
        assert cost_export.status_code==200 and 'Cost Type,Amount' in cost_export.text

        backup=client.get('/api/admin/backup',headers=admin)
        assert backup.status_code==200,backup.text
        sha=hashlib.sha256(backup.content).hexdigest()
        assert backup.headers['x-euas-backup-sha256']==sha
        history=client.get('/api/admin/backups',headers=admin)
        assert history.status_code==200,history.text
        assert history.json()[0]['sha256']==sha and history.json()[0]['size_bytes']==len(backup.content)

        analytics=client.get('/api/analytics',headers=admin).json()
        assert analytics['maintenance_cost_ledger'] and sum(float(x['amount']) for x in analytics['maintenance_cost_ledger'])>0

        viewer=auth(client,'exec','Viewer@2026')
        assert client.get('/api/audit/integrity',headers=viewer).status_code==200
        assert client.get('/api/governance/retention',headers=viewer).status_code==200
        assert client.patch(f"/api/governance/retention/{audit_policy['id']}",headers=viewer,json={'retention_days':3000}).status_code==403
        assert client.get('/api/admin/backups',headers=viewer).status_code==200

        # Negative path: tampering with a persisted audit row must be detected, then restoring
        # the exact stored value must return the chain to a valid state.
        assert client.get('/api/audit/integrity',headers=admin).json()['valid'] is True
        with sqlite3.connect(TEST_DB) as conn:
            row=conn.execute('SELECT id,new_value FROM audit_logs ORDER BY id DESC LIMIT 1').fetchone()
            audit_id,original=row
            conn.execute('UPDATE audit_logs SET new_value=? WHERE id=?',('tampered-by-regression-test',audit_id))
            conn.commit()
        broken=client.get('/api/audit/integrity',headers=admin).json()
        assert broken['valid'] is False and broken['first_invalid_id']==audit_id
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE audit_logs SET new_value=? WHERE id=?',(original,audit_id))
            conn.commit()
        assert client.get('/api/audit/integrity',headers=admin).json()['valid'] is True
