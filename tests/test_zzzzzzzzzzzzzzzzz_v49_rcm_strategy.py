from datetime import date, timedelta
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def make_fmea(client, headers, asset_id, suffix, severity=9, occurrence=7, detectability=5):
    mode=client.post('/api/reliability/failure-modes',headers=headers,json={
        'name':f'v4.9 {suffix} failure mode','category':'RCM Regression','description':'RCM strategy regression failure mode.'
    })
    assert mode.status_code==200,mode.text
    fmea=client.post('/api/reliability/fmea',headers=headers,json={
        'asset_id':asset_id,'failure_mode_id':mode.json()['id'],'function_description':'Provide the required utility function reliably.',
        'failure_effect':'Loss or degradation of the required utility function.','failure_cause':'Representative degradational failure mechanism.',
        'current_controls':'Existing inspections and operational monitoring.','recommended_action':'Apply the selected reliability-centered maintenance strategy.',
        'severity':severity,'occurrence':occurrence,'detectability':detectability,'status':'Active'
    })
    assert fmea.status_code==200,fmea.text
    return mode.json(),fmea.json()


def test_v49_condition_based_rcm_governed_approval_activation_and_review():
    with TestClient(app) as client:
        planner=auth(client,'planner','Planner@2026')
        manager=auth(client,'seif','EUAS@2026')
        technician=auth(client,'tech1','Tech@2026')
        channel=next(x for x in client.get('/api/telemetry/channels',headers=planner).json() if x['channel_code']=='TEL-PMP301-VIB')
        mode,fmea=make_fmea(client,planner,channel['asset_id'],'condition-based')
        assert fmea['rpn']==315 and fmea['risk_band']=='Critical'

        cbm=client.post('/api/cbm/rules',headers=planner,json={
            'name':'v4.9 RCM linked vibration rule','channel_id':channel['id'],'operator':'>=','threshold_low':7.6,
            'consecutive_readings':2,'cooldown_minutes':60,'severity':'Critical','action_type':'Recommendation',
            'work_priority':'Critical','instructions':'Inspect rotating equipment condition.','asset_fmea_id':fmea['id']
        })
        assert cbm.status_code==200,cbm.text

        denied=client.post('/api/reliability/rcm',headers=technician,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Pump cannot maintain required hydraulic performance.',
            'consequence_classification':'Operational','strategy_type':'Condition-Based','task_description':'Monitor vibration and inspect on confirmed breach.',
            'justification':'Condition monitoring provides evidence before functional failure.','linked_cbm_rule_id':cbm.json()['id']
        })
        assert denied.status_code==403

        created=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Pump cannot maintain required hydraulic performance.',
            'consequence_classification':'Operational','strategy_type':'Condition-Based','task_description':'Monitor vibration and inspect bearings on a confirmed condition breach.',
            'justification':'The observable vibration condition provides a deterministic warning before functional failure.',
            'linked_cbm_rule_id':cbm.json()['id']
        })
        assert created.status_code==200,created.text
        rcm=created.json();assert rcm['status']=='Draft'
        expected_due=(date.today()+timedelta(days=90)).isoformat()
        assert rcm['review_due_date']==expected_due

        submitted=client.post(f"/api/reliability/rcm/{rcm['id']}/submit",headers=planner,json={'notes':'Submit RCM strategy for independent reliability approval.'})
        assert submitted.status_code==200,submitted.text
        assert submitted.json()['status']=='Review'
        approvals=client.get('/api/approvals?status=Pending',headers=manager).json()
        approval=next(a for a in approvals if a['record_type']=='rcm_strategy' and a['record_id']==rcm['id'])

        self_decision=client.post(f"/api/approvals/{approval['id']}/decision",headers=planner,json={
            'decision':'approve','comments':'Requester must not self approve.','current_password':'Planner@2026','signer_intent':f"I approve {rcm['strategy_no']}"
        })
        assert self_decision.status_code==403

        approved=client.post(f"/api/approvals/{approval['id']}/decision",headers=manager,json={
            'decision':'approve','comments':'RCM consequence and task selection reviewed.','current_password':'EUAS@2026','signer_intent':f"I approve {rcm['strategy_no']}"
        })
        assert approved.status_code==200,approved.text
        assert approved.json()['status']=='Approved' and approved.json()['signature_evidence']['evidence_no'].startswith('SIG-')
        evidence=client.get(f"/api/approvals/{approval['id']}/signature-evidence",headers=manager)
        assert evidence.status_code==200 and evidence.json()['credential_verified']==1 and evidence.json()['record_type']=='rcm_strategy'

        activated=client.post(f"/api/reliability/rcm/{rcm['id']}/activate",headers=planner,json={'notes':'Activate approved condition-based strategy.'})
        assert activated.status_code==200,activated.text
        row=next(x for x in client.get('/api/reliability/rcm',headers=planner).json() if x['id']==rcm['id'])
        assert row['status']=='Active' and row['linked_cbm_rule_no']==cbm.json()['rule_no'] and row['fmea_no']==fmea['fmea_no']

        reviewed=client.post(f"/api/reliability/rcm/{rcm['id']}/review",headers=planner,json={
            'outcome':'Continue','notes':'Condition monitoring remains technically applicable and the task is retained.'
        })
        assert reviewed.status_code==200,reviewed.text
        history=client.get(f"/api/reliability/rcm/{rcm['id']}/reviews",headers=planner).json()
        assert history and history[0]['outcome']=='Continue' and history[0]['old_status']=='Active'

        summary=client.get('/api/reliability/rcm/summary',headers=planner).json()
        assert summary['active_strategies']>=1 and summary['covered_fmea']>=1 and summary['strategy_coverage_pct']>0
        export=client.get('/api/exports/rcm-strategies.csv',headers=planner)
        assert export.status_code==200 and rcm['strategy_no'] in export.text and fmea['fmea_no'] in export.text
        metrics=client.get('/api/metrics',headers=manager).text
        assert 'euas_active_rcm_strategies ' in metrics and 'euas_rcm_strategy_coverage_pct ' in metrics and 'euas_critical_fmea_without_rcm ' in metrics


