from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_v48_failure_mode_hierarchy_fmea_review_and_governed_work():
    with TestClient(app) as client:
        admin=auth(client);tech=auth(client,'tech1','Tech@2026')
        asset=client.get('/api/assets',headers=admin).json()[0]
        forbidden=client.post('/api/reliability/failure-modes',headers=tech,json={'name':'Technician should not manage FMEA','category':'Regression'})
        assert forbidden.status_code==403

        parent=client.post('/api/reliability/failure-modes',headers=admin,json={
            'name':'V4.8 Mechanical Degradation','category':'Mechanical','description':'Regression parent failure-mode family.'
        })
        assert parent.status_code==200,parent.text
        child=client.post('/api/reliability/failure-modes',headers=admin,json={
            'name':'V4.8 Bearing Degradation','category':'Mechanical','parent_id':parent.json()['id'],'description':'Bearing degradation child mode.'
        })
        assert child.status_code==200,child.text
        cycle=client.patch(f"/api/reliability/failure-modes/{parent.json()['id']}",headers=admin,json={'parent_id':child.json()['id']})
        assert cycle.status_code==409

        created=client.post('/api/reliability/fmea',headers=admin,json={
            'asset_id':asset['id'],'failure_mode_id':child.json()['id'],'function_description':'Maintain reliable rotating support.',
            'failure_effect':'Loss of rotating stability and possible forced outage.','failure_cause':'Progressive bearing wear or lubrication degradation.',
            'current_controls':'Routine vibration inspection.','recommended_action':'Inspect lubrication and bearing condition; plan replacement if degraded.',
            'severity':9,'occurrence':7,'detectability':5,'status':'Active','review_due_date':'2099-01-01'
        })
        assert created.status_code==200,created.text
        f=created.json();assert f['rpn']==315 and f['risk_band']=='Critical'
        listed=client.get(f"/api/reliability/fmea?asset_id={asset['id']}",headers=admin).json()
        rec=next(x for x in listed if x['id']==f['id'])
        assert rec['parent_mode_no']==parent.json()['mode_no'] and rec['mode_no']==child.json()['mode_no']

        reviewed=client.post(f"/api/reliability/fmea/{f['id']}/review",headers=admin,json={
            'severity':8,'occurrence':4,'detectability':3,'notes':'Controls verified and occurrence reduced after maintenance review.','status':'Active','review_due_date':'2099-06-01'
        })
        assert reviewed.status_code==200,reviewed.text
        assert reviewed.json()['rpn']==96 and reviewed.json()['risk_band']=='Medium'
        history=client.get(f"/api/reliability/fmea/{f['id']}/reviews",headers=admin).json()
        assert history and history[0]['old_rpn']==315 and history[0]['new_rpn']==96

        work=client.post(f"/api/reliability/fmea/{f['id']}/work-order",headers=admin,json={'priority':'High','notes':'Convert reviewed reliability action to governed maintenance.'})
        assert work.status_code==200,work.text
        wo=client.get(f"/api/work-orders/{work.json()['id']}",headers=admin).json()
        assert wo['status']=='Submitted' and wo['work_type']=='Reliability / FMEA'
        assert wo['asset_fmea_id']==f['id'] and wo['failure_code']==child.json()['mode_no']
        approvals=client.get('/api/approvals?status=Pending',headers=admin).json()
        assert any(a['record_type']=='work_order' and a['record_id']==wo['id'] for a in approvals)
        duplicate=client.post(f"/api/reliability/fmea/{f['id']}/work-order",headers=admin,json={'priority':'High'})
        assert duplicate.status_code==409

        summary=client.get('/api/reliability/summary',headers=admin).json()
        assert summary['active_records']>=1 and isinstance(summary['risk_bands'],dict)


