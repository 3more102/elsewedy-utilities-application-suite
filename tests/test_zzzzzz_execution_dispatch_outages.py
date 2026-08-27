from datetime import datetime, timedelta, date
from fastapi.testclient import TestClient
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_reservations_dispatch_and_outage_reliability():
    with TestClient(app) as client:
        admin=auth(client)
        tech1=auth(client,'tech1','Tech@2026')
        tech2=auth(client,'tech2','Tech2@2026')
        assets=client.get('/api/assets',headers=admin,params={'q':'TR-001'}).json()
        tr=next(a for a in assets if a['asset_no']=='TR-001')
        inv=client.get('/api/inventory',headers=admin).json()
        spare=max(inv,key=lambda x: float(x['available_stock']))
        available_before=float(spare['available_stock'])
        assert available_before>=2

        # Create and approve a dispatchable work order assigned later by dispatch.
        created=client.post('/api/work-orders',headers=admin,json={
            'title':'v3.8 execution coordination regression','asset_id':tr['id'],'priority':'High',
            'estimated_hours':3,'target_start':date.today().isoformat(),'target_finish':(date.today()+timedelta(days=1)).isoformat()
        })
        assert created.status_code==200,created.text
        wo_id=created.json()['id']
        assert client.post(f'/api/work-orders/{wo_id}/transition',headers=admin,json={'action':'submit'}).status_code==200
        assert client.post(f'/api/work-orders/{wo_id}/transition',headers=admin,json={'action':'approve'}).status_code==200

        # Plan and reserve material. Reserved stock must reduce portfolio availability but remain accessible to this WO.
        req=client.post(f'/api/work-orders/{wo_id}/requirements',headers=admin,json={'item_id':spare['id'],'quantity':2})
        assert req.status_code==200,req.text
        res=client.post(f'/api/work-orders/{wo_id}/reservations',headers=admin,json={'item_id':spare['id'],'quantity':2,'notes':'Stage for dispatch'})
        assert res.status_code==200,res.text
        reservation_id=res.json()['id']
        assert res.json()['readiness']['state']=='Ready'
        after_res=next(x for x in client.get('/api/inventory',headers=admin).json() if x['id']==spare['id'])
        assert float(after_res['reserved_stock'])>=2
        detail=client.get(f'/api/work-orders/{wo_id}',headers=admin).json()
        assert any(r['id']==reservation_id for r in detail['reservations'])
        assert detail['parts_readiness']['reserved_items']>=1

        # Generic stock issue cannot consume units protected by a work-order reservation.
        protected=client.post(f"/api/inventory/{spare['id']}/transaction",headers=admin,json={'tx_type':'ISSUE','quantity':available_before-1})
        assert protected.status_code==409,protected.text

        # Dispatch the approved work. Dispatch moves WO to Assigned and notifies the technician.
        dispatch=client.post(f'/api/work-orders/{wo_id}/dispatch',headers=admin,json={'technician_user_id':next(u['id'] for u in client.get('/api/admin/users',headers=admin).json() if u['username']=='tech2'),'eta_minutes':20,'notes':'Priority transformer response'})
        assert dispatch.status_code==200,dispatch.text
        dispatch_id=dispatch.json()['id']
        wo=client.get(f'/api/work-orders/{wo_id}',headers=admin).json()
        assert wo['status']=='Assigned' and wo['assigned_to_name']

        # Other technician cannot consume this WO reservation.
        denied=client.post(f'/api/reservations/{reservation_id}/issue',headers=tech1,json={'quantity':1})
        assert denied.status_code==403

        # Assigned technician advances dispatch; arrival starts the work and SLA response clock.
        for action,expected in [('accept','Accepted'),('enroute','En Route'),('arrive','On Site')]:
            r=client.post(f'/api/dispatch/{dispatch_id}/transition',headers=tech2,json={'action':action})
            assert r.status_code==200,r.text
            assert r.json()['status']==expected
        started=client.get(f'/api/work-orders/{wo_id}',headers=admin).json()
        assert started['status']=='In Progress' and started['actual_start']
        board=client.get('/api/dispatch/board',headers=admin).json()
        tech2row=next(t for t in board['technicians'] if t['username']=='tech2')
        assert tech2row['availability']=='Busy' and tech2row['dispatch']['id']==dispatch_id

        # Consume reserved material; reservation balance and reserved_stock fall together.
        issue=client.post(f'/api/reservations/{reservation_id}/issue',headers=tech2,json={'quantity':1})
        assert issue.status_code==200,issue.text
        assert issue.json()['status']=='Partially Issued'
        direct=client.post(f'/api/work-orders/{wo_id}/materials',headers=tech2,json={'item_id':spare['id'],'quantity':1})
        assert direct.status_code==200,direct.text
        reservation=next(r for r in client.get(f'/api/work-orders/{wo_id}/reservations',headers=admin).json() if r['id']==reservation_id)
        assert reservation['status']=='Issued' and float(reservation['issued_quantity'])==2

        # Record a real forced outage with explicit timestamps and close it.
        start=datetime.now()-timedelta(hours=2)
        outage=client.post('/api/outages',headers=admin,json={
            'asset_id':tr['id'],'work_order_id':wo_id,'outage_type':'Forced','cause_code':'TEMP-HIGH',
            'impact':'Transformer isolated for thermal investigation','lost_capacity':40,'capacity_unit':'MVA','start_at':start.isoformat(timespec='seconds')
        })
        assert outage.status_code==200,outage.text
        outage_id=outage.json()['id']
        open_ops=client.get('/api/operations',headers=admin).json()['open_outages']
        assert any(o['id']==outage_id for o in open_ops)
        closed=client.post(f'/api/outages/{outage_id}/close',headers=admin,json={'end_at':datetime.now().isoformat(timespec='seconds')})
        assert closed.status_code==200,closed.text
        assert 1.9 <= float(closed.json()['duration_hours']) <= 2.1

        # Reliability now prefers outage timestamps rather than work-order actual_hours.
        rel=client.get('/api/reliability/assets',headers=admin,params={'period_days':365}).json()['assets']
        tr_rel=next(x for x in rel if x['asset_no']=='TR-001')
        assert tr_rel['downtime_source']=='outage_events'
        assert tr_rel['failures']>=1 and tr_rel['downtime_hours']>=1.9
        assert tr_rel['availability_pct']<100

        # Completing dispatch frees the technician but does not falsely close the work order.
        done=client.post(f'/api/dispatch/{dispatch_id}/transition',headers=tech2,json={'action':'complete'})
        assert done.status_code==200 and done.json()['status']=='Completed'
        board2=client.get('/api/dispatch/board',headers=admin).json()
        tech2b=next(t for t in board2['technicians'] if t['username']=='tech2')
        assert tech2b['availability']=='Available'
        assert client.get(f'/api/work-orders/{wo_id}',headers=admin).json()['status']=='In Progress'
