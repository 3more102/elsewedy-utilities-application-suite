from datetime import date, timedelta
from fastapi.testclient import TestClient
from app.database import db, now
from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r=client.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':f"Bearer {r.json()['token']}"}


def test_workforce_parts_and_reliability_planning():
    with TestClient(app) as client:
        admin=auth(client)
        tech_headers=auth(client,'tech1','Tech@2026')
        ref=client.get('/api/reference',headers=admin).json()
        ncs=next(s for s in ref['sites'] if s['site_code']=='NCS-01')

        # Workforce is now schedule/profile based, not technicians x 40h.
        techs=client.get('/api/workforce/technicians',headers=admin,params={'site_id':ncs['id']})
        assert techs.status_code==200,techs.text
        tech_rows=techs.json()
        assert len(tech_rows)>=2
        assert all(t['craft_code']=='ELEC-HV' for t in tech_rows)
        tech2=next(t for t in tech_rows if t['username']=='tech2')

        # Create one approved absence on a guaranteed scheduled weekday. The
        # reference seed uses date.today()+14 and can land on a weekend,
        # making a capacity-variation assertion depend on the CI calendar.
        absence_day=date.today()+timedelta(days=7)
        while absence_day.weekday()>=5:
            absence_day+=timedelta(days=1)
        with db() as conn:
            admin_id=conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id']
            conn.execute('''INSERT INTO technician_absences(user_id,start_date,end_date,absence_type,hours_per_day,status,notes,created_by,created_at)
                            VALUES(?,?,?,?,?,'Approved',?,?,?)''',
                         (tech2['user_id'],absence_day.isoformat(),absence_day.isoformat(),'CI Regression Leave',8,
                          'Calendar-safe workforce capacity regression',admin_id,now()))

        capacity=client.get('/api/workforce/capacity',headers=admin,params={'site_id':ncs['id'],'weeks':5})
        assert capacity.status_code==200,capacity.text
        weeks=capacity.json()['weeks']
        assert weeks and all(w['source']=='workforce_schedule' for w in weeks)
        assert min(w['capacity_hours'] for w in weeks) < max(w['capacity_hours'] for w in weeks)

        changed=client.put(f"/api/workforce/technicians/{tech2['user_id']}",headers=admin,json={
            'craft_id':tech2['craft_id'],'home_site_id':ncs['id'],'weekly_hours':40,'efficiency_pct':50,'active':True
        })
        assert changed.status_code==200,changed.text
        lower=client.get('/api/workforce/capacity',headers=admin,params={'site_id':ncs['id'],'weeks':1}).json()['weeks'][0]
        assert lower['capacity_hours'] < max(w['capacity_hours'] for w in weeks)

        # Technician role cannot edit workforce planning master data.
        denied=client.put(f"/api/workforce/technicians/{tech2['user_id']}",headers=tech_headers,json={
            'craft_id':tech2['craft_id'],'home_site_id':ncs['id'],'weekly_hours':40,'efficiency_pct':80,'active':True
        })
        assert denied.status_code==403

        # Parts readiness is tied to planned requirements and live available stock.
        tr=next(a for a in client.get('/api/assets',headers=admin).json() if a['asset_no']=='TR-001')
        created=client.post('/api/work-orders',headers=admin,json={
            'title':'v3.7 parts readiness regression','asset_id':tr['id'],'priority':'Medium','estimated_hours':2,
            'target_start':date.today().isoformat(),'target_finish':(date.today()+timedelta(days=1)).isoformat()
        })
        assert created.status_code==200,created.text
        wo={'id':created.json()['id']}
        inv=client.get('/api/inventory',headers=admin).json()
        spare=max(inv,key=lambda i:float(i['available_stock']))
        available=float(spare['available_stock'])
        assert available>=1
        req=client.post(f"/api/work-orders/{wo['id']}/requirements",headers=admin,json={
            'item_id':spare['id'],'quantity':1,'required_by':(date.today()+timedelta(days=1)).isoformat()
        })
        assert req.status_code==200,req.text
        assert req.json()['readiness']['state']=='Ready'
        shortage=client.post(f"/api/work-orders/{wo['id']}/requirements",headers=admin,json={
            'item_id':spare['id'],'quantity':available+10,'required_by':(date.today()+timedelta(days=1)).isoformat()
        })
        assert shortage.status_code==200,shortage.text
        assert shortage.json()['readiness']['state']=='Shortage'
        forecast=client.get('/api/planning/maintenance-forecast',headers=admin,params={'site_id':ncs['id'],'horizon_days':90})
        assert forecast.status_code==200,forecast.text
        plan=forecast.json()
        assert plan['capacity_source']=='workforce_schedule'
        assert plan['summary']['parts_shortage_jobs']>=1
        assert any('ELEC-HV' in w['craft_capacity'] for w in plan['weeks'])

        # Reliability is calculated per asset/site from completed corrective failures and downtime.
        rel=client.get('/api/reliability/assets',headers=admin,params={'period_days':365})
        assert rel.status_code==200,rel.text
        pump=next(x for x in rel.json()['assets'] if x['asset_no']=='PMP-301')
        assert pump['failures']>=1
        assert pump['mttr_hours']>0
        assert pump['mtbf_hours'] is not None and pump['mtbf_hours']>0
        assert 0 < pump['availability_pct'] < 100

        sites=client.get('/api/reliability/sites',headers=admin,params={'period_days':365})
        assert sites.status_code==200,sites.text
        iwp=next(x for x in sites.json()['sites'] if x['site_code']=='IWP-01')
        assert iwp['failures']>=1 and iwp['availability_pct']<100

        analytics=client.get('/api/analytics',headers=admin)
        assert analytics.status_code==200,analytics.text
        data=analytics.json()
        assert data['summary']['reliability_period_days']==365
        assert data['site_reliability']
        assert data['maintenance_forecast']['capacity_source']=='workforce_schedule'
