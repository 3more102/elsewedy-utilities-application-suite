from datetime import date, timedelta
from fastapi.testclient import TestClient

from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_automation_observability_exports_and_backup():
    with TestClient(app) as client:
        admin=auth(client)
        ref=client.get('/api/reference',headers=admin).json()
        tech=next(x for x in ref['users'] if x['username']=='tech1')
        tr=next(x for x in client.get('/api/assets',headers=admin).json() if x['asset_no']=='TR-001')

        # Create a clearly overdue assigned work order so the automation engine has an alertable record.
        created=client.post('/api/work-orders',headers=admin,json={
            'title':'Automation overdue regression','asset_id':tr['id'],'assigned_to':tech['id'],
            'priority':'High','target_finish':(date.today()-timedelta(days=3)).isoformat()
        })
        assert created.status_code==200,created.text

        before=client.get('/api/automation/status',headers=admin)
        assert before.status_code==200 and 'queue' in before.json()

        run=client.post('/api/automation/run',headers=admin)
        assert run.status_code==200,run.text
        payload=run.json()
        assert payload['status']=='Succeeded'
        assert payload['run_no'].startswith('JOB-')
        assert payload['summary']['overdue_alerts']>=1

        runs=client.get('/api/automation/runs',headers=admin)
        assert runs.status_code==200
        assert any(x['run_no']==payload['run_no'] and x['status']=='Succeeded' for x in runs.json())

        # A second run should not duplicate unread alerts for the same work order/recipient.
        run2=client.post('/api/automation/run',headers=admin)
        assert run2.status_code==200
        assert run2.json()['summary']['overdue_alerts']==0

        # Notification bulk-read endpoint is operational.
        tech_headers=auth(client,'tech1','Tech@2026')
        notes=client.get('/api/notifications',headers=tech_headers).json()
        assert any(x['title']=='Overdue work order' for x in notes)
        bulk=client.post('/api/notifications/read-all',headers=tech_headers)
        assert bulk.status_code==200
        assert all(x['is_read'] for x in client.get('/api/notifications',headers=tech_headers).json())

        # Protected observability endpoint emits Prometheus-compatible text.
        metrics=client.get('/api/metrics',headers=admin)
        assert metrics.status_code==200
        assert 'euas_requests_total ' in metrics.text
        assert 'euas_automation_runs_succeeded ' in metrics.text

        # CSV exports are real downloadable datasets.
        for path,needle in [
            ('/api/exports/work-orders.csv','Work Order,Title'),
            ('/api/exports/inventory.csv','Item,Name,Category'),
            ('/api/exports/procurement.csv','PR,Title,Requester'),
            ('/api/exports/audit.csv','Time,User,Action'),
        ]:
            r=client.get(path,headers=admin)
            assert r.status_code==200,path
            assert 'text/csv' in r.headers.get('content-type','')
            assert needle in r.text

        # SQLite backup is transactionally snapshotted into a ZIP bundle.
        backup=client.get('/api/admin/backup',headers=admin)
        assert backup.status_code==200,backup.text
        assert backup.content[:2]==b'PK'
        assert 'application/zip' in backup.headers.get('content-type','')

        # Audit filters narrow the immutable trace by module/search text.
        filtered=client.get('/api/audit',headers=admin,params={'module':'Automation','q':payload['run_no']})
        assert filtered.status_code==200
        assert any(x['record_id']==payload['run_no'] for x in filtered.json())