def test_v49_rcm_strategy_validation_time_based_linkage_revision_and_reapproval():
    with TestClient(app) as client:
        planner=auth(client,'planner','Planner@2026')
        manager=auth(client,'seif','EUAS@2026')
        assets=client.get('/api/assets',headers=planner).json()
        asset=assets[0];other=next(x for x in assets if x['id']!=asset['id'])
        _,fmea=make_fmea(client,planner,asset['id'],'time-based',severity=7,occurrence=5,detectability=4)

        unsafe=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Safety function unavailable on demand.',
            'consequence_classification':'Safety','strategy_type':'Run-to-Failure','task_description':'Allow operation until failure.',
            'justification':'This intentionally invalid strategy must be rejected by the RCM guard.'
        })
        assert unsafe.status_code==422

        missing_interval=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Asset cannot sustain its intended output.',
            'consequence_classification':'Operational','strategy_type':'Time-Based','task_description':'Perform scheduled intrusive maintenance.',
            'justification':'Age-related degradation supports a scheduled task but interval is intentionally missing.'
        })
        assert missing_interval.status_code==422

        wrong_pm=client.post('/api/maintenance-plans',headers=planner,json={
            'name':'v4.9 wrong asset PM','asset_id':other['id'],'trigger_type':'Calendar','interval_days':90,'next_due':(date.today()+timedelta(days=90)).isoformat(),'priority':'Medium','job_plan':'Wrong asset plan.'
        })
        assert wrong_pm.status_code==200,wrong_pm.text
        mismatch=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Asset cannot sustain its intended output.',
            'consequence_classification':'Operational','strategy_type':'Time-Based','task_description':'Perform scheduled intrusive maintenance.',
            'justification':'The PM link is intentionally on the wrong asset for regression validation.','interval_days':90,'linked_pm_plan_id':wrong_pm.json()['id']
        })
        assert mismatch.status_code==422

        pm=client.post('/api/maintenance-plans',headers=planner,json={
            'name':'v4.9 RCM scheduled task','asset_id':asset['id'],'trigger_type':'Calendar','interval_days':120,'next_due':(date.today()+timedelta(days=120)).isoformat(),'priority':'High','job_plan':'Execute age-based inspection and overhaul task.'
        })
        assert pm.status_code==200,pm.text
        created=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Asset cannot sustain its intended output.',
            'consequence_classification':'Operational','strategy_type':'Time-Based','task_description':'Perform scheduled age-based inspection and overhaul.',
            'justification':'Observed age-related degradation supports a periodic preventive task.','interval_days':120,'linked_pm_plan_id':pm.json()['id']
        })
        assert created.status_code==200,created.text
        rcm=created.json()
        duplicate=client.post('/api/reliability/rcm',headers=planner,json={
            'asset_fmea_id':fmea['id'],'functional_failure':'Duplicate strategy.','consequence_classification':'Operational','strategy_type':'Redesign',
            'task_description':'Duplicate.','justification':'Only one governed RCM strategy is allowed per FMEA record.'
        })
        assert duplicate.status_code==409

        assert client.post(f"/api/reliability/rcm/{rcm['id']}/submit",headers=planner,json={'notes':'Submit scheduled strategy.'}).status_code==200
        ap=next(a for a in client.get('/api/approvals?status=Pending',headers=manager).json() if a['record_type']=='rcm_strategy' and a['record_id']==rcm['id'])
        approve=client.post(f"/api/approvals/{ap['id']}/decision",headers=manager,json={
            'decision':'approve','comments':'Scheduled strategy accepted.','current_password':'EUAS@2026','signer_intent':f"I approve {rcm['strategy_no']}"
        })
        assert approve.status_code==200,approve.text
        assert client.post(f"/api/reliability/rcm/{rcm['id']}/activate",headers=planner,json={'notes':'Activate scheduled task strategy.'}).status_code==200

        revise=client.post(f"/api/reliability/rcm/{rcm['id']}/review",headers=planner,json={
            'outcome':'Revise','notes':'New engineering evidence requires redesign instead of periodic intrusive maintenance.'
        })
        assert revise.status_code==200 and revise.json()['status']=='Draft'
        patch=client.patch(f"/api/reliability/rcm/{rcm['id']}",headers=planner,json={
            'strategy_type':'Redesign','task_description':'Develop and approve an engineering redesign to eliminate the failure mechanism.',
            'justification':'Updated evidence shows the failure mechanism is not adequately controlled by periodic maintenance.',
            'interval_days':None,'linked_pm_plan_id':None
        })
        assert patch.status_code==200,patch.text
        assert patch.json()['strategy_type']=='Redesign' and patch.json()['linked_pm_plan_id'] is None

        resubmit=client.post(f"/api/reliability/rcm/{rcm['id']}/submit",headers=planner,json={'notes':'Resubmit revised redesign strategy.'})
        assert resubmit.status_code==200,resubmit.text
        ap2=next(a for a in client.get('/api/approvals?status=Pending',headers=manager).json() if a['record_type']=='rcm_strategy' and a['record_id']==rcm['id'])
        reject=client.post(f"/api/approvals/{ap2['id']}/decision",headers=manager,json={
            'decision':'reject','comments':'Engineering scope needs more definition.','current_password':'EUAS@2026','signer_intent':f"I reject {rcm['strategy_no']}"
        })
        assert reject.status_code==200,reject.text
        final=next(x for x in client.get('/api/reliability/rcm',headers=planner).json() if x['id']==rcm['id'])
        assert final['status']=='Draft' and 'more definition' in final['last_decision_comments']
