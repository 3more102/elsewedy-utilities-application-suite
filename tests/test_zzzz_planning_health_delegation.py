from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_health_forecast_and_approval_delegation():
    with TestClient(app) as client:
        admin=auth(client)
        supervisor=auth(client,'supervisor','Supervisor@2026')
        planner=auth(client,'planner','Planner@2026')
        ref=client.get('/api/reference',headers=admin).json()
        users={x['username']:x['id'] for x in ref['users']}

        # Asset health engine returns deterministic portfolio scores and persists snapshots.
        health=client.get('/api/assets/health',headers=admin)
        assert health.status_code==200,health.text
        hp=health.json()
        assert hp['assets'] and 0 <= hp['average_score'] <= 100
        assert all(0 <= x['score'] <= 100 and x['risk_band'] in ('Healthy','Monitor','Warning','Critical') for x in hp['assets'])
        recalculated=client.post('/api/assets/health/recalculate',headers=admin)
        assert recalculated.status_code==200,recalculated.text
        assert recalculated.json()['count']==len(hp['assets'])
        tr=next(x for x in hp['assets'] if x['asset_no']=='TR-001')
        tr_detail=client.get(f"/api/assets/{tr['asset_id']}/health",headers=admin)
        assert tr_detail.status_code==200 and tr_detail.json()['history']

        # 90-day maintenance planning forecast exposes demand versus technician capacity.
        forecast=client.get('/api/planning/maintenance-forecast',headers=admin,params={'horizon_days':90})
        assert forecast.status_code==200,forecast.text
        plan=forecast.json()
        assert plan['horizon_days']==90 and plan['technicians']>=1 and plan['weeks']
        assert all('demand_hours' in w and 'capacity_hours' in w and 'utilization_pct' in w for w in plan['weeks'])
        assert plan['summary']['demand_hours'] >= 0

        # Supervisor delegates Work Management approvals to the planner.
        start=(datetime.now()-timedelta(minutes=1)).isoformat(timespec='seconds')
        end=(datetime.now()+timedelta(days=2)).isoformat(timespec='seconds')
        delegated=client.post('/api/approval-delegations',headers=supervisor,json={
            'delegate_user_id':users['planner'],'module':'Work Management','start_at':start,'end_at':end
        })
        assert delegated.status_code==200,delegated.text
        delegation_id=delegated.json()['id']

        tr_asset=next(x for x in client.get('/api/assets',headers=admin,params={'q':'TR-001'}).json() if x['asset_no']=='TR-001')
        wo=client.post('/api/work-orders',headers=admin,json={
            'title':'Delegated approval regression','asset_id':tr_asset['id'],'priority':'High',
            'supervisor_id':users['supervisor'],'estimated_hours':2
        })
        assert wo.status_code==200,wo.text
        wid=wo.json()['id']
        submitted=client.post(f'/api/work-orders/{wid}/transition',headers=admin,json={'action':'submit'})
        assert submitted.status_code==200,submitted.text

        queue=client.get('/api/approvals',headers=planner,params={'status':'Pending'}).json()
        ap=next(x for x in queue if x['record_id']==wid and x['record_type']=='work_order')
        decision=client.post(f"/api/approvals/{ap['id']}/decision",headers=planner,json={'decision':'approve','comments':'Approved under active delegation'})
        assert decision.status_code==200,decision.text
        assert decision.json()['status']=='Approved'

        # Once deactivated, the same delegate may no longer decide a user-assigned approval.
        assert client.patch(f'/api/approval-delegations/{delegation_id}/deactivate',headers=supervisor).status_code==200
        wo2=client.post('/api/work-orders',headers=admin,json={
            'title':'Delegation disabled regression','asset_id':tr_asset['id'],'priority':'Medium',
            'supervisor_id':users['supervisor'],'estimated_hours':1
        }).json()
        assert client.post(f"/api/work-orders/{wo2['id']}/transition",headers=admin,json={'action':'submit'}).status_code==200
        all_pending=client.get('/api/approvals',headers=admin,params={'status':'Pending'}).json()
        ap2=next(x for x in all_pending if x['record_id']==wo2['id'] and x['record_type']=='work_order')
        denied=client.post(f"/api/approvals/{ap2['id']}/decision",headers=planner,json={'decision':'approve'})
        assert denied.status_code==403