def test_v48_cbm_fmea_linkage_exports_and_metrics():
    with TestClient(app) as client:
        admin=auth(client)
        channel=next(x for x in client.get('/api/telemetry/channels',headers=admin).json() if x['channel_code']=='TEL-PMP301-VIB')
        mode=client.post('/api/reliability/failure-modes',headers=admin,json={
            'name':'V4.8 CBM-linked bearing vibration','category':'Condition Monitoring','description':'Regression failure mode for CBM traceability.'
        })
        assert mode.status_code==200,mode.text
        fmea=client.post('/api/reliability/fmea',headers=admin,json={
            'asset_id':channel['asset_id'],'failure_mode_id':mode.json()['id'],'function_description':'Maintain acceptable pump vibration.',
            'failure_effect':'Excessive vibration can damage rotating components.','failure_cause':'Bearing wear or misalignment.',
            'current_controls':'Online vibration telemetry.','recommended_action':'Inspect alignment, lubrication and bearings.',
            'severity':8,'occurrence':5,'detectability':4,'status':'Active'
        })
        assert fmea.status_code==200,fmea.text
        f=fmea.json()
        rule=client.post('/api/cbm/rules',headers=admin,json={
            'name':'V4.8 FMEA-linked CBM rule','channel_id':channel['id'],'operator':'>=','threshold_low':7.7,
            'consecutive_readings':1,'cooldown_minutes':0,'severity':'Critical','action_type':'WorkOrder','work_priority':'Critical',
            'instructions':'Execute the linked FMEA reliability action.','asset_fmea_id':f['id']
        })
        assert rule.status_code==200,rule.text
        rule_row=next(x for x in client.get(f"/api/cbm/rules?channel_id={channel['id']}",headers=admin).json() if x['id']==rule.json()['id'])
        assert rule_row['fmea_no']==f['fmea_no'] and rule_row['failure_mode_no']==mode.json()['mode_no']

        triggered=client.post('/api/telemetry/ingest',headers=admin,json={
            'source_system':'V4.8 FMEA Regression','idempotency_key':'v48-fmea-cbm-batch',
            'readings':[{'channel_code':channel['channel_code'],'value':8.1,'quality':'Good','external_id':'v48-fmea-cbm-reading'}]
        })
        assert triggered.status_code==200,triggered.text
        assert triggered.json()['cbm_events_opened']>=1 and triggered.json()['cbm_work_orders_created']>=1
        event=next(e for e in client.get('/api/cbm/events',headers=admin).json() if e['rule_no']==rule.json()['rule_no'])
        assert event['asset_fmea_id']==f['id'] and event['fmea_no']==f['fmea_no']
        wo=client.get(f"/api/work-orders/{event['work_order_id']}",headers=admin).json()
        assert wo['asset_fmea_id']==f['id'] and wo['failure_code']==mode.json()['mode_no']
        assert f['fmea_no'] in wo['description']

        other_asset=next(a for a in client.get('/api/assets',headers=admin).json() if a['id']!=channel['asset_id'])
        other_mode=client.post('/api/reliability/failure-modes',headers=admin,json={'name':'V4.8 other-asset mode','category':'Regression'}).json()
        other_fmea=client.post('/api/reliability/fmea',headers=admin,json={
            'asset_id':other_asset['id'],'failure_mode_id':other_mode['id'],'failure_effect':'Other asset effect.','failure_cause':'Other asset cause.',
            'severity':4,'occurrence':3,'detectability':3
        }).json()
        mismatch=client.post('/api/cbm/rules',headers=admin,json={
            'name':'V4.8 invalid cross-asset FMEA link','channel_id':channel['id'],'operator':'>=','threshold_low':99,'asset_fmea_id':other_fmea['id']
        })
        assert mismatch.status_code==422

        fm_csv=client.get('/api/exports/failure-modes.csv',headers=admin)
        fmea_csv=client.get('/api/exports/fmea.csv',headers=admin)
        assert fm_csv.status_code==200 and mode.json()['mode_no'] in fm_csv.text
        assert fmea_csv.status_code==200 and f['fmea_no'] in fmea_csv.text
        metrics=client.get('/api/metrics',headers=admin).text
        assert 'euas_active_fmea_records ' in metrics and 'euas_critical_fmea_records ' in metrics and 'euas_overdue_fmea_reviews ' in metrics
