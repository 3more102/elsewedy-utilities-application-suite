import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import app

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_sla_escalation_and_durable_outbox():
    with TestClient(app) as client:
        admin=auth(client)
        ref=client.get('/api/reference',headers=admin).json()
        tech=next(x for x in ref['users'] if x['username']=='tech1')
        supervisor=next(x for x in ref['users'] if x['username']=='supervisor')
        tr=next(x for x in client.get('/api/assets',headers=admin,params={'q':'TR-001'}).json() if x['asset_no']=='TR-001')

        policies=client.get('/api/sla/policies',headers=admin)
        assert policies.status_code==200,policies.text
        emergency=next(x for x in policies.json() if x['priority']=='Emergency')
        changed=client.patch(f"/api/sla/policies/{emergency['id']}",headers=admin,json={'response_minutes':20,'resolution_minutes':300})
        assert changed.status_code==200,changed.text
        assert changed.json()['response_minutes']==20 and changed.json()['resolution_minutes']==300

        created=client.post('/api/work-orders',headers=admin,json={
            'title':'SLA escalation regression','asset_id':tr['id'],'priority':'Emergency',
            'assigned_to':tech['id'],'supervisor_id':supervisor['id'],'estimated_hours':2
        })
        assert created.status_code==200,created.text
        wid=created.json()['id']; wo_no=created.json()['wo_no']

        work=client.get(f'/api/work-orders/{wid}',headers=admin).json()
        assert work['sla_response_status']=='Pending' and work['sla_resolution_status']=='Pending'
        assert work['sla_response_due'] and work['sla_resolution_due']

        # Move both deadlines into the past to deterministically exercise the breach engine.
        past=(datetime.now()-timedelta(hours=2)).isoformat(timespec='seconds')
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE work_order_sla SET response_due=?,resolution_due=? WHERE work_order_id=?',(past,past,wid))
            conn.commit()

        run=client.post('/api/automation/run',headers=admin)
        assert run.status_code==200,run.text
        summary=run.json()['summary']
        assert summary['sla_response_breaches']>=1
        assert summary['sla_resolution_breaches']>=1
        assert summary['outbox_skipped']>=1  # default test environment has no outbound webhook

        updated=client.get(f'/api/work-orders/{wid}',headers=admin).json()
        assert updated['sla_response_status']=='Breached'
        assert updated['sla_resolution_status']=='Breached'
        assert updated['sla_escalated_level']>=1

        sla=client.get('/api/sla/summary',headers=admin)
        assert sla.status_code==200
        assert sla.json()['active_breaches']>=1
        events=client.get('/api/sla/events',headers=admin).json()
        related=[x for x in events if x['wo_no']==wo_no]
        assert {x['event_type'] for x in related} >= {'Response Breach','Resolution Breach'}

        notes=client.get('/api/notifications',headers=auth(client,'tech1','Tech@2026')).json()
        assert any(x['title']=='SLA response breach' and wo_no in x['message'] for x in notes)

        outbox=client.get('/api/events/outbox',headers=admin).json()
        related_events=[x for x in outbox if x['aggregate_id']==wo_no]
        assert related_events and any(x['event_type'].startswith('workflow.work_management') for x in related_events)
        assert any(x['event_type'].startswith('sla.') for x in related_events)
        # Additive per-row delivery budget annotation for operator surfaces.
        from app import application as _application
        assert all(x['max_attempts']==_application.OUTBOX_MAX_ATTEMPTS for x in outbox)
        retry_target=related_events[0]
        retry=client.post(f"/api/events/outbox/{retry_target['id']}/retry",headers=admin)
        assert retry.status_code==200
        assert next(x for x in client.get('/api/events/outbox',headers=admin).json() if x['id']==retry_target['id'])['status']=='Pending'

        run2=client.post('/api/automation/run',headers=admin)
        assert run2.status_code==200
        assert run2.json()['summary']['sla_response_breaches']==0
        assert run2.json()['summary']['sla_resolution_breaches']==0

        metrics=client.get('/api/metrics',headers=admin)
        assert metrics.status_code==200
        assert 'euas_sla_breaches_total ' in metrics.text
        assert 'euas_outbox_pending ' in metrics.text
        assert 'euas_outbox_attempt_exhausted ' in metrics.text

        status=client.get('/api/automation/status',headers=admin)
        assert status.status_code==200
        assert 'outbox_exhausted' in status.json()['queue']

        export=client.get('/api/exports/sla.csv',headers=admin)
        assert export.status_code==200 and 'text/csv' in export.headers.get('content-type','')
        assert 'Response Due,Response Status,Resolution Due,Resolution Status' in export.text
        assert wo_no in export.text

        viewer=auth(client,'exec','Viewer@2026')
        assert client.get('/api/sla/summary',headers=viewer).status_code==200
        assert client.post(f"/api/events/outbox/{retry_target['id']}/retry",headers=viewer).status_code==403

        # Restore the seeded policy so later manual demos preserve the intended defaults.
        restored=client.patch(f"/api/sla/policies/{emergency['id']}",headers=admin,json={'response_minutes':15,'resolution_minutes':240})
        assert restored.status_code==200


def test_signed_webhook_delivery_contract(monkeypatch):
    import hashlib, hmac, json
    import app.main as mainmod

    captured=[]
    class FakeResponse:
        status=202
        def __enter__(self): return self
        def __exit__(self,*args): return False

    def fake_urlopen(req,timeout=0):
        captured.append(req)
        return FakeResponse()

    monkeypatch.setattr(mainmod,'EVENT_WEBHOOK_URL','https://integration.example.test/euas/events')
    monkeypatch.setattr(mainmod,'EVENT_WEBHOOK_SECRET','qa-webhook-secret')
    monkeypatch.setattr(mainmod.urllib_request,'urlopen',fake_urlopen)

    with TestClient(app) as client:
        admin=auth(client)
        tr=next(x for x in client.get('/api/assets',headers=admin,params={'q':'TR-001'}).json() if x['asset_no']=='TR-001')
        created=client.post('/api/work-orders',headers=admin,json={'title':'Webhook delivery regression','asset_id':tr['id'],'priority':'Low'})
        assert created.status_code==200
        wo_no=created.json()['wo_no']

        run=client.post('/api/automation/run',headers=admin)
        assert run.status_code==200,run.text
        assert run.json()['summary']['outbox_delivered']>=1
        assert captured

        matching=[]
        for req in captured:
            body=json.loads(req.data.decode())
            if body.get('aggregate_id')==wo_no:
                matching.append((req,body))
        assert matching
        req,body=matching[0]
        assert req.full_url=='https://integration.example.test/euas/events'
        assert body['event_type'].startswith('workflow.work_management')
        expected='sha256='+hmac.new(b'qa-webhook-secret',req.data,hashlib.sha256).hexdigest()
        headers={k.lower():v for k,v in req.header_items()}
        assert headers['x-euas-signature']==expected
        assert headers['x-euas-event-id']==body['event_no']

        outbox=client.get('/api/events/outbox',headers=admin).json()
        event=next(x for x in outbox if x['aggregate_id']==wo_no)
        assert event['status']=='Delivered' and event['attempts']>=1
