from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def channel(client, headers, code='TEL-PMP301-VIB'):
    return next(x for x in client.get('/api/telemetry/channels',headers=headers).json() if x['channel_code']==code)


def ingest(client, headers, code, value, ext, quality='Good', key=None):
    return client.post('/api/telemetry/ingest',headers=headers,json={
        'source_system':'CBM Regression','idempotency_key':key or f'batch-{ext}',
        'readings':[{'channel_code':code,'value':value,'quality':quality,'external_id':ext}]
    })


def test_v47_good_quality_consecutive_trigger_auto_work_and_clear():
    with TestClient(app) as client:
        admin=auth(client)
        ch=channel(client,admin)
        created=client.post('/api/cbm/rules',headers=admin,json={
            'name':'V4.7 pump vibration condition rule','channel_id':ch['id'],'operator':'>=','threshold_low':5.5,
            'consecutive_readings':2,'cooldown_minutes':60,'severity':'Warning','action_type':'WorkOrder',
            'work_priority':'High','instructions':'Inspect bearings, alignment and pump vibration source.'
        })
        assert created.status_code==200,created.text
        rule_no=created.json()['rule_no'];rule_id=created.json()['id']

        uncertain=ingest(client,admin,ch['channel_code'],6.2,'v47-uncertain','Uncertain')
        assert uncertain.status_code==200,uncertain.text
        assert uncertain.json()['cbm_events_opened']==0
        assert uncertain.json()['results'][0]['cbm']==[]
        state=next(r for r in client.get(f'/api/cbm/rules?channel_id={ch["id"]}',headers=admin).json() if r['id']==rule_id)
        assert (state.get('consecutive_hits') or 0)==0

        first=ingest(client,admin,ch['channel_code'],5.6,'v47-good-1')
        assert first.status_code==200,first.text
        assert first.json()['cbm_events_opened']==0
        assert first.json()['results'][0]['cbm'][0]['action']=='pending'
        assert first.json()['results'][0]['cbm'][0]['hits']==1

        second=ingest(client,admin,ch['channel_code'],5.9,'v47-good-2',key='v47-trigger-batch')
        assert second.status_code==200,second.text
        payload=second.json()
        assert payload['cbm_events_opened']==1 and payload['cbm_work_orders_created']==1
        cbm=payload['results'][0]['cbm'][0]
        assert cbm['action']=='opened' and cbm['event_no'].startswith('CBM-') and cbm['work_order'].startswith('WO-')

        replay=ingest(client,admin,ch['channel_code'],5.9,'v47-good-2-replay',key='v47-trigger-batch')
        assert replay.status_code==200 and replay.json()['idempotent_replay'] is True
        assert replay.json()['cbm_events_opened']==1 and replay.json()['cbm_work_orders_created']==1

        event=next(e for e in client.get('/api/cbm/events?status=Open',headers=admin).json() if e['rule_no']==rule_no)
        assert event['work_order_id'] and event['wo_no']==cbm['work_order']
        work=client.get(f"/api/work-orders/{event['work_order_id']}",headers=admin).json()
        assert work['status']=='Submitted' and work['work_type']=='Condition-Based Maintenance'
        assert work['failure_code']==f'CBM-{rule_no}'
        approvals=client.get('/api/approvals?status=Pending',headers=admin).json()
        assert any(a['record_type']=='work_order' and a['record_id']==event['work_order_id'] for a in approvals)

        cleared=ingest(client,admin,ch['channel_code'],3.0,'v47-clear')
        assert cleared.status_code==200,cleared.text
        assert cleared.json()['cbm_events_resolved']==1
        resolved=next(e for e in client.get('/api/cbm/events?status=Resolved',headers=admin).json() if e['id']==event['id'])
        assert 'Condition cleared by Good-quality telemetry reading' in resolved['resolution_reason']

        metrics=client.get('/api/metrics',headers=admin).text
        assert 'euas_active_cbm_rules ' in metrics and 'euas_cbm_work_orders_total ' in metrics
        rules_csv=client.get('/api/exports/cbm-rules.csv',headers=admin)
        events_csv=client.get('/api/exports/cbm-events.csv',headers=admin)
        assert rules_csv.status_code==200 and rule_no in rules_csv.text
        assert events_csv.status_code==200 and event['event_no'] in events_csv.text and cbm['work_order'] in events_csv.text


def test_v47_rule_validation_test_mode_permissions_acknowledge_and_cooldown():
    with TestClient(app) as client:
        admin=auth(client)
        tech=auth(client,'tech1','Tech@2026')
        ch=channel(client,admin,'TEL-TR001-LOAD')

        forbidden=client.post('/api/cbm/rules',headers=tech,json={
            'name':'Technician must not author CBM','channel_id':ch['id'],'operator':'>=','threshold_low':80
        })
        assert forbidden.status_code==403
        invalid=client.post('/api/cbm/rules',headers=admin,json={
            'name':'Invalid range must fail','channel_id':ch['id'],'operator':'between','threshold_low':90,'threshold_high':80
        })
        assert invalid.status_code==422

        created=client.post('/api/cbm/rules',headers=admin,json={
            'name':'V4.7 transformer loading recommendation','channel_id':ch['id'],'operator':'outside','threshold_low':20,'threshold_high':85,
            'consecutive_readings':1,'cooldown_minutes':120,'severity':'Critical','action_type':'Recommendation','work_priority':'Critical'
        })
        assert created.status_code==200,created.text
        rid=created.json()['id'];rno=created.json()['rule_no']
        test_hi=client.post(f'/api/cbm/rules/{rid}/test?value=90',headers=tech)
        test_mid=client.post(f'/api/cbm/rules/{rid}/test?value=50',headers=tech)
        assert test_hi.status_code==200 and test_hi.json()['matches'] is True and test_hi.json()['side_effects'] is False
        assert test_mid.status_code==200 and test_mid.json()['matches'] is False
        assert not any(e['rule_no']==rno for e in client.get('/api/cbm/events',headers=admin).json())

        opened=ingest(client,admin,ch['channel_code'],90,'v47-range-open')
        assert opened.status_code==200 and opened.json()['cbm_events_opened']==1
        event=next(e for e in client.get('/api/cbm/events?status=Open',headers=admin).json() if e['rule_no']==rno)
        ack=client.post(f"/api/cbm/events/{event['id']}/acknowledge",headers=tech)
        assert ack.status_code==200 and ack.json()['status']=='Acknowledged'
        manual=client.post(f"/api/cbm/events/{event['id']}/resolve",headers=admin,json={'reason':'Validated condition during regression and closed recommendation.'})
        assert manual.status_code==200

        cooldown=ingest(client,admin,ch['channel_code'],92,'v47-range-cooldown')
        assert cooldown.status_code==200
        result=next(x for x in cooldown.json()['results'][0]['cbm'] if x['rule_no']==rno)
        assert result['action']=='cooldown'
        assert not any(e['rule_no']==rno for e in client.get('/api/cbm/events?status=Open',headers=admin).json())

        disabled=client.patch(f'/api/cbm/rules/{rid}',headers=admin,json={'active':False})
        assert disabled.status_code==200 and disabled.json()['active']==0
        after=ingest(client,admin,ch['channel_code'],95,'v47-range-disabled')
        assert after.status_code==200
        assert all(x['rule_no']!=rno for x in after.json()['results'][0]['cbm'])
