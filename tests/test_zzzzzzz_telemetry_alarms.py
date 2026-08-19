from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_telemetry_threshold_alarm_and_corrective_workflow():
    with TestClient(app) as client:
        admin=auth(client)
        tech=auth(client,'tech1','Tech@2026')
        assets=client.get('/api/assets',headers=admin).json()
        cb=next(a for a in assets if a['asset_no']=='CB-101')

        # Create an independent telemetry point so the test does not depend on seeded alarm state.
        ch=client.post('/api/telemetry/channels',headers=admin,json={
            'channel_code':'TEL-TEST-CB101-CURRENT','asset_id':cb['id'],'name':'CB-101 Phase Current',
            'metric_type':'Current','unit':'A','source_system':'Test SCADA','warning_high':50,'critical_high':75
        })
        assert ch.status_code==200,ch.text
        channel_id=ch.json()['id']

        normal=client.post('/api/telemetry/ingest',headers=admin,json={'readings':[{'channel_code':'TEL-TEST-CB101-CURRENT','value':40,'quality':'Good'}]})
        assert normal.status_code==200 and normal.json()['normal']==1

        warning=client.post('/api/telemetry/ingest',headers=admin,json={'readings':[{'channel_code':'TEL-TEST-CB101-CURRENT','value':60,'quality':'Good'}]})
        assert warning.status_code==200,warning.text
        assert warning.json()['alarms_opened']==1 and warning.json()['results'][0]['severity']=='Warning'
        alarm_id=warning.json()['results'][0]['alarm_id']

        # Technician can acknowledge an operational alarm.
        ack=client.post(f'/api/alarms/{alarm_id}/acknowledge',headers=tech)
        assert ack.status_code==200 and ack.json()['status']=='Acknowledged'

        # Escalating telemetry updates the same active alarm instead of creating duplicates.
        critical=client.post('/api/telemetry/ingest',headers=admin,json={'readings':[{'channel_code':'TEL-TEST-CB101-CURRENT','value':80,'quality':'Good'}]})
        assert critical.status_code==200 and critical.json()['alarms_updated']==1
        current=next(a for a in client.get('/api/alarms',headers=admin).json() if a['id']==alarm_id)
        assert current['severity']=='Critical' and current['status']=='Acknowledged' and current['occurrence_count']>=2

        # Convert the alarm into a linked corrective work order exactly once.
        work=client.post(f'/api/alarms/{alarm_id}/work-order',headers=admin,json={})
        assert work.status_code==200,work.text
        wo_id=work.json()['id'];wo=client.get(f'/api/work-orders/{wo_id}',headers=admin).json()
        assert wo['status']=='Submitted' and wo['priority']=='Critical' and wo['failure_code']=='ALARM-TEL-TEST-CB101-CURRENT'
        again=client.post(f'/api/alarms/{alarm_id}/work-order',headers=admin,json={})
        assert again.status_code==200 and again.json()['existing'] is True and again.json()['id']==wo_id

        # Returning to normal auto-clears the active alarm, retaining it as evidence until closed.
        clear=client.post('/api/telemetry/ingest',headers=admin,json={'readings':[{'channel_code':'TEL-TEST-CB101-CURRENT','value':30,'quality':'Good'}]})
        assert clear.status_code==200 and clear.json()['alarms_cleared']==1
        cleared=next(a for a in client.get('/api/alarms',headers=admin).json() if a['id']==alarm_id)
        assert cleared['status']=='Cleared' and cleared['work_order_id']==wo_id
        closed=client.post(f'/api/alarms/{alarm_id}/close',headers=admin)
        assert closed.status_code==200 and closed.json()['status']=='Closed'

        # Time-series history is queryable and channel deactivation blocks future ingestion.
        readings=client.get('/api/telemetry/readings',headers=admin,params={'channel_id':channel_id,'hours':24}).json()
        assert len(readings)>=4 and readings[0]['channel_code']=='TEL-TEST-CB101-CURRENT'
        assert client.patch(f'/api/telemetry/channels/{channel_id}',headers=admin,json={'active':False}).status_code==200
        denied=client.post('/api/telemetry/ingest',headers=admin,json={'readings':[{'channel_code':'TEL-TEST-CB101-CURRENT','value':90}]})
        assert denied.status_code==404

        intel=client.get('/api/operations/intelligence',headers=admin).json()
        assert intel['telemetry_channels']>=3 and 'critical_alarms' in intel
