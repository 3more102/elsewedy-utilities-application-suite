from __future__ import annotations
import asyncio, csv, hashlib, hmac, io, json, logging, secrets, shutil, sqlite3, time, uuid, zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib import request as urllib_request, error as urllib_error
from fastapi import FastAPI, Depends, Header, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import APP_NAME, APP_VERSION, STATIC_DIR, UPLOAD_DIR, SESSION_HOURS, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, ALLOWED_DOC_SUFFIXES, DB_BACKEND, DB_PATH, SCHEMA_VERSION, AUTOMATION_INTERVAL_MINUTES, EVENT_WEBHOOK_URL, EVENT_WEBHOOK_SECRET, OUTBOX_MAX_ATTEMPTS
from .database import db, init_db, now
from apps.audit import audit, verify_audit_chain
from apps.events import emit_event, process_outbox, rearm_outbox_event, workflow_event
from apps.assets import AssetDeleteBlocked, AssetNotFound, create_asset as create_asset_record, delete_asset as delete_asset_record, update_asset as update_asset_record
from core.shared import next_no
from apps.identity import hash_password, verify_password, current_user, login_key as _login_key, login_is_blocked as _login_is_blocked, login_failure as _login_failure, login_success as _login_success
from apps.authorization import require_roles, require_permission, effective_permissions, has_permission

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(hash_password)
    with db() as conn:
        _backfill_work_order_slas(conn)
        for incident in rows(conn.execute('SELECT id FROM alarm_incidents ORDER BY id')):
            _refresh_incident(conn,incident['id'])
    scheduler_task = asyncio.create_task(_automation_loop()) if AUTOMATION_INTERVAL_MINUTES > 0 else None
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url='/api/docs', redoc_url=None, lifespan=lifespan)


# Lightweight hardening suitable for the self-contained EUAS reference deployment.
# Production deployments should place EUAS behind a reverse proxy/WAF as well.
_REQUEST_METRICS = {'started_at': time.time(), 'requests_total': 0, 'errors_total': 0, 'latency_ms_total': 0.0, 'status': {}}
logger = logging.getLogger('euas')

@app.middleware('http')
async def security_headers(request: Request, call_next):
    request_id = request.headers.get('x-request-id') or uuid.uuid4().hex
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _REQUEST_METRICS['requests_total'] += 1
    _REQUEST_METRICS['latency_ms_total'] += elapsed_ms
    code = str(response.status_code)
    _REQUEST_METRICS['status'][code] = _REQUEST_METRICS['status'].get(code, 0) + 1
    if response.status_code >= 500:
        _REQUEST_METRICS['errors_total'] += 1
    response.headers['X-Request-ID'] = request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(self), geolocation=(self), microphone=()'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else response.headers.get('Cache-Control','no-cache')
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    return response

WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor')
WORK_ROLES = ('admin','maintenance_manager','planner','supervisor','technician')
INV_ROLES = ('admin','maintenance_manager','planner','storekeeper','technician')
DOC_WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor','technician','storekeeper','procurement','hse','project_manager')
PROC_ROLES = ('admin','maintenance_manager','procurement')
HSE_ROLES = ('admin','hse','maintenance_manager')
PROJECT_ROLES = ('admin','project_manager','maintenance_manager')
TELEMETRY_WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor','technician')

def telemetry_ingest_principal(authorization:Optional[str]=Header(default=None),x_euas_integration_key:Optional[str]=Header(default=None,alias='X-EUAS-Integration-Key')):
    if x_euas_integration_key:
        digest=hashlib.sha256(x_euas_integration_key.encode()).hexdigest();stamp=now()
        with db() as conn:
            key=conn.execute("SELECT * FROM integration_api_keys WHERE key_hash=? AND active=1 AND scope='telemetry:write'",(digest,)).fetchone()
            if not key or (key['expires_at'] and key['expires_at']<=stamp):raise HTTPException(401,'Invalid or expired integration API key')
            system=conn.execute("SELECT id,username,full_name FROM users WHERE username='system'").fetchone()
            if not system:raise HTTPException(503,'Automation principal is unavailable')
            conn.execute('UPDATE integration_api_keys SET last_used_at=? WHERE id=?',(stamp,key['id']))
            return {'id':system['id'],'username':system['username'],'full_name':f"Integration: {key['name']}",'role':'integration','role_name':'Integration Service','integration_key_no':key['key_no']}
    user=current_user(authorization)
    if user['role'] not in TELEMETRY_WRITE_ROLES:raise HTTPException(403,'Insufficient permissions')
    return user


def rows(cur): return [dict(r) for r in cur.fetchall()]
def one(cur):
    r=cur.fetchone(); return dict(r) if r else None

def post_cost(conn, work_order, cost_type, amount, quantity, reference, user_id):
    if amount<=0:return None
    no=next_no(conn,'maintenance_cost_ledger','entry_no','COST-',1)
    cur=conn.execute('INSERT INTO maintenance_cost_ledger(entry_no,work_order_id,asset_id,cost_type,amount,quantity,reference,posted_by,posted_at) VALUES(?,?,?,?,?,?,?,?,?)',(no,work_order['id'],work_order.get('asset_id'),cost_type,amount,quantity,reference or work_order['wo_no'],user_id,now()))
    return {'id':cur.lastrowid,'entry_no':no,'amount':amount}


def _asset_health(conn, asset_id:int):
    a=get_or_404(conn,"SELECT a.*,l.name location_name,s.name site_name FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?",(asset_id,),"Asset not found")
    condition_penalty={'Good':0,'Fair':10,'Warning':25,'Poor':40,'Critical':55}.get(a.get('condition'),15)
    criticality_penalty={'Low':0,'Medium':3,'High':7,'Critical':12}.get(a.get('criticality'),3)
    status_penalty=0 if a.get('status') in ('Operating','Standby') else (10 if a.get('status') in ('Under Maintenance','Restricted') else 25)
    open_work=rows(conn.execute("SELECT priority,target_finish,status FROM work_orders WHERE asset_id=? AND status NOT IN ('Completed','Closed','Cancelled')",(asset_id,)))
    high=sum(1 for w in open_work if w['priority'] in ('Emergency','Critical','High'))
    overdue=sum(1 for w in open_work if w.get('target_finish') and str(w['target_finish'])<date.today().isoformat())
    failed=conn.execute("SELECT COUNT(*) FROM inspections WHERE asset_id=? AND result='Fail'",(asset_id,)).fetchone()[0]
    sla=conn.execute("SELECT COUNT(*) FROM work_order_sla s JOIN work_orders w ON w.id=s.work_order_id WHERE w.asset_id=? AND (s.response_status='Breached' OR s.resolution_status='Breached')",(asset_id,)).fetchone()[0]
    alarm_rows=rows(conn.execute("SELECT severity FROM operational_alarms WHERE asset_id=? AND status IN ('Open','Acknowledged')",(asset_id,)))
    alarms=len(alarm_rows);critical_alarms=sum(1 for x in alarm_rows if x['severity']=='Critical')
    penalties={
      'condition':condition_penalty,'criticality':criticality_penalty,'status':status_penalty,
      'priority_work':min(25,high*7),'overdue_work':min(20,overdue*5),
      'failed_inspections':min(16,int(failed)*8),'sla_breaches':min(10,int(sla)*5),
      'operational_alarms':min(18,alarms*5+critical_alarms*5)
    }
    score=max(0,min(100,100-sum(penalties.values())))
    band='Healthy' if score>=85 else 'Monitor' if score>=70 else 'Warning' if score>=50 else 'Critical'
    return {'asset_id':asset_id,'asset_no':a['asset_no'],'name':a['name'],'site_name':a.get('site_name'),'location_name':a.get('location_name'),
            'score':round(score,1),'risk_band':band,'factors':penalties,'open_priority_work':high,'overdue_work':overdue,
            'failed_inspections':int(failed),'sla_breaches':int(sla),'condition':a.get('condition'),'criticality':a.get('criticality'),'status':a.get('status')}

def _save_asset_health(conn, asset_id:int, actor_id:Optional[int]=None):
    h=_asset_health(conn,asset_id)
    conn.execute('INSERT INTO asset_health_snapshots(asset_id,score,risk_band,factors_json,calculated_at,calculated_by) VALUES(?,?,?,?,?,?)',
                 (asset_id,h['score'],h['risk_band'],json.dumps(h['factors'],sort_keys=True),now(),actor_id))
    return h

def _delegation_active(conn, approval, user_id:int):
    assigned=approval.get('assigned_user_id')
    if not assigned:return False
    stamp=now()
    r=conn.execute("SELECT id FROM approval_delegations WHERE delegator_user_id=? AND delegate_user_id=? AND active=1 AND start_at<=? AND end_at>=? AND (module='*' OR module=?) ORDER BY id DESC LIMIT 1",
                   (assigned,user_id,stamp,stamp,approval.get('module') or '')).fetchone()
    return bool(r)

def _forecast_bucket_start(d:date):
    return d-timedelta(days=d.weekday())

def _parse_days_of_week(value):
    result=set()
    for raw in str(value or '0,1,2,3,4').split(','):
        try:
            n=int(raw.strip())
            if 0<=n<=6:result.add(n)
        except Exception:
            pass
    return result or {0,1,2,3,4}

def _workforce_week_capacity(conn, week_start:date, site_id:Optional[int]=None):
    week_end=week_start+timedelta(days=6)
    sql="""SELECT tp.*,u.full_name,u.username,c.craft_code,c.name craft_name,s.site_code,s.name site_name
             FROM technician_profiles tp JOIN users u ON u.id=tp.user_id JOIN roles r ON r.id=u.role_id
             LEFT JOIN crafts c ON c.id=tp.craft_id LEFT JOIN sites s ON s.id=tp.home_site_id
             WHERE tp.active=1 AND u.active=1 AND r.code='technician'"""
    args=[]
    if site_id is not None:sql+=' AND tp.home_site_id=?';args.append(site_id)
    techs=rows(conn.execute(sql,args))
    details=[];craft_capacity={};total=0.0
    for t in techs:
        assignments=rows(conn.execute("""SELECT tsa.*,st.paid_hours,st.shift_code,st.name shift_name FROM technician_shift_assignments tsa
          JOIN shift_templates st ON st.id=tsa.shift_id WHERE tsa.user_id=? AND tsa.active=1 AND st.active=1
          AND tsa.effective_from<=? AND (tsa.effective_to IS NULL OR tsa.effective_to>=?)""",(t['user_id'],week_end.isoformat(),week_start.isoformat())))
        scheduled=0.0
        if assignments:
            for offset in range(7):
                day=week_start+timedelta(days=offset);day_hours=0.0
                for a in assignments:
                    if day.weekday() not in _parse_days_of_week(a.get('days_of_week')):continue
                    if str(a['effective_from'])[:10]>day.isoformat():continue
                    if a.get('effective_to') and str(a['effective_to'])[:10]<day.isoformat():continue
                    day_hours=max(day_hours,float(a.get('paid_hours') or 0))
                scheduled+=day_hours
        else:
            scheduled=float(t.get('weekly_hours') or 40)
        absence=0.0
        absences=rows(conn.execute("""SELECT * FROM technician_absences WHERE user_id=? AND status='Approved'
          AND start_date<=? AND end_date>=?""",(t['user_id'],week_end.isoformat(),week_start.isoformat())))
        for a in absences:
            try:a_start=date.fromisoformat(str(a['start_date'])[:10]);a_end=date.fromisoformat(str(a['end_date'])[:10])
            except Exception:continue
            for offset in range(7):
                day=week_start+timedelta(days=offset)
                if a_start<=day<=a_end and day.weekday()<5:absence+=float(a.get('hours_per_day') or 8)
        available=max(0.0,scheduled-absence)*max(0.0,min(100.0,float(t.get('efficiency_pct') or 100)))/100.0
        available=round(available,1);total+=available
        craft=t.get('craft_code') or 'UNASSIGNED';craft_capacity[craft]=round(craft_capacity.get(craft,0.0)+available,1)
        details.append({'user_id':t['user_id'],'username':t['username'],'name':t['full_name'],'craft_code':t.get('craft_code'),'craft_name':t.get('craft_name'),
                        'site_code':t.get('site_code'),'site_name':t.get('site_name'),'scheduled_hours':round(scheduled,1),'absence_hours':round(absence,1),
                        'efficiency_pct':float(t.get('efficiency_pct') or 100),'available_hours':available})
    # Compatibility fallback for upgraded databases that do not yet have workforce profiles.
    if not techs:
        q="SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='technician' AND u.active=1"
        count=int(conn.execute(q).fetchone()[0]);total=float(count)*40.0
        return {'technicians':count,'capacity_hours':round(total,1),'craft_capacity':{'UNASSIGNED':round(total,1)},'details':[],'source':'role_fallback'}
    return {'technicians':len(techs),'capacity_hours':round(total,1),'craft_capacity':craft_capacity,'details':details,'source':'workforce_schedule'}

def _reservation_rows(conn, work_order_id:int):
    return rows(conn.execute("""SELECT r.*,i.item_no,i.name item_name,i.unit,i.current_stock,i.reserved_stock,u.full_name reserved_by_name
      FROM inventory_reservations r JOIN inventory_items i ON i.id=r.inventory_item_id JOIN users u ON u.id=r.reserved_by
      WHERE r.work_order_id=? ORDER BY r.id DESC""",(work_order_id,)))

def _sync_reserved_stock(conn, item_id:int):
    reserved=conn.execute("SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations WHERE inventory_item_id=? AND status IN ('Reserved','Partially Issued')",(item_id,)).fetchone()[0] or 0
    conn.execute('UPDATE inventory_items SET reserved_stock=? WHERE id=?',(max(0.0,float(reserved)),item_id))
    return max(0.0,float(reserved))

def _work_order_parts_readiness(conn, work_order_id:int):
    reqs=rows(conn.execute("""SELECT r.*,i.item_no,i.name,i.unit,i.current_stock,i.reserved_stock,w.warehouse_code,w.name warehouse_name
      FROM work_order_requirements r JOIN inventory_items i ON i.id=r.inventory_item_id JOIN warehouses w ON w.id=i.warehouse_id
      WHERE r.work_order_id=? AND r.status<>'Cancelled' ORDER BY i.item_no""",(work_order_id,)))
    if not reqs:return {'state':'Unknown','ready':None,'requirements':[],'shortage_items':0,'reserved_items':0}
    shortages=0;reserved_items=0
    for r in reqs:
        issued=float(conn.execute('SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',(work_order_id,r['inventory_item_id'])).fetchone()[0] or 0)
        reserved=float(conn.execute("SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')",(work_order_id,r['inventory_item_id'])).fetchone()[0] or 0)
        unreserved=max(0.0,float(r['current_stock'])-float(r['reserved_stock']))
        remaining=max(0.0,float(r['quantity'])-issued)
        secured=reserved+unreserved
        r['available_stock']=round(unreserved,3);r['reserved_for_work']=round(reserved,3);r['issued_quantity']=round(issued,3);r['remaining_required']=round(remaining,3)
        r['shortage']=round(max(0.0,remaining-secured),3);r['ready']=r['shortage']<=0
        if reserved>0:reserved_items+=1
        if not r['ready']:shortages+=1
    return {'state':'Ready' if shortages==0 else 'Shortage','ready':shortages==0,'requirements':reqs,'shortage_items':shortages,'reserved_items':reserved_items}

def _maintenance_forecast(conn,horizon_days:int=90,site_id:Optional[int]=None):
    start=date.today();end=start+timedelta(days=horizon_days)
    weeks={}
    cursor=_forecast_bucket_start(start)
    while cursor<=end:
        cap=_workforce_week_capacity(conn,cursor,site_id)
        weeks[cursor.isoformat()]={'week_start':cursor.isoformat(),'pm_jobs':0,'backlog_jobs':0,'demand_hours':0.0,'capacity_hours':cap['capacity_hours'],
                                   'technicians':cap['technicians'],'capacity_source':cap['source'],'parts_ready_jobs':0,'parts_shortage_jobs':0,'parts_unknown_jobs':0,
                                   'craft_demand':{},'craft_capacity':cap['craft_capacity']}
        cursor+=timedelta(days=7)
    site_clause='';args=[]
    if site_id is not None:
        site_clause=' AND l.site_id=?';args.append(site_id)
    pms=rows(conn.execute("SELECT p.*,a.asset_no,l.site_id FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id LEFT JOIN locations l ON l.id=a.location_id WHERE p.active=1 AND p.next_due IS NOT NULL"+site_clause,args))
    for p in pms:
        try:d=date.fromisoformat(str(p['next_due'])[:10])
        except Exception:continue
        if start<=d<=end:
            key=_forecast_bucket_start(d).isoformat();b=weeks.get(key)
            if b:
                b['pm_jobs']+=1;b['demand_hours']+=2.0;b['parts_unknown_jobs']+=1
    wargs=[];wsite=''
    if site_id is not None:wsite=' AND l.site_id=?';wargs.append(site_id)
    work=rows(conn.execute("SELECT w.*,l.site_id FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id WHERE w.status NOT IN ('Completed','Closed','Cancelled')"+wsite,wargs))
    for w in work:
        raw=w.get('target_start') or w.get('target_finish') or start.isoformat()
        try:d=date.fromisoformat(str(raw)[:10])
        except Exception:d=start
        if d<start:d=start
        if d>end:continue
        key=_forecast_bucket_start(d).isoformat();b=weeks.get(key)
        if not b:continue
        demand=float(w.get('estimated_hours') or 2.0);b['backlog_jobs']+=1;b['demand_hours']+=demand
        readiness=_work_order_parts_readiness(conn,w['id'])
        if readiness['state']=='Ready':b['parts_ready_jobs']+=1
        elif readiness['state']=='Shortage':b['parts_shortage_jobs']+=1
        else:b['parts_unknown_jobs']+=1
        crafts=rows(conn.execute("""SELECT c.craft_code,r.planned_hours FROM work_order_craft_requirements r JOIN crafts c ON c.id=r.craft_id WHERE r.work_order_id=?""",(w['id'],)))
        if crafts:
            for c in crafts:b['craft_demand'][c['craft_code']]=round(b['craft_demand'].get(c['craft_code'],0.0)+float(c['planned_hours'] or 0),1)
        else:b['craft_demand']['UNASSIGNED']=round(b['craft_demand'].get('UNASSIGNED',0.0)+demand,1)
    out=[]
    for b in weeks.values():
        b['demand_hours']=round(b['demand_hours'],1);capacity=float(b['capacity_hours'] or 0);b['utilization_pct']=round(100*b['demand_hours']/capacity,1) if capacity else (100.0 if b['demand_hours'] else 0.0)
        b['capacity_state']='Over Capacity' if b['utilization_pct']>100 else 'High' if b['utilization_pct']>=80 else 'Available'
        craft_states={}
        for code,demand in b['craft_demand'].items():
            cap=float(b['craft_capacity'].get(code,0));craft_states[code]={'demand_hours':round(demand,1),'capacity_hours':round(cap,1),'shortage_hours':round(max(0,demand-cap),1)}
        b['craft_states']=craft_states
        out.append(b)
    total_capacity=round(sum(x['capacity_hours'] for x in out),1)
    return {'horizon_days':horizon_days,'technicians':max([x['technicians'] for x in out] or [0]),'weekly_capacity_hours':round(out[0]['capacity_hours'],1) if out else 0,'capacity_source':out[0]['capacity_source'] if out else 'none','weeks':out,
            'summary':{'pm_jobs':sum(x['pm_jobs'] for x in out),'backlog_jobs':sum(x['backlog_jobs'] for x in out),'demand_hours':round(sum(x['demand_hours'] for x in out),1),
                       'capacity_hours':total_capacity,'peak_utilization_pct':max([x['utilization_pct'] for x in out] or [0]),
                       'parts_shortage_jobs':sum(x['parts_shortage_jobs'] for x in out),'parts_ready_jobs':sum(x['parts_ready_jobs'] for x in out)}}

def _outage_overlap_hours(start_value, end_value, window_start:datetime, window_end:datetime):
    try:start=_dt(start_value)
    except Exception:return 0.0
    try:end=_dt(end_value) if end_value else min(datetime.now(),window_end)
    except Exception:end=min(datetime.now(),window_end)
    left=max(start,window_start);right=min(end,window_end)
    return max(0.0,(right-left).total_seconds()/3600.0)

def _asset_reliability_rows(conn, period_days:int=365, site_id:Optional[int]=None):
    today=date.today();cutoff=today-timedelta(days=period_days);window_end=datetime.now()
    sql="""SELECT a.id,a.asset_no,a.name,a.commissioning_date,a.criticality,a.condition,s.id site_id,s.site_code,s.name site_name
             FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1"""
    args=[]
    if site_id is not None:sql+=' AND s.id=?';args.append(site_id)
    assets=rows(conn.execute(sql,args));result=[]
    for a in assets:
        start=cutoff
        if a.get('commissioning_date'):
            try:start=max(start,date.fromisoformat(str(a['commissioning_date'])[:10]))
            except Exception:pass
        window_start=datetime.combine(start,datetime.min.time());period_hours=max(24.0,(window_end-window_start).total_seconds()/3600.0)
        outages=rows(conn.execute("""SELECT * FROM asset_outages WHERE asset_id=? AND outage_type='Forced' AND start_at<=? AND (end_at IS NULL OR end_at>=?) ORDER BY start_at""",(a['id'],window_end.isoformat(timespec='seconds'),window_start.isoformat(timespec='seconds'))))
        if outages:
            failure_count=len(outages);downtime=sum(_outage_overlap_hours(x['start_at'],x.get('end_at'),window_start,window_end) for x in outages);source='outage_events'
        else:
            failures=rows(conn.execute("""SELECT id,wo_no,actual_hours,actual_cost,COALESCE(actual_finish,created_at) event_date FROM work_orders
              WHERE asset_id=? AND status IN ('Completed','Closed') AND (work_type LIKE 'Corrective%' OR work_type='Breakdown')
              AND COALESCE(actual_finish,created_at)>=?""",(a['id'],start.isoformat())))
            failure_count=len(failures);downtime=sum(float(x.get('actual_hours') or 0) for x in failures);source='work_order_hours_fallback' if failures else 'no_failures'
        uptime=max(0.0,period_hours-downtime);mttr=round(downtime/failure_count,2) if failure_count else 0.0
        mtbf=round(uptime/failure_count,2) if failure_count else None
        availability=round(100*uptime/period_hours,3) if period_hours else 100.0
        cost=conn.execute('SELECT COALESCE(SUM(amount),0) FROM maintenance_cost_ledger WHERE asset_id=? AND posted_at>=?',(a['id'],start.isoformat())).fetchone()[0] or 0
        result.append({**a,'period_days':max(1,(today-start).days),'period_hours':round(period_hours,1),'failures':failure_count,'downtime_hours':round(downtime,2),
                       'downtime_source':source,'mtbf_hours':mtbf,'mttr_hours':mttr,'availability_pct':availability,'maintenance_cost':round(float(cost),2)})
    return result

def _site_reliability_rows(conn, period_days:int=365):
    assets=_asset_reliability_rows(conn,period_days,None);sites={}
    for a in assets:
        key=a.get('site_id');
        if key is None:continue
        s=sites.setdefault(key,{'site_id':key,'site_code':a.get('site_code'),'site_name':a.get('site_name'),'assets':0,'failures':0,'period_hours':0.0,'downtime_hours':0.0,'maintenance_cost':0.0})
        s['assets']+=1;s['failures']+=a['failures'];s['period_hours']+=a['period_hours'];s['downtime_hours']+=a['downtime_hours'];s['maintenance_cost']+=a['maintenance_cost']
    out=[]
    for s in sites.values():
        uptime=max(0.0,s['period_hours']-s['downtime_hours']);failures=s['failures']
        s['mtbf_hours']=round(uptime/failures,2) if failures else None;s['mttr_hours']=round(s['downtime_hours']/failures,2) if failures else 0.0
        s['availability_pct']=round(100*uptime/s['period_hours'],3) if s['period_hours'] else 100.0;s['maintenance_cost']=round(s['maintenance_cost'],2)
        s['period_hours']=round(s['period_hours'],1);s['downtime_hours']=round(s['downtime_hours'],2);out.append(s)
    return sorted(out,key=lambda x:(x['availability_pct'],x['site_name'] or ''))

def notify(conn,title,message,severity='Info',user_id=None,role_code=None,module='',record_id=''):
    conn.execute('INSERT INTO notifications(user_id,role_code,title,message,severity,link_module,link_id,created_at) VALUES(?,?,?,?,?,?,?,?)',(user_id,role_code,title,message,severity,module,record_id,now()))

def create_approval(conn,module,record_type,record_id,record_code,title,requested_by,assigned_role=None,assigned_user_id=None):
    existing=conn.execute("SELECT * FROM approval_requests WHERE module=? AND record_type=? AND record_id=? AND status='Pending'",(module,record_type,record_id)).fetchone()
    if existing:return dict(existing)
    no=next_no(conn,'approval_requests','approval_no','APR-',9001)
    cur=conn.execute("INSERT INTO approval_requests(approval_no,module,record_type,record_id,record_code,title,requested_by,assigned_role,assigned_user_id,status,requested_at) VALUES(?,?,?,?,?,?,?,?,?,'Pending',?)",(no,module,record_type,record_id,record_code,title,requested_by,assigned_role,assigned_user_id,now()))
    notify(conn,'Approval waiting',f'{record_code} requires approval','Info',assigned_user_id,assigned_role,'approvals',no)
    return {'id':cur.lastrowid,'approval_no':no}

def resolve_approval(conn,module,record_type,record_id,decision,user_id,comments=''):
    ap=conn.execute("SELECT * FROM approval_requests WHERE module=? AND record_type=? AND record_id=? AND status='Pending' ORDER BY id DESC LIMIT 1",(module,record_type,record_id)).fetchone()
    if not ap:return None
    new_status='Approved' if decision.lower()=='approve' else 'Rejected'
    conn.execute('UPDATE approval_requests SET status=?,decided_at=?,decided_by=?,comments=? WHERE id=?',(new_status,now(),user_id,comments,ap['id']))
    return dict(ap)|{'status':new_status}

def get_or_404(conn, sql, args, message='Record not found'):
    r=conn.execute(sql,args).fetchone()
    if not r: raise HTTPException(404,message)
    return dict(r)

def _approval_expected_intent(decision:str, record_code:str) -> str:
    action='approve' if decision.lower().strip()=='approve' else 'reject'
    return f'I {action} {record_code}'

def _approval_target_snapshot(conn, approval):
    if approval['record_type']=='work_order':
        return get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(approval['record_id'],),'Work order not found')
    if approval['record_type']=='purchase_requisition':
        return get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(approval['record_id'],),'Purchase requisition not found')
    if approval['record_type']=='alarm_shelf':
        return get_or_404(conn,'SELECT * FROM alarm_shelves WHERE id=?',(approval['record_id'],),'Alarm shelf request not found')
    if approval['record_type']=='rcm_strategy':
        return get_or_404(conn,'SELECT * FROM rcm_strategies WHERE id=?',(approval['record_id'],),'RCM strategy not found')
    return {'record_type':approval['record_type'],'record_id':approval['record_id']}

def _approval_signature_digest(prev_hash:str, payload_json:str) -> str:
    return hashlib.sha256(f'{prev_hash or ""}|{payload_json}'.encode('utf-8')).hexdigest()

def _record_approval_signature(conn, approval, target_status:str, user, intent_statement:str, comments:str, delegated:bool, record_snapshot:dict):
    evidence_no=next_no(conn,'approval_signature_evidence','evidence_no','SIG-',9001)
    signed_at=now()
    payload={
      'schema':1,'evidence_no':evidence_no,
      'approval':{'id':approval['id'],'approval_no':approval['approval_no'],'module':approval['module'],'record_type':approval['record_type'],
                  'record_id':approval['record_id'],'record_code':approval['record_code'],'title':approval['title'],
                  'requested_by':approval['requested_by'],'requested_at':approval['requested_at']},
      'decision':target_status,
      'signer':{'user_id':user['id'],'username':user['username'],'full_name':user['full_name'],'role':user['role']},
      'authority':{'delegated':bool(delegated)},'credential_verified':True,
      'intent_statement':intent_statement,'comments':comments or '', 'signed_at':signed_at,
      'record_snapshot':record_snapshot
    }
    payload_json=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str)
    prev=conn.execute('SELECT evidence_hash FROM approval_signature_evidence ORDER BY id DESC LIMIT 1').fetchone()
    prev_hash=prev['evidence_hash'] if prev and prev['evidence_hash'] else ''
    digest=_approval_signature_digest(prev_hash,payload_json)
    cur=conn.execute('''INSERT INTO approval_signature_evidence(
      evidence_no,approval_id,approval_no,module,record_type,record_id,record_code,decision,signer_user_id,signer_username,signer_name,signer_role,
      delegated_authority,credential_verified,intent_statement,comments,signed_at,payload_json,prev_hash,evidence_hash
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
      evidence_no,approval['id'],approval['approval_no'],approval['module'],approval['record_type'],approval['record_id'],approval['record_code'],target_status,
      user['id'],user['username'],user['full_name'],user['role'],int(bool(delegated)),1,intent_statement,comments or '',signed_at,payload_json,prev_hash,digest))
    return {'id':cur.lastrowid,'evidence_no':evidence_no,'signed_at':signed_at,'evidence_hash':digest,'prev_hash':prev_hash}

def verify_approval_signature_chain(conn):
    prev='';checked=0
    for r in conn.execute('SELECT * FROM approval_signature_evidence ORDER BY id').fetchall():
        checked+=1
        payload_json=r['payload_json'] or ''
        expected=_approval_signature_digest(prev,payload_json)
        try:payload=json.loads(payload_json)
        except Exception:
            return {'valid':False,'checked':checked,'first_invalid_id':r['id'],'first_invalid_evidence_no':r['evidence_no'],'reason':'invalid_payload_json','head_hash':prev}
        approval=payload.get('approval') or {}; signer=payload.get('signer') or {}; authority=payload.get('authority') or {}
        columns_match=(
          payload.get('evidence_no')==r['evidence_no'] and approval.get('approval_no')==r['approval_no'] and approval.get('module')==r['module'] and
          approval.get('record_type')==r['record_type'] and int(approval.get('record_id',-1))==int(r['record_id']) and approval.get('record_code')==r['record_code'] and
          payload.get('decision')==r['decision'] and int(signer.get('user_id',-1))==int(r['signer_user_id']) and signer.get('username')==r['signer_username'] and
          signer.get('full_name')==r['signer_name'] and signer.get('role')==r['signer_role'] and int(bool(authority.get('delegated')))==int(r['delegated_authority']) and
          bool(payload.get('credential_verified'))==bool(r['credential_verified']) and payload.get('intent_statement')==r['intent_statement'] and
          (payload.get('comments') or '')==(r['comments'] or '') and payload.get('signed_at')==r['signed_at']
        )
        if (r['prev_hash'] or '')!=prev or (r['evidence_hash'] or '')!=expected or not columns_match:
            reason='chain_link' if (r['prev_hash'] or '')!=prev else ('hash_mismatch' if (r['evidence_hash'] or '')!=expected else 'column_payload_mismatch')
            return {'valid':False,'checked':checked,'first_invalid_id':r['id'],'first_invalid_evidence_no':r['evidence_no'],'reason':reason,'head_hash':prev}
        prev=r['evidence_hash']
    return {'valid':True,'checked':checked,'first_invalid_id':None,'first_invalid_evidence_no':None,'reason':'ok','head_hash':prev}

def user_id_by_username(conn, username):
    r=conn.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone(); return r['id'] if r else None

def _dt(value):
    if isinstance(value,datetime): return value
    return datetime.fromisoformat(str(value))

def _channel_site(conn, asset_id:int):
    r=conn.execute('SELECT s.id site_id,s.site_code,s.name site_name FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?',(asset_id,)).fetchone()
    return dict(r) if r else {'site_id':None,'site_code':None,'site_name':None}

SEVERITY_RANK={'Info':0,'Warning':1,'Critical':2}


def _topology_graph(conn):
    links=rows(conn.execute("SELECT * FROM asset_topology_links WHERE active=1 ORDER BY id"))
    down={};undirected={}
    for link in links:
        u=int(link['upstream_asset_id']);v=int(link['downstream_asset_id'])
        down.setdefault(u,[]).append(v)
        undirected.setdefault(u,[]).append(v);undirected.setdefault(v,[]).append(u)
    return links,down,undirected


def _graph_distance(graph:dict[int,list[int]], start:int, target:int, max_hops:int=3):
    if start==target:return 0
    seen={start};front=[(start,0)]
    while front:
        node,hops=front.pop(0)
        if hops>=max_hops:continue
        for nxt in graph.get(node,[]):
            if nxt==target:return hops+1
            if nxt not in seen:
                seen.add(nxt);front.append((nxt,hops+1))
    return None


def _incident_root_cause(conn, members:list[dict]):
    asset_ids=sorted({int(x['asset_id']) for x in members})
    if not asset_ids:return {'asset_id':None,'asset_no':'','mode':'Asset','score':0,'reason':'No alarm members','hops':0}
    placeholders=','.join('?' for _ in asset_ids)
    meta={int(r['id']):dict(r) for r in conn.execute(f'SELECT id,asset_no,name FROM assets WHERE id IN ({placeholders})',asset_ids).fetchall()}
    if len(asset_ids)==1:
        a=meta[asset_ids[0]]
        return {'asset_id':asset_ids[0],'asset_no':a['asset_no'],'mode':'Asset','score':100.0,'reason':f"All correlated alarms originate from {a['asset_no']}.",'hops':0}
    _,down,undirected=_topology_graph(conn)
    first_seen={aid:min(_dt(x['opened_at']) for x in members if int(x['asset_id'])==aid) for aid in asset_ids}
    severity={aid:max((SEVERITY_RANK.get(x['severity'],0) for x in members if int(x['asset_id'])==aid),default=0) for aid in asset_ids}
    candidates=[]
    for aid in asset_ids:
        directed=[_graph_distance(down,aid,other,3) for other in asset_ids if other!=aid]
        downstream_count=sum(d is not None for d in directed)
        connected=[_graph_distance(undirected,aid,other,3) for other in asset_ids if other!=aid]
        connected_count=sum(d is not None for d in connected)
        candidates.append((aid,downstream_count,connected_count,first_seen[aid],severity[aid],directed,connected))
    candidates.sort(key=lambda x:(-x[1],-x[2],x[3],-x[4],x[0]))
    aid,downstream_count,connected_count,opened,sev,directed,connected=candidates[0]
    other_count=max(len(asset_ids)-1,1)
    earliest=min(first_seen.values())==opened
    score=min(95.0,60.0+25.0*(downstream_count/other_count)+(10.0 if earliest else 0.0))
    reachable=[d for d in directed if d is not None] or [d for d in connected if d is not None]
    hops=max(reachable,default=0)
    a=meta[aid]
    if downstream_count:
        reason=f"{a['asset_no']} is upstream of {downstream_count} of {other_count} other alarmed asset(s) within the configured topology"
        if earliest:reason+=' and its alarm evidence appeared earliest'
        reason+='.'
    else:
        reason=f"{a['asset_no']} is the earliest alarmed asset in the connected topology; no alarmed asset is upstream of another alarmed member."
    return {'asset_id':aid,'asset_no':a['asset_no'],'mode':'Topology','score':round(score,1),'reason':reason,'hops':hops}


def _incident_candidate_distance(conn, incident_id:int, asset_id:int, max_hops:int=2):
    member_assets=[int(r['asset_id']) for r in conn.execute('SELECT DISTINCT oa.asset_id FROM alarm_incident_members m JOIN operational_alarms oa ON oa.id=m.alarm_id WHERE m.incident_id=?',(incident_id,)).fetchall()]
    if asset_id in member_assets:return 0
    _,_,undirected=_topology_graph(conn)
    ds=[_graph_distance(undirected,asset_id,x,max_hops) for x in member_assets]
    ds=[x for x in ds if x is not None]
    return min(ds) if ds else None


def _normalize_quality(value:str) -> str:
    q=str(value or 'Good').strip().title()
    if q not in ('Good','Uncertain','Bad'):
        raise HTTPException(422,"Telemetry quality must be Good, Uncertain or Bad")
    return q


def _active_alarm_suppression(conn, channel:dict, captured_at:str):
    """Return the most specific active suppression window for a telemetry channel."""
    site=_channel_site(conn,channel['asset_id'])
    rows_=rows(conn.execute("""SELECT s.*,u.full_name created_by_name FROM alarm_suppressions s
      LEFT JOIN users u ON u.id=s.created_by
      WHERE s.active=1 AND s.start_at<=? AND s.end_at>=?
      AND ((s.channel_id=? ) OR (s.channel_id IS NULL AND s.asset_id=? )
           OR (s.channel_id IS NULL AND s.asset_id IS NULL AND s.site_id=?))""",
      (captured_at,captured_at,channel['id'],channel['asset_id'],site.get('site_id'))))
    if not rows_:return None
    rows_.sort(key=lambda x:(1 if x.get('channel_id') else 0,1 if x.get('asset_id') else 0,1 if x.get('site_id') else 0),reverse=True)
    return rows_[0]


def _incident_member_summary(conn, incident_id:int):
    members=rows(conn.execute("""SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name
      FROM alarm_incident_members m JOIN operational_alarms oa ON oa.id=m.alarm_id
      JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id
      WHERE m.incident_id=? ORDER BY oa.opened_at""",(incident_id,)))
    active=[x for x in members if x['status'] in ('Open','Acknowledged')]
    severity=max((x['severity'] for x in members),key=lambda x:SEVERITY_RANK.get(x,0),default='Warning')
    return members,active,severity


def _refresh_incident(conn, incident_id:int, actor_id:Optional[int]=None):
    incident=conn.execute('SELECT * FROM alarm_incidents WHERE id=?',(incident_id,)).fetchone()
    if not incident:return None
    incident=dict(incident)
    members,active,severity=_incident_member_summary(conn,incident_id)
    last=max((x['last_seen_at'] for x in members),default=incident['last_seen_at'])
    root=_incident_root_cause(conn,members)
    title=incident['title'];key=incident['correlation_key']
    if root['asset_id']:
        if root['mode']=='Topology':
            title=f"{root['asset_no']} topology-correlated operational incident";key=f"topology:{root['asset_id']}"
        else:
            title=f"{root['asset_no']} operational alarm incident";key=f"asset:{root['asset_id']}"
    updates={'severity':severity,'alarm_count':len(members),'last_seen_at':last,'root_cause_asset_id':root['asset_id'],
             'correlation_mode':root['mode'],'root_cause_score':root['score'],'root_cause_reason':root['reason'],'topology_hops':root['hops'],
             'title':title,'correlation_key':key,'updated_at':now()}
    if not active and incident['status'] in ('Open','Acknowledged'):
        updates['status']='Resolved';updates['resolved_at']=now();updates['resolved_by']=actor_id
    conn.execute('UPDATE alarm_incidents SET '+','.join(f'{k}=?' for k in updates)+' WHERE id=?',(*updates.values(),incident_id))
    updated=one(conn.execute('SELECT * FROM alarm_incidents WHERE id=?',(incident_id,)))
    if root['mode']=='Topology' and incident.get('root_cause_asset_id') not in (None,root['asset_id']):
        emit_event(conn,'operations.incident.root_cause_updated','alarm_incident',incident['incident_no'],{'incident_no':incident['incident_no'],'root_cause_asset_id':root['asset_id'],'score':root['score'],'reason':root['reason']})
        if actor_id:audit(conn,actor_id,'UPDATE INCIDENT ROOT CAUSE','Utilities Operations',incident['incident_no'],incident.get('root_cause_asset_id'),root)
    if incident['status'] in ('Open','Acknowledged') and updated and updated['status']=='Resolved':
        emit_event(conn,'operations.incident.auto_resolved','alarm_incident',incident['incident_no'],{'incident_no':incident['incident_no'],'alarm_count':len(members)})
        if actor_id:audit(conn,actor_id,'AUTO RESOLVE INCIDENT','Utilities Operations',incident['incident_no'],incident['status'],'Resolved')
    return updated


def _correlate_alarm(conn, alarm_id:int, actor_id:Optional[int]=None):
    alarm=get_or_404(conn,"""SELECT oa.*,a.asset_no,a.name asset_name FROM operational_alarms oa
      JOIN assets a ON a.id=oa.asset_id WHERE oa.id=?""",(alarm_id,),'Alarm not found')
    existing=conn.execute('SELECT incident_id FROM alarm_incident_members WHERE alarm_id=? ORDER BY incident_id DESC LIMIT 1',(alarm_id,)).fetchone()
    if existing:return _refresh_incident(conn,existing['incident_id'],actor_id)
    cutoff=(_dt(alarm['last_seen_at'])-timedelta(minutes=30)).isoformat(timespec='seconds')
    sql="SELECT * FROM alarm_incidents WHERE status IN ('Open','Acknowledged') AND last_seen_at>=?";args=[cutoff]
    if alarm.get('site_id') is None:sql+=' AND site_id IS NULL'
    else:sql+=' AND site_id=?';args.append(alarm['site_id'])
    candidates=[]
    for inc in rows(conn.execute(sql,args)):
        dist=_incident_candidate_distance(conn,inc['id'],alarm['asset_id'],1)
        if dist is not None:candidates.append((dist,-_dt(inc['last_seen_at']).timestamp(),inc))
    candidates.sort(key=lambda x:(x[0],x[1],x[2]['id']))
    incident=candidates[0][2] if candidates else None
    if not incident:
        no=next_no(conn,'alarm_incidents','incident_no','INC-',60001)
        cur=conn.execute("""INSERT INTO alarm_incidents(incident_no,correlation_key,site_id,asset_id,title,severity,status,opened_at,last_seen_at,alarm_count,root_cause_asset_id,correlation_mode,root_cause_score,root_cause_reason,topology_hops,created_at,updated_at)
          VALUES(?,?,?,?,?,?,'Open',?,?,0,?,'Asset',100,?,0,?,?)""",
          (no,f"asset:{alarm['asset_id']}",alarm.get('site_id'),alarm['asset_id'],f"{alarm['asset_no']} operational alarm incident",alarm['severity'],alarm['opened_at'],alarm['last_seen_at'],alarm['asset_id'],f"Initial alarm evidence originates from {alarm['asset_no']}.",now(),now()))
        incident_id=cur.lastrowid
        emit_event(conn,'operations.incident.opened','alarm_incident',no,{'incident_no':no,'asset_id':alarm['asset_id'],'alarm_no':alarm['alarm_no'],'severity':alarm['severity']})
        notify_once(conn,'Operational incident',f"{no} — {alarm['asset_no']} correlated alarm incident",alarm['severity'],None,'maintenance_manager','commandcenter',no)
        if actor_id:audit(conn,actor_id,'INCIDENT OPEN','Utilities Operations',no,'',{'alarm':alarm['alarm_no'],'asset':alarm['asset_no']})
    else:
        incident_id=incident['id'];no=incident['incident_no']
    conn.execute('INSERT OR IGNORE INTO alarm_incident_members(incident_id,alarm_id,added_at) VALUES(?,?,?)',(incident_id,alarm_id,now()))
    updated=_refresh_incident(conn,incident_id,actor_id)
    if updated and updated.get('correlation_mode')=='Topology':
        emit_event(conn,'operations.incident.topology_correlated','alarm_incident',no,{'incident_no':no,'alarm_no':alarm['alarm_no'],'asset_id':alarm['asset_id'],'root_cause_asset_id':updated.get('root_cause_asset_id'),'score':updated.get('root_cause_score')})
    return updated


def _refresh_incidents_for_alarm(conn, alarm_id:int, actor_id:Optional[int]=None):
    result=[]
    for r in conn.execute('SELECT incident_id FROM alarm_incident_members WHERE alarm_id=?',(alarm_id,)).fetchall():
        updated=_refresh_incident(conn,r['incident_id'],actor_id)
        if updated:result.append(updated)
    return result


def _telemetry_quality_summary(conn, hours:int=24, site_id:Optional[int]=None):
    cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
    sql="""SELECT tr.quality,COUNT(*) count FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id
      JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id
      WHERE tr.captured_at>=?""";args=[cutoff]
    if site_id is not None:sql+=' AND s.id=?';args.append(site_id)
    sql+=' GROUP BY tr.quality';counts={r['quality']:int(r['count']) for r in conn.execute(sql,args).fetchall()}
    total=sum(counts.values());good=counts.get('Good',0);bad=counts.get('Bad',0);uncertain=counts.get('Uncertain',0)
    return {'hours':hours,'total_readings':total,'good':good,'uncertain':uncertain,'bad':bad,
            'good_percent':round(good/max(total,1)*100,1),'bad_percent':round(bad/max(total,1)*100,1)}


def _telemetry_series(conn, channel_id:int, hours:int, bucket_minutes:int):
    cutoff=datetime.now()-timedelta(hours=hours)
    data=rows(conn.execute('SELECT value,quality,captured_at FROM telemetry_readings WHERE channel_id=? AND captured_at>=? ORDER BY captured_at',(channel_id,cutoff.isoformat(timespec='seconds'))))
    buckets={}
    span=max(1,bucket_minutes)*60
    for r in data:
        try:dt=_dt(r['captured_at'])
        except Exception:continue
        epoch=int(dt.timestamp());bucket_epoch=epoch-(epoch%span);key=datetime.fromtimestamp(bucket_epoch).isoformat(timespec='seconds')
        b=buckets.setdefault(key,{'timestamp':key,'values':[],'good':0,'uncertain':0,'bad':0})
        b['values'].append(float(r['value']));q=str(r['quality']).lower();b[q]=b.get(q,0)+1
    points=[]
    for key in sorted(buckets):
        b=buckets[key];vals=b.pop('values');points.append(b|{'min':min(vals),'max':max(vals),'avg':round(sum(vals)/len(vals),4),'count':len(vals)})
    return points


def _fmea_risk(severity:int, occurrence:int, detectability:int):
    vals=(int(severity),int(occurrence),int(detectability))
    if any(v<1 or v>10 for v in vals):raise HTTPException(422,'FMEA severity, occurrence and detectability must be between 1 and 10')
    rpn=vals[0]*vals[1]*vals[2]
    band='Critical' if rpn>=300 else 'High' if rpn>=160 else 'Medium' if rpn>=80 else 'Low'
    return rpn,band

def _fmea_record(conn, asset_fmea_id:int, expected_asset_id:Optional[int]=None, active_required:bool=True):
    rec=one(conn.execute("""SELECT f.*,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,fm.category failure_mode_category
      FROM asset_fmea f JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id WHERE f.id=?""",(asset_fmea_id,)))
    if not rec:raise HTTPException(404,'Asset FMEA record not found')
    if expected_asset_id is not None and int(rec['asset_id'])!=int(expected_asset_id):raise HTTPException(422,'FMEA record must belong to the same asset')
    if active_required and rec['status']=='Retired':raise HTTPException(409,'Retired FMEA records cannot be linked to new work or CBM rules')
    return rec

def _failure_mode_cycle(conn, mode_id:int, parent_id:Optional[int]):
    if parent_id is None:return False
    if int(parent_id)==int(mode_id):return True
    seen=set();cur=parent_id
    while cur is not None and cur not in seen:
        if int(cur)==int(mode_id):return True
        seen.add(cur);row=conn.execute('SELECT parent_id FROM failure_modes WHERE id=?',(cur,)).fetchone();cur=row['parent_id'] if row else None
    return False

RCM_CONSEQUENCES=('Safety','Environmental','Operational','Non-Operational','Hidden')
RCM_STRATEGY_TYPES=('Condition-Based','Time-Based','Run-to-Failure','Failure-Finding','Redesign')

def _rcm_review_days(risk_band:str):
    return {'Critical':90,'High':180,'Medium':365,'Low':730}.get(str(risk_band),365)

def _rcm_default_review_due(fmea:dict):
    return (date.today()+timedelta(days=_rcm_review_days(fmea.get('risk_band') or 'Medium'))).isoformat()

def _rcm_strategy_record(conn, strategy_id:int):
    rec=one(conn.execute("""SELECT r.*,f.fmea_no,f.asset_id,f.rpn,f.risk_band,f.status fmea_status,a.asset_no,a.name asset_name,
      fm.mode_no,fm.name failure_mode_name,ou.full_name owner_name,ap.full_name approved_by_name,ac.full_name activated_by_name,
      cb.rule_no linked_cbm_rule_no,cb.name linked_cbm_rule_name,pm.pm_no linked_pm_no,pm.name linked_pm_name
      FROM rcm_strategies r JOIN asset_fmea f ON f.id=r.asset_fmea_id JOIN assets a ON a.id=f.asset_id
      JOIN failure_modes fm ON fm.id=f.failure_mode_id LEFT JOIN users ou ON ou.id=r.owner_id LEFT JOIN users ap ON ap.id=r.approved_by
      LEFT JOIN users ac ON ac.id=r.activated_by LEFT JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id
      LEFT JOIN maintenance_plans pm ON pm.id=r.linked_pm_plan_id WHERE r.id=?""",(strategy_id,)))
    if not rec:raise HTTPException(404,'RCM strategy not found')
    return rec

def _validate_rcm_payload(conn, fmea:dict, data:dict, require_ready:bool=False):
    consequence=str(data.get('consequence_classification') or '')
    strategy=str(data.get('strategy_type') or '')
    if consequence not in RCM_CONSEQUENCES:raise HTTPException(422,'Invalid RCM consequence classification')
    if strategy not in RCM_STRATEGY_TYPES:raise HTTPException(422,'Invalid RCM maintenance strategy type')
    if fmea.get('status')=='Retired':raise HTTPException(409,'Retired FMEA records cannot have new or submitted RCM strategies')
    interval=data.get('interval_days')
    if strategy in ('Time-Based','Failure-Finding') and not interval:raise HTTPException(422,f'{strategy} strategies require interval_days')
    if interval is not None and (int(interval)<1 or int(interval)>3650):raise HTTPException(422,'RCM interval_days must be between 1 and 3650')
    if strategy=='Run-to-Failure' and consequence in ('Safety','Environmental'):
        raise HTTPException(422,'Run-to-Failure is not permitted for Safety or Environmental consequence classifications')
    cbm_id=data.get('linked_cbm_rule_id')
    if cbm_id:
        cb=one(conn.execute('SELECT id,asset_fmea_id,active FROM cbm_rules WHERE id=?',(cbm_id,)))
        if not cb:raise HTTPException(404,'Linked CBM rule not found')
        if int(cb.get('asset_fmea_id') or 0)!=int(fmea['id']):raise HTTPException(422,'Linked CBM rule must reference the same FMEA record')
        if require_ready and not cb.get('active'):raise HTTPException(409,'Linked CBM rule must be active before RCM submission or activation')
    if require_ready and strategy=='Condition-Based' and not cbm_id:
        raise HTTPException(422,'Condition-Based RCM strategies require a linked active CBM rule before submission')
    pm_id=data.get('linked_pm_plan_id')
    if pm_id:
        pm=one(conn.execute('SELECT id,asset_id,active FROM maintenance_plans WHERE id=?',(pm_id,)))
        if not pm:raise HTTPException(404,'Linked maintenance plan not found')
        if int(pm['asset_id'])!=int(fmea['asset_id']):raise HTTPException(422,'Linked maintenance plan must belong to the same asset')
        if require_ready and not pm.get('active'):raise HTTPException(409,'Linked maintenance plan must be active before RCM submission or activation')
    if require_ready and strategy=='Time-Based' and not pm_id:
        raise HTTPException(422,'Time-Based RCM strategies require a linked active maintenance plan before submission')
    return True

def _cbm_condition(rule:dict, value:float):
    op=str(rule.get('operator') or '').strip()
    low=rule.get('threshold_low'); high=rule.get('threshold_high')
    low=float(low) if low is not None else None; high=float(high) if high is not None else None
    if op=='>=': return low is not None and value>=low
    if op=='>': return low is not None and value>low
    if op=='<=': return low is not None and value<=low
    if op=='<': return low is not None and value<low
    if op=='between': return low is not None and high is not None and low<=value<=high
    if op=='outside': return low is not None and high is not None and (value<low or value>high)
    return False

def _cbm_rule_threshold_text(rule:dict):
    op=rule.get('operator');low=rule.get('threshold_low');high=rule.get('threshold_high')
    if op in ('>=','>','<=','<'): return f"{op} {low:g}" if low is not None else op
    if op=='between': return f"between {low:g} and {high:g}"
    if op=='outside': return f"outside {low:g} to {high:g}"
    return str(op or '')

def _create_cbm_work_order(conn, rule:dict, channel:dict, event_no:str, value:float, actor_id:int):
    asset=get_or_404(conn,'SELECT id,asset_no,name,location_id FROM assets WHERE id=?',(channel['asset_id'],),'CBM asset not found')
    fmea=_fmea_record(conn,rule['asset_fmea_id'],asset['id']) if rule.get('asset_fmea_id') else None
    no=next_no(conn,'work_orders','wo_no','WO-',10026)
    priority=rule.get('work_priority') or ('Critical' if rule.get('severity')=='Critical' else 'High')
    finish_days=1 if priority in ('Emergency','Critical') else 2 if priority=='High' else 5
    title=f"CBM: {rule['name']} — {asset['asset_no']}"
    fmea_text=f" Linked FMEA {fmea['fmea_no']} / {fmea['mode_no']} ({fmea['failure_mode_name']}), current RPN {fmea['rpn']} {fmea['risk_band']}." if fmea else ''
    desc=f"Generated by {rule['rule_no']} after {int(rule['consecutive_readings'])} consecutive Good-quality reading(s). Channel {channel['channel_code']} measured {value:g} {channel.get('unit') or ''}; rule condition {_cbm_rule_threshold_text(rule)}. Source event {event_no}.{fmea_text}"
    instructions=(rule.get('instructions') or (fmea.get('recommended_action') if fmea else '') or 'Inspect the asset condition, validate the signal and perform condition-based maintenance as required.').strip()
    failure_code=fmea['mode_no'] if fmea else f"CBM-{rule['rule_no']}"
    cur=conn.execute("""INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,asset_fmea_id,requested_by,target_start,target_finish,estimated_hours,instructions,created_at,updated_at)
      VALUES(?,?,?,?,?,?,'Submitted','Condition-Based Maintenance',?,?,?,?,?,?,?,?,?)""",
      (no,title,desc,asset['id'],asset.get('location_id'),priority,failure_code,fmea['id'] if fmea else None,actor_id,date.today().isoformat(),(date.today()+timedelta(days=finish_days)).isoformat(),2.0,instructions,now(),now()))
    _ensure_work_sla(conn,cur.lastrowid)
    create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f"Approve {no} — {title}",actor_id,assigned_role='maintenance_manager')
    workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'CBM GENERATED','', 'Submitted',actor_id,event_no)
    emit_event(conn,'maintenance.cbm.work_order_created','cbm_event',event_no,{'event_no':event_no,'rule_no':rule['rule_no'],'work_order':no,'asset_id':asset['id'],'fmea_no':fmea['fmea_no'] if fmea else None})
    return {'id':cur.lastrowid,'wo_no':no}

def _evaluate_cbm_rules(conn, channel:dict, value:float, captured_at:str, reading_id:int, actor_id:int):
    results=[]
    rules=rows(conn.execute('SELECT * FROM cbm_rules WHERE channel_id=? AND active=1 ORDER BY id',(channel['id'],)))
    for rule in rules:
        state=one(conn.execute('SELECT * FROM cbm_rule_state WHERE rule_id=?',(rule['id'],))) or {'consecutive_hits':0,'last_triggered_at':None,'active_event_id':None}
        breached=_cbm_condition(rule,value);hits=int(state.get('consecutive_hits') or 0)+1 if breached else 0
        active=one(conn.execute("SELECT * FROM cbm_events WHERE rule_id=? AND status IN ('Open','Acknowledged') ORDER BY id DESC LIMIT 1",(rule['id'],)))
        if not breached:
            resolved_no=None
            if active:
                reason=f"Condition cleared by Good-quality telemetry reading {value:g} {channel.get('unit') or ''}".strip()
                conn.execute("UPDATE cbm_events SET status='Resolved',resolved_at=?,resolution_reason=?,last_seen_at=? WHERE id=?",(captured_at,reason,captured_at,active['id']))
                emit_event(conn,'maintenance.cbm.event_resolved','cbm_event',active['event_no'],{'event_no':active['event_no'],'rule_no':rule['rule_no'],'value':value,'captured_at':captured_at})
                if actor_id:audit(conn,actor_id,'CBM AUTO RESOLVE','Condition-Based Maintenance',active['event_no'],active['status'],'Resolved')
                resolved_no=active['event_no']
            conn.execute("""INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
              VALUES(?,0,?,'Good',?,?,NULL) ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=0,last_value=excluded.last_value,last_quality='Good',last_evaluated_at=excluded.last_evaluated_at,active_event_id=NULL""",
              (rule['id'],value,captured_at,state.get('last_triggered_at')))
            results.append({'rule_no':rule['rule_no'],'action':'resolved' if resolved_no else 'normal','event_no':resolved_no,'work_order':None})
            continue
        if active:
            conn.execute('UPDATE cbm_events SET trigger_value=?,last_seen_at=?,occurrence_count=occurrence_count+1 WHERE id=?',(value,captured_at,active['id']))
            conn.execute("""INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
              VALUES(?,? ,?,'Good',?,?,?) ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,last_quality='Good',last_evaluated_at=excluded.last_evaluated_at,active_event_id=excluded.active_event_id""",
              (rule['id'],hits,value,captured_at,state.get('last_triggered_at'),active['id']))
            results.append({'rule_no':rule['rule_no'],'action':'active','event_no':active['event_no'],'work_order':None})
            continue
        required=max(1,int(rule.get('consecutive_readings') or 1))
        cooldown=False
        if state.get('last_triggered_at'):
            try: cooldown=_dt(captured_at) < _dt(state['last_triggered_at'])+timedelta(minutes=max(0,int(rule.get('cooldown_minutes') or 0)))
            except Exception: cooldown=False
        if hits<required or cooldown:
            conn.execute("""INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
              VALUES(?,? ,?,'Good',?,?,NULL) ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,last_quality='Good',last_evaluated_at=excluded.last_evaluated_at""",
              (rule['id'],hits,value,captured_at,state.get('last_triggered_at')))
            results.append({'rule_no':rule['rule_no'],'action':'cooldown' if cooldown else 'pending','event_no':None,'work_order':None,'hits':hits,'required':required})
            continue
        event_no=next_no(conn,'cbm_events','event_no','CBM-',80001)
        message=f"{rule['name']}: {channel['channel_code']} value {value:g} {channel.get('unit') or ''} matched {_cbm_rule_threshold_text(rule)} after {hits} consecutive Good reading(s).".strip()
        cur=conn.execute("""INSERT INTO cbm_events(event_no,rule_id,channel_id,asset_id,reading_id,severity,status,trigger_value,message,asset_fmea_id,opened_at,last_seen_at,occurrence_count)
          VALUES(?,?,?,?,?,?,'Open',?,?,?,?,?,1)""",(event_no,rule['id'],channel['id'],channel['asset_id'],reading_id,rule['severity'],value,message,rule.get('asset_fmea_id'),captured_at,captured_at))
        work=None
        if rule['action_type']=='WorkOrder':
            work=_create_cbm_work_order(conn,rule,channel,event_no,value,actor_id)
            conn.execute('UPDATE cbm_events SET work_order_id=? WHERE id=?',(work['id'],cur.lastrowid))
        notify_once(conn,'Condition-based maintenance trigger',f"{event_no} — {message}",rule['severity'],None,'maintenance_manager','telemetry',event_no)
        emit_event(conn,'maintenance.cbm.event_opened','cbm_event',event_no,{'event_no':event_no,'rule_no':rule['rule_no'],'channel_code':channel['channel_code'],'asset_id':channel['asset_id'],'value':value,'action_type':rule['action_type'],'work_order':work['wo_no'] if work else None})
        if actor_id:audit(conn,actor_id,'CBM TRIGGER','Condition-Based Maintenance',event_no,'',{'rule':rule['rule_no'],'value':value,'work_order':work['wo_no'] if work else None})
        conn.execute("""INSERT INTO cbm_rule_state(rule_id,consecutive_hits,last_value,last_quality,last_evaluated_at,last_triggered_at,active_event_id)
          VALUES(?,? ,?,'Good',?,?,?) ON CONFLICT(rule_id) DO UPDATE SET consecutive_hits=excluded.consecutive_hits,last_value=excluded.last_value,last_quality='Good',last_evaluated_at=excluded.last_evaluated_at,last_triggered_at=excluded.last_triggered_at,active_event_id=excluded.active_event_id""",
          (rule['id'],hits,value,captured_at,captured_at,cur.lastrowid))
        results.append({'rule_no':rule['rule_no'],'action':'opened','event_no':event_no,'work_order':work['wo_no'] if work else None})
    return results

def _telemetry_alarm_level(channel, value:float):
    checks=[('Critical','critical_high','high'),('Critical','critical_low','low'),('Warning','warning_high','high'),('Warning','warning_low','low')]
    for severity,key,direction in checks:
        threshold=channel.get(key)
        if threshold is None: continue
        threshold=float(threshold)
        if direction=='high' and value>=threshold:return severity,threshold
        if direction=='low' and value<=threshold:return severity,threshold
    return None,None

def _evaluate_telemetry_alarm(conn, channel:dict, value:float, captured_at:str, actor_id:Optional[int]):
    severity,threshold=_telemetry_alarm_level(channel,value)
    active=conn.execute("SELECT * FROM operational_alarms WHERE channel_id=? AND status IN ('Open','Acknowledged') ORDER BY id DESC LIMIT 1",(channel['id'],)).fetchone()
    site=_channel_site(conn,channel['asset_id']); unit=channel.get('unit') or ''
    if severity:
        suppression=_active_alarm_suppression(conn,channel,captured_at)
        if suppression:
            return {'action':'suppressed','alarm_id':None,'alarm_no':None,'severity':severity,'threshold':threshold,
                    'suppression_id':suppression['id'],'suppression_no':suppression['suppression_no'],'suppression_reason':suppression['reason']}
        message=f"{channel['name']} {severity.lower()}: {value:g} {unit}".strip()
        if active:
            conn.execute('UPDATE operational_alarms SET severity=?,message=?,trigger_value=?,threshold_value=?,last_seen_at=?,occurrence_count=occurrence_count+1 WHERE id=?',(severity,message,value,threshold,captured_at,active['id']))
            incident=_correlate_alarm(conn,active['id'],actor_id)
            return {'action':'updated','alarm_id':active['id'],'alarm_no':active['alarm_no'],'severity':severity,
                    'incident_id':incident['id'] if incident else None,'incident_no':incident['incident_no'] if incident else None}
        no=next_no(conn,'operational_alarms','alarm_no','ALM-',50001)
        cur=conn.execute("INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)",(no,channel['id'],channel['asset_id'],site.get('site_id'),severity,message,value,threshold,captured_at,captured_at))
        notify_once(conn,'Operational alarm',f"{no} — {message}",severity,None,'maintenance_manager','operations',no)
        notify_once(conn,'Operational alarm',f"{no} — {message}",severity,None,'asset_manager','operations',no)
        emit_event(conn,'operations.alarm.opened','alarm',no,{'alarm_no':no,'channel_code':channel['channel_code'],'asset_id':channel['asset_id'],'severity':severity,'value':value,'threshold':threshold,'captured_at':captured_at})
        if actor_id:audit(conn,actor_id,'ALARM OPEN','Utilities Operations',no,'',{'channel':channel['channel_code'],'severity':severity,'value':value,'threshold':threshold})
        incident=_correlate_alarm(conn,cur.lastrowid,actor_id)
        return {'action':'opened','alarm_id':cur.lastrowid,'alarm_no':no,'severity':severity,
                'incident_id':incident['id'] if incident else None,'incident_no':incident['incident_no'] if incident else None}
    if active:
        conn.execute("UPDATE operational_alarms SET status='Cleared',cleared_at=?,last_seen_at=?,trigger_value=? WHERE id=?",(captured_at,captured_at,value,active['id']))
        emit_event(conn,'operations.alarm.cleared','alarm',active['alarm_no'],{'alarm_no':active['alarm_no'],'channel_code':channel['channel_code'],'asset_id':channel['asset_id'],'value':value,'captured_at':captured_at})
        if actor_id:audit(conn,actor_id,'ALARM CLEAR','Utilities Operations',active['alarm_no'],active['status'],'Cleared')
        incidents=_refresh_incidents_for_alarm(conn,active['id'],actor_id)
        return {'action':'cleared','alarm_id':active['id'],'alarm_no':active['alarm_no'],'severity':active['severity'],
                'incidents_resolved':[x['incident_no'] for x in incidents if x.get('status')=='Resolved']}
    return {'action':'normal','alarm_id':None,'alarm_no':None,'severity':None}

def _operations_intelligence(conn, site_id:Optional[int]=None):
    args=[];site_clause=''
    if site_id is not None:site_clause=' AND oa.site_id=?';args.append(site_id)
    active_alarms=int(conn.execute("SELECT COUNT(*) FROM operational_alarms oa WHERE oa.status IN ('Open','Acknowledged')"+site_clause,args).fetchone()[0])
    critical=int(conn.execute("SELECT COUNT(*) FROM operational_alarms oa WHERE oa.status IN ('Open','Acknowledged') AND oa.severity='Critical'"+site_clause,args).fetchone()[0])
    ch_args=[];ch_clause=''
    if site_id is not None:ch_clause=' AND s.id=?';ch_args.append(site_id)
    channels=int(conn.execute('SELECT COUNT(*) FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE tc.active=1'+ch_clause,ch_args).fetchone()[0])
    stale_cut=(datetime.now()-timedelta(hours=24)).isoformat(timespec='seconds')
    stale=int(conn.execute('SELECT COUNT(*) FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE tc.active=1 AND (tc.last_reading_at IS NULL OR tc.last_reading_at<?)'+ch_clause,[stale_cut]+ch_args).fetchone()[0])
    inc_sql="SELECT COUNT(*) FROM alarm_incidents WHERE status IN ('Open','Acknowledged')";inc_args=[]
    if site_id is not None:inc_sql+=' AND site_id=?';inc_args.append(site_id)
    incidents=int(conn.execute(inc_sql,inc_args).fetchone()[0])
    sup_sql="SELECT COUNT(*) FROM alarm_suppressions WHERE active=1 AND start_at<=? AND end_at>=?";sup_args=[now(),now()]
    if site_id is not None:sup_sql+=' AND (site_id=? OR site_id IS NULL)';sup_args.append(site_id)
    suppressions=int(conn.execute(sup_sql,sup_args).fetchone()[0])
    cbm_rule_sql="SELECT COUNT(*) FROM cbm_rules r JOIN telemetry_channels tc ON tc.id=r.channel_id JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE r.active=1";cbm_rule_args=[]
    cbm_event_sql="SELECT COUNT(*) FROM cbm_events e JOIN assets a ON a.id=e.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE e.status IN ('Open','Acknowledged')";cbm_event_args=[]
    if site_id is not None:cbm_rule_sql+=' AND s.id=?';cbm_rule_args.append(site_id);cbm_event_sql+=' AND s.id=?';cbm_event_args.append(site_id)
    cbm_rules_active=int(conn.execute(cbm_rule_sql,cbm_rule_args).fetchone()[0]);cbm_events_open=int(conn.execute(cbm_event_sql,cbm_event_args).fetchone()[0])
    quality=_telemetry_quality_summary(conn,24,site_id)
    return {'active_alarms':active_alarms,'critical_alarms':critical,'telemetry_channels':channels,'stale_channels_24h':stale,
            'open_incidents':incidents,'active_suppressions':suppressions,'active_cbm_rules':cbm_rules_active,'open_cbm_events':cbm_events_open,'data_quality':quality}

def _ensure_work_sla(conn,work_order_id:int,force:bool=False):
    w=conn.execute('SELECT id,priority,status,created_at,actual_start,actual_finish FROM work_orders WHERE id=?',(work_order_id,)).fetchone()
    if not w:return None
    existing=conn.execute('SELECT * FROM work_order_sla WHERE work_order_id=?',(work_order_id,)).fetchone()
    if existing and not force:return dict(existing)
    policy=conn.execute('SELECT * FROM sla_policies WHERE priority=? AND active=1',(w['priority'],)).fetchone() or conn.execute("SELECT * FROM sla_policies WHERE priority='Medium' AND active=1").fetchone()
    if not policy:return None
    created=_dt(w['created_at']); response_due=created+timedelta(minutes=policy['response_minutes']); resolution_due=created+timedelta(minutes=policy['resolution_minutes'])
    first=w['actual_start'];resolved=w['actual_finish']
    response_status='Pending' if not first else ('Met' if _dt(first)<=response_due else 'Breached')
    resolution_status='Pending' if not resolved else ('Met' if _dt(resolved)<=resolution_due else 'Breached')
    if existing:
        conn.execute('UPDATE work_order_sla SET policy_id=?,response_due=?,resolution_due=?,first_response_at=?,resolved_at=?,response_status=?,resolution_status=?,updated_at=? WHERE work_order_id=?',(policy['id'],response_due.isoformat(timespec='seconds'),resolution_due.isoformat(timespec='seconds'),first,resolved,response_status,resolution_status,now(),work_order_id))
    else:
        conn.execute('INSERT INTO work_order_sla(work_order_id,policy_id,response_due,resolution_due,first_response_at,resolved_at,response_status,resolution_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(work_order_id,policy['id'],response_due.isoformat(timespec='seconds'),resolution_due.isoformat(timespec='seconds'),first,resolved,response_status,resolution_status,now()))
    return dict(conn.execute('SELECT * FROM work_order_sla WHERE work_order_id=?',(work_order_id,)).fetchone())

def _backfill_work_order_slas(conn):
    for w in rows(conn.execute('SELECT id FROM work_orders WHERE id NOT IN (SELECT work_order_id FROM work_order_sla)')):
        _ensure_work_sla(conn,w['id'])

def _mark_sla_response(conn,work_order_id:int,at_value:str):
    sla=_ensure_work_sla(conn,work_order_id)
    if not sla:return
    status='Met' if _dt(at_value)<=_dt(sla['response_due']) else 'Breached'
    conn.execute('UPDATE work_order_sla SET first_response_at=?,response_status=?,updated_at=? WHERE work_order_id=?',(at_value,status,now(),work_order_id))

def _mark_sla_resolution(conn,work_order_id:int,at_value:str):
    sla=_ensure_work_sla(conn,work_order_id)
    if not sla:return
    status='Met' if _dt(at_value)<=_dt(sla['resolution_due']) else 'Breached'
    conn.execute('UPDATE work_order_sla SET resolved_at=?,resolution_status=?,updated_at=? WHERE work_order_id=?',(at_value,status,now(),work_order_id))

def _run_sla_scan(conn,actor_id:int,target:date):
    _backfill_work_order_slas(conn); cutoff=datetime.now() if target==date.today() else datetime.combine(target,datetime.max.time()).replace(microsecond=0)
    response_breaches=0;resolution_breaches=0
    active=rows(conn.execute("""SELECT w.id,w.wo_no,w.title,w.status,w.assigned_to,w.supervisor_id,s.response_due,s.resolution_due,s.response_status,s.resolution_status,s.escalated_level
      FROM work_orders w JOIN work_order_sla s ON s.work_order_id=w.id WHERE w.status NOT IN ('Completed','Closed','Cancelled')"""))
    for w in active:
        if w['response_status']=='Pending' and _dt(w['response_due'])<cutoff and w['status'] not in ('In Progress','Completed','Closed'):
            cur=conn.execute("INSERT OR IGNORE INTO sla_events(work_order_id,event_type,level,message,created_at) VALUES(?,'Response Breach',1,?,?)",(w['id'],f"{w['wo_no']} exceeded response SLA",now()))
            if cur.rowcount:
                response_breaches+=1
                recipients={x for x in (w['assigned_to'],w['supervisor_id']) if x}
                for uid in recipients: notify_once(conn,'SLA response breach',f"{w['wo_no']} — {w['title']} exceeded response target",'Critical',uid,None,'work',w['wo_no']+':sla-response')
                notify_once(conn,'SLA response breach',f"{w['wo_no']} exceeded response target",'Critical',None,'maintenance_manager','work',w['wo_no']+':sla-response')
                emit_event(conn,'sla.response_breached','work_order',w['wo_no'],{'work_order_id':w['id'],'due_at':w['response_due']})
            conn.execute("UPDATE work_order_sla SET response_status='Breached',escalated_level=CASE WHEN escalated_level<1 THEN 1 ELSE escalated_level END,updated_at=? WHERE work_order_id=?",(now(),w['id']))
        if w['resolution_status']=='Pending' and _dt(w['resolution_due'])<cutoff:
            cur=conn.execute("INSERT OR IGNORE INTO sla_events(work_order_id,event_type,level,message,created_at) VALUES(?,'Resolution Breach',1,?,?)",(w['id'],f"{w['wo_no']} exceeded resolution SLA",now()))
            if cur.rowcount:
                resolution_breaches+=1
                recipients={x for x in (w['assigned_to'],w['supervisor_id']) if x}
                for uid in recipients: notify_once(conn,'SLA resolution breach',f"{w['wo_no']} — {w['title']} exceeded resolution target",'Critical',uid,None,'work',w['wo_no']+':sla-resolution')
                notify_once(conn,'SLA resolution breach',f"{w['wo_no']} exceeded resolution target",'Critical',None,'maintenance_manager','work',w['wo_no']+':sla-resolution')
                emit_event(conn,'sla.resolution_breached','work_order',w['wo_no'],{'work_order_id':w['id'],'due_at':w['resolution_due']})
            conn.execute("UPDATE work_order_sla SET resolution_status='Breached',escalated_level=CASE WHEN escalated_level<1 THEN 1 ELSE escalated_level END,updated_at=? WHERE work_order_id=?",(now(),w['id']))
    return {'response_breaches':response_breaches,'resolution_breaches':resolution_breaches}

def _process_outbox(conn):
    return process_outbox(
        conn,
        webhook_url=EVENT_WEBHOOK_URL,
        webhook_secret=EVENT_WEBHOOK_SECRET,
        max_attempts=OUTBOX_MAX_ATTEMPTS,
        app_version=APP_VERSION,
        urlopen=urllib_request.urlopen,
        request_factory=urllib_request.Request,
    )

def notify_once(conn,title,message,severity='Info',user_id=None,role_code=None,module='',record_id=''):
    existing=conn.execute('''SELECT id FROM notifications WHERE title=? AND link_module=? AND link_id=? AND is_read=0
      AND ((user_id=? ) OR (user_id IS NULL AND ? IS NULL))
      AND ((role_code=? ) OR (role_code IS NULL AND ? IS NULL)) LIMIT 1''',(title,module,record_id,user_id,user_id,role_code,role_code)).fetchone()
    if existing:return False
    notify(conn,title,message,severity,user_id,role_code,module,record_id);return True

def csv_response(filename, headers, data_rows):
    buf=io.StringIO();w=csv.writer(buf);w.writerow(headers);w.writerows(data_rows)
    return StreamingResponse(iter([buf.getvalue()]),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{filename}"'})

def _generate_due_pm(conn, actor_id:int, target:date):
    generated=[]
    plans=rows(conn.execute('''SELECT p.*,a.asset_no,a.location_id,a.meter_reading,a.condition FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id WHERE p.active=1'''))
    for p in plans:
        due=False
        if p['trigger_type']=='Calendar' and p['next_due'] and date.fromisoformat(p['next_due'])<=target:due=True
        if p['trigger_type'] in ('Meter','Runtime','Usage') and p['meter_interval'] and p['meter_reading']-p['last_meter']>=p['meter_interval']:due=True
        if p['trigger_type']=='Condition':due=p['condition'] in ('Warning','Poor','Critical')
        if not due:continue
        if conn.execute("SELECT id FROM work_orders WHERE pm_plan_id=? AND status NOT IN ('Closed','Cancelled')",(p['id'],)).fetchone():continue
        no=next_no(conn,'work_orders','wo_no','WO-',10026)
        cur=conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,requested_by,target_start,target_finish,instructions,pm_plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?, 'Submitted','Preventive Maintenance',?,?,?,?,?,?,?)''',(no,p['name'],p['job_plan'],p['asset_id'],p['location_id'],p['priority'],actor_id,target.isoformat(),target.isoformat(),p['job_plan'],p['id'],now(),now()))
        _ensure_work_sla(conn,cur.lastrowid)
        create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f'Approve {no} — {p["name"]}',actor_id,assigned_role='supervisor')
        workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'AUTO SUBMIT','', 'Submitted',actor_id,f'Generated from {p["pm_no"]}')
        next_due=p['next_due'];last_meter=p['last_meter']
        if p['trigger_type']=='Calendar' and p['interval_days']:
            d=date.fromisoformat(p['next_due'])
            while d<=target:d+=timedelta(days=p['interval_days'])
            next_due=d.isoformat()
        if p['trigger_type'] in ('Meter','Runtime','Usage'):last_meter=p['meter_reading']
        conn.execute('UPDATE maintenance_plans SET next_due=?,last_meter=?,last_generated=? WHERE id=?',(next_due,last_meter,now(),p['id']))
        audit(conn,actor_id,'GENERATE WO','Preventive Maintenance',p['pm_no'],'',no);generated.append(no)
        notify_once(conn,'Preventive work generated',f'{no} generated from {p["pm_no"]}','Info',None,'planner','maintenance',p['pm_no'])
    return generated

def _run_reorder_scan(conn, actor_id:int):
    created=[]
    low=rows(conn.execute('''SELECT i.*,w.site_id FROM inventory_items i JOIN warehouses w ON w.id=i.warehouse_id WHERE i.current_stock-i.reserved_stock<=i.reorder_point'''))
    for i in low:
        existing=conn.execute("SELECT pr.id FROM purchase_requisitions pr JOIN purchase_requisition_items x ON x.pr_id=pr.id WHERE x.inventory_item_id=? AND pr.status NOT IN ('Received','Cancelled','Rejected') LIMIT 1",(i['id'],)).fetchone()
        if existing:continue
        qty=max(i['max_level']-i['current_stock'],i['reorder_point']-i['current_stock']+1,1);no=next_no(conn,'purchase_requisitions','pr_no','PR-',8001)
        cur=conn.execute('INSERT INTO purchase_requisitions(pr_no,title,requester_id,site_id,status,justification,total_estimate,created_at) VALUES(?,?,?,?,?,?,?,?)',(no,f"Auto-replenishment — {i['item_no']}",actor_id,i['site_id'],'Submitted','Automatically generated because available stock reached reorder point.',qty*i['unit_price'],now()))
        conn.execute('INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,i['id'],i['name'],qty,i['unit_price']))
        create_approval(conn,'Procurement','purchase_requisition',cur.lastrowid,no,f'Approve {no} — Auto-replenishment',actor_id,assigned_role='procurement')
        workflow_event(conn,'Procurement','purchase_requisition',cur.lastrowid,no,'AUTO SUBMIT','', 'Submitted',actor_id,'Automatic reorder')
        audit(conn,actor_id,'AUTO CREATE','Procurement',no,'',{'item':i['item_no'],'qty':qty});created.append(no)
        notify_once(conn,'Purchase requisition created',f'{no} created for {i["item_no"]}','Info',None,'procurement','procurement',no)
    return created

def _execute_automation(conn, actor_id:int, trigger_source='manual', as_of:Optional[str]=None):
    target=date.fromisoformat(as_of) if as_of else date.today();run_no=next_no(conn,'job_runs','run_no','JOB-',1)
    cur=conn.execute("INSERT INTO job_runs(run_no,trigger_source,status,actor_id,as_of,started_at) VALUES(?,?,'Running',?,?,?)",(run_no,trigger_source,actor_id,target.isoformat(),now()));run_id=cur.lastrowid
    conn.execute('SAVEPOINT automation_payload')
    try:
        pm=_generate_due_pm(conn,actor_id,target);reorders=_run_reorder_scan(conn,actor_id);sla=_run_sla_scan(conn,actor_id,target)
        overdue_alerts=0;warranty_alerts=0;contract_alerts=0;approval_alerts=0
        overdue=rows(conn.execute("SELECT * FROM work_orders WHERE target_finish IS NOT NULL AND target_finish<? AND status NOT IN ('Completed','Closed','Cancelled')",(target.isoformat(),)))
        for w in overdue:
            recipients={x for x in (w['assigned_to'],w['supervisor_id']) if x}
            for uid in recipients:
                overdue_alerts += int(notify_once(conn,'Overdue work order',f"{w['wo_no']} — {w['title']} is overdue",'Warning',uid,None,'work',w['wo_no']))
        horizon=(target+timedelta(days=30)).isoformat()
        for a in rows(conn.execute("SELECT asset_no,name,warranty_expiry FROM assets WHERE warranty_expiry IS NOT NULL AND warranty_expiry>=? AND warranty_expiry<=?",(target.isoformat(),horizon))):
            warranty_alerts += int(notify_once(conn,'Asset warranty expiring',f"{a['asset_no']} — {a['name']} warranty expires {a['warranty_expiry']}",'Warning',None,'asset_manager','assets',a['asset_no']))
        for c in rows(conn.execute("SELECT contract_no,title,end_date FROM contracts WHERE status='Active' AND end_date IS NOT NULL AND end_date>=? AND end_date<=?",(target.isoformat(),horizon))):
            contract_alerts += int(notify_once(conn,'Contract expiring',f"{c['contract_no']} — {c['title']} expires {c['end_date']}",'Warning',None,'procurement','contracts',c['contract_no']))
        stale_cutoff=(datetime.combine(target,datetime.min.time())-timedelta(days=2)).isoformat(timespec='seconds')
        for ap in rows(conn.execute("SELECT * FROM approval_requests WHERE status='Pending' AND requested_at<?",(stale_cutoff,))):
            approval_alerts += int(notify_once(conn,'Approval overdue',f"{ap['record_code']} has been waiting for approval",'Warning',ap['assigned_user_id'],ap['assigned_role'],'approvals',ap['approval_no']))
        health_results=[_save_asset_health(conn,x['id'],actor_id) for x in rows(conn.execute('SELECT id FROM assets ORDER BY id'))]
        critical_health=0
        for h in health_results:
            if h['risk_band']=='Critical':
                critical_health+=1
                notify_once(conn,'Critical asset health',f"{h['asset_no']} — {h['name']} health score {h['score']}",'Critical',None,'asset_manager','assets',h['asset_no']+':health')
        expired=conn.execute('UPDATE approval_delegations SET active=0 WHERE active=1 AND end_at<?',(now(),)).rowcount
        expired_suppressions=conn.execute('UPDATE alarm_suppressions SET active=0 WHERE active=1 AND end_at<?',(now(),)).rowcount
        expired_shelves=conn.execute("UPDATE alarm_shelves SET status='Expired' WHERE status='Approved' AND end_at<=?",(now(),)).rowcount
        stale_telemetry_alerts=0;stale_cut=(datetime.now()-timedelta(hours=24)).isoformat(timespec='seconds')
        for ch in rows(conn.execute("SELECT tc.channel_code,tc.name,a.asset_no FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id WHERE tc.active=1 AND (tc.last_reading_at IS NULL OR tc.last_reading_at<?)",(stale_cut,))):
            stale_telemetry_alerts += int(notify_once(conn,'Stale telemetry',f"{ch['channel_code']} — {ch['asset_no']} {ch['name']} has no good recent data",'Warning',None,'maintenance_manager','commandcenter',ch['channel_code']+':stale'))
        correlated=0
        for alarm_row in rows(conn.execute("SELECT oa.id FROM operational_alarms oa WHERE oa.status IN ('Open','Acknowledged') AND NOT EXISTS (SELECT 1 FROM alarm_incident_members m WHERE m.alarm_id=oa.id)")):
            _correlate_alarm(conn,alarm_row['id'],actor_id);correlated+=1
        outbox=_process_outbox(conn)
        health_avg=round(sum(x['score'] for x in health_results)/max(len(health_results),1),1)
        summary={'pm_work_orders':len(pm),'reorder_requisitions':len(reorders),'overdue_alerts':overdue_alerts,'warranty_alerts':warranty_alerts,'contract_alerts':contract_alerts,'approval_alerts':approval_alerts,'sla_response_breaches':sla['response_breaches'],'sla_resolution_breaches':sla['resolution_breaches'],'asset_health_average':health_avg,'critical_health_assets':critical_health,'delegations_expired':expired,'alarm_suppressions_expired':expired_suppressions,'alarm_shelves_expired':expired_shelves,'stale_telemetry_alerts':stale_telemetry_alerts,'alarms_correlated':correlated,'outbox_delivered':outbox['delivered'],'outbox_failed':outbox['failed'],'outbox_dead_lettered':outbox['dead_lettered'],'outbox_skipped':outbox['skipped']}
        conn.execute('RELEASE SAVEPOINT automation_payload')
        conn.execute("UPDATE job_runs SET status='Succeeded',finished_at=?,summary_json=? WHERE id=?",(now(),json.dumps(summary),run_id));audit(conn,actor_id,'RUN','Automation',run_no,'',summary)
        return {'id':run_id,'run_no':run_no,'status':'Succeeded','as_of':target.isoformat(),'summary':summary}
    except Exception as exc:
        conn.execute('ROLLBACK TO SAVEPOINT automation_payload');conn.execute('RELEASE SAVEPOINT automation_payload')
        conn.execute("UPDATE job_runs SET status='Failed',finished_at=?,error_message=? WHERE id=?",(now(),str(exc)[:1000],run_id))
        return {'id':run_id,'run_no':run_no,'status':'Failed','as_of':target.isoformat(),'error':str(exc)}

async def _automation_loop():
    await asyncio.sleep(max(60,AUTOMATION_INTERVAL_MINUTES*60))
    while True:
        try:
            with db() as conn:
                system=conn.execute("SELECT id FROM users WHERE username='system'").fetchone()
                if system:_execute_automation(conn,system['id'],'scheduler')
        except Exception:
            logger.exception('EUAS scheduled automation run failed')
        await asyncio.sleep(max(60,AUTOMATION_INTERVAL_MINUTES*60))

# ---------- request models ----------
class LoginIn(BaseModel): username:str; password:str
class AssetIn(BaseModel):
    asset_no: Optional[str]=None; name:str; description:str=''; asset_type_id:Optional[int]=None; category:str
    manufacturer:str=''; model:str=''; serial_no:str=''; installation_date:Optional[str]=None; commissioning_date:Optional[str]=None
    purchase_cost:float=0; replacement_cost:float=0; current_value:float=0; criticality:str='Medium'; condition:str='Good'; status:str='Operating'
    location_id:Optional[int]=None; parent_asset_id:Optional[int]=None; department:str=''; responsible_user_id:Optional[int]=None; vendor_id:Optional[int]=None
    warranty_expiry:Optional[str]=None; maintenance_strategy:str='Preventive'; last_maintenance:Optional[str]=None; next_maintenance:Optional[str]=None; meter_reading:float=0
class AssetPatch(BaseModel):
    name:Optional[str]=None; description:Optional[str]=None; category:Optional[str]=None; manufacturer:Optional[str]=None; model:Optional[str]=None; serial_no:Optional[str]=None
    installation_date:Optional[str]=None; commissioning_date:Optional[str]=None; purchase_cost:Optional[float]=None; replacement_cost:Optional[float]=None; current_value:Optional[float]=None
    criticality:Optional[str]=None; condition:Optional[str]=None; status:Optional[str]=None; location_id:Optional[int]=None; parent_asset_id:Optional[int]=None; department:Optional[str]=None
    responsible_user_id:Optional[int]=None; vendor_id:Optional[int]=None; warranty_expiry:Optional[str]=None; maintenance_strategy:Optional[str]=None; last_maintenance:Optional[str]=None; next_maintenance:Optional[str]=None; meter_reading:Optional[float]=None
class WorkOrderIn(BaseModel):
    title:str; description:str=''; asset_id:Optional[int]=None; location_id:Optional[int]=None; priority:str='Medium'; work_type:str='Corrective Maintenance'; failure_code:str=''; asset_fmea_id:Optional[int]=None
    assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_start:Optional[str]=None; target_finish:Optional[str]=None; estimated_hours:float=0; safety_requirements:str=''; instructions:str=''; checklist:str=''; estimated_cost:float=0
class WorkOrderPatch(BaseModel):
    title:Optional[str]=None; description:Optional[str]=None; priority:Optional[str]=None; assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_start:Optional[str]=None; target_finish:Optional[str]=None; estimated_hours:Optional[float]=None; safety_requirements:Optional[str]=None; instructions:Optional[str]=None; comments:Optional[str]=None; completion_notes:Optional[str]=None
class TransitionIn(BaseModel): action:str; notes:str=''; signature:str=''
class SLAPolicyPatch(BaseModel): response_minutes:Optional[int]=Field(default=None,gt=0); resolution_minutes:Optional[int]=Field(default=None,gt=0); active:Optional[bool]=None
class NoteIn(BaseModel): note:str
class FieldAssetUpdate(BaseModel): condition:Optional[str]=None; meter_reading:Optional[float]=None
class LaborIn(BaseModel): user_id:Optional[int]=None; hours:float=Field(gt=0); labor_rate:float=0; notes:str=''; work_date:Optional[str]=None
class MaterialIn(BaseModel): item_id:int; quantity:float=Field(gt=0)
class PMIn(BaseModel): name:str; asset_id:int; trigger_type:str='Calendar'; interval_days:Optional[int]=None; meter_interval:Optional[float]=None; next_due:Optional[str]=None; priority:str='Medium'; job_plan:str=''
class InventoryIn(BaseModel): name:str; description:str=''; category:str; warehouse_id:int; current_stock:float=0; reserved_stock:float=0; min_level:float=0; max_level:float=0; reorder_point:float=0; unit_price:float=0; unit:str='ea'; vendor_id:Optional[int]=None; bin:str=''
class InventoryTxIn(BaseModel): tx_type:str; quantity:float; reference:str=''; to_warehouse_id:Optional[int]=None; work_order_id:Optional[int]=None
class PRIn(BaseModel): title:str; site_id:Optional[int]=None; work_order_id:Optional[int]=None; project_id:Optional[int]=None; justification:str=''; items:list[dict]=[]
class POIn(BaseModel): pr_id:int; vendor_id:int; expected_delivery:Optional[str]=None
class QuoteIn(BaseModel): pr_id:int; vendor_id:int; amount:float=Field(gt=0); valid_until:Optional[str]=None
class VendorIn(BaseModel): vendor_code:Optional[str]=None; name:str; category:str; contact_person:str=''; email:str=''; phone:str=''; status:str='Active'
class ContractIn(BaseModel): contract_no:Optional[str]=None; title:str; vendor_id:Optional[int]=None; start_date:Optional[str]=None; end_date:Optional[str]=None; value:float=0; status:str='Active'
class InspectionIn(BaseModel): template_name:str; asset_id:Optional[int]=None; work_order_id:Optional[int]=None; items:list[str]=[]
class InspectionSubmit(BaseModel): responses:list[dict]; remarks:str=''; create_corrective_on_fail:bool=True
class HSEIn(BaseModel): incident_type:str; title:str; site_id:Optional[int]=None; location_id:Optional[int]=None; asset_id:Optional[int]=None; severity:int=Field(ge=1,le=5); probability:int=Field(ge=1,le=5); description:str; corrective_action:str=''; occurred_at:Optional[str]=None
class ProjectIn(BaseModel): name:str; manager_id:Optional[int]=None; site_id:Optional[int]=None; start_date:Optional[str]=None; finish_date:Optional[str]=None; budget:float=0; status:str='Active'
class MeterReadingIn(BaseModel): reading:float
class UserIn(BaseModel): username:str; password:str=Field(min_length=8); full_name:str; email:Optional[str]=None; role_code:str; department:str=''; phone:str=''
class ProfilePatch(BaseModel):
    full_name: Optional[str]=Field(default=None,min_length=2,max_length=120)
    email: Optional[str]=Field(default=None,max_length=180)
    department: Optional[str]=Field(default=None,max_length=120)
    phone: Optional[str]=Field(default=None,max_length=50)
class PasswordChange(BaseModel):
    current_password: str
    new_password: str=Field(min_length=10,max_length=128)
class ProjectTaskIn(BaseModel):
    task_name:str=Field(min_length=2,max_length=200); owner_id:Optional[int]=None; due_date:Optional[str]=None; status:str='Open'; progress:float=Field(default=0,ge=0,le=100)
class ProjectTaskPatch(BaseModel):
    task_name:Optional[str]=Field(default=None,min_length=2,max_length=200); owner_id:Optional[int]=None; due_date:Optional[str]=None; status:Optional[str]=None; progress:Optional[float]=Field(default=None,ge=0,le=100)
class HSEPatch(BaseModel):
    status:Optional[str]=None; corrective_action:Optional[str]=None; severity:Optional[int]=Field(default=None,ge=1,le=5); probability:Optional[int]=Field(default=None,ge=1,le=5)
class UserStatusIn(BaseModel): active:bool
class RolePermissionUpdateIn(BaseModel):
    permissions:list[str]; current_password:str; reason:str=Field(min_length=10,max_length=500); confirmation:str
class UserPermissionOverrideIn(BaseModel):
    permission_code:str; effect:str; current_password:str; reason:str=Field(min_length=10,max_length=500); confirmation:str; expires_at:Optional[str]=None
class UserRoleUpdateIn(BaseModel):
    role_code:str; current_password:str; reason:str=Field(min_length=10,max_length=500); confirmation:str
class ApprovalDecisionIn(BaseModel):
    decision:str; comments:str=''; current_password:str=''; signer_intent:str=Field(default='',max_length=240)
class ApprovalDelegationIn(BaseModel):
    delegate_user_id:int; module:str='*'; start_at:str; end_at:str
class WorkRequirementIn(BaseModel):
    item_id:int; quantity:float=Field(gt=0); required_by:Optional[str]=None
class CraftRequirementIn(BaseModel):
    craft_id:int; planned_hours:float=Field(gt=0)
class TechnicianProfileIn(BaseModel):
    craft_id:Optional[int]=None; home_site_id:Optional[int]=None; weekly_hours:float=Field(default=40,gt=0,le=84); efficiency_pct:float=Field(default=100,gt=0,le=100); active:bool=True
class ShiftAssignmentIn(BaseModel):
    shift_id:int; effective_from:str; effective_to:Optional[str]=None; days_of_week:str='0,1,2,3,4'; active:bool=True
class AbsenceIn(BaseModel):
    user_id:int; start_date:str; end_date:str; absence_type:str; hours_per_day:float=Field(default=8,gt=0,le=24); status:str='Approved'; notes:str=''
class ReservationIn(BaseModel):
    item_id:int; quantity:float=Field(gt=0); notes:str=''
class ReservationIssueIn(BaseModel):
    quantity:Optional[float]=Field(default=None,gt=0)
class OutageIn(BaseModel):
    asset_id:int; work_order_id:Optional[int]=None; outage_type:str='Forced'; cause_code:str=''; impact:str=''; lost_capacity:float=0; capacity_unit:str=''; start_at:Optional[str]=None
class OutageCloseIn(BaseModel):
    end_at:Optional[str]=None; impact:Optional[str]=None
class TelemetryChannelIn(BaseModel):
    channel_code:Optional[str]=None; asset_id:int; name:str; metric_type:str; unit:str; source_system:str='Manual'
    warning_low:Optional[float]=None; critical_low:Optional[float]=None; warning_high:Optional[float]=None; critical_high:Optional[float]=None; active:bool=True
class TelemetryChannelPatch(BaseModel):
    name:Optional[str]=None; metric_type:Optional[str]=None; unit:Optional[str]=None; source_system:Optional[str]=None
    warning_low:Optional[float]=None; critical_low:Optional[float]=None; warning_high:Optional[float]=None; critical_high:Optional[float]=None; active:Optional[bool]=None
class TelemetryReadingItem(BaseModel):
    channel_code:str; value:float; captured_at:Optional[str]=None; quality:str='Good'; source:Optional[str]=None; external_id:Optional[str]=Field(default=None,max_length=160)
class TelemetryIngestIn(BaseModel):
    readings:list[TelemetryReadingItem]=Field(min_length=1,max_length=500); source_system:str='API'; idempotency_key:Optional[str]=Field(default=None,max_length=160)
class CbmRuleIn(BaseModel):
    name:str=Field(min_length=3,max_length=160); channel_id:int; operator:str
    threshold_low:Optional[float]=None; threshold_high:Optional[float]=None
    consecutive_readings:int=Field(default=1,ge=1,le=100); cooldown_minutes:int=Field(default=60,ge=0,le=10080)
    severity:str='Warning'; action_type:str='Recommendation'; work_priority:str='High'; instructions:str=Field(default='',max_length=2000); asset_fmea_id:Optional[int]=None; active:bool=True
class CbmRulePatch(BaseModel):
    name:Optional[str]=Field(default=None,min_length=3,max_length=160); operator:Optional[str]=None
    threshold_low:Optional[float]=None; threshold_high:Optional[float]=None; consecutive_readings:Optional[int]=Field(default=None,ge=1,le=100)
    cooldown_minutes:Optional[int]=Field(default=None,ge=0,le=10080); severity:Optional[str]=None; action_type:Optional[str]=None
    work_priority:Optional[str]=None; instructions:Optional[str]=Field(default=None,max_length=2000); asset_fmea_id:Optional[int]=None; active:Optional[bool]=None
class CbmEventResolveIn(BaseModel):
    reason:str=Field(min_length=3,max_length=500)
class FailureModeIn(BaseModel):
    name:str=Field(min_length=3,max_length=160); category:str=Field(default='General',min_length=2,max_length=100); description:str=Field(default='',max_length=2000); parent_id:Optional[int]=None; active:bool=True
class FailureModePatch(BaseModel):
    name:Optional[str]=Field(default=None,min_length=3,max_length=160); category:Optional[str]=Field(default=None,min_length=2,max_length=100); description:Optional[str]=Field(default=None,max_length=2000); parent_id:Optional[int]=None; active:Optional[bool]=None
class FmeaIn(BaseModel):
    asset_id:int; failure_mode_id:int; function_description:str=Field(default='',max_length=1000); failure_effect:str=Field(min_length=3,max_length=2000); failure_cause:str=Field(min_length=3,max_length=2000)
    current_controls:str=Field(default='',max_length=2000); recommended_action:str=Field(default='',max_length=2000); severity:int=Field(ge=1,le=10); occurrence:int=Field(ge=1,le=10); detectability:int=Field(ge=1,le=10)
    status:str='Active'; owner_id:Optional[int]=None; review_due_date:Optional[str]=None
class FmeaPatch(BaseModel):
    function_description:Optional[str]=Field(default=None,max_length=1000); failure_effect:Optional[str]=Field(default=None,min_length=3,max_length=2000); failure_cause:Optional[str]=Field(default=None,min_length=3,max_length=2000)
    current_controls:Optional[str]=Field(default=None,max_length=2000); recommended_action:Optional[str]=Field(default=None,max_length=2000); severity:Optional[int]=Field(default=None,ge=1,le=10); occurrence:Optional[int]=Field(default=None,ge=1,le=10); detectability:Optional[int]=Field(default=None,ge=1,le=10)
    status:Optional[str]=None; owner_id:Optional[int]=None; review_due_date:Optional[str]=None
class FmeaReviewIn(BaseModel):
    severity:int=Field(ge=1,le=10); occurrence:int=Field(ge=1,le=10); detectability:int=Field(ge=1,le=10); notes:str=Field(min_length=10,max_length=2000); status:Optional[str]=None; review_due_date:Optional[str]=None
class FmeaWorkOrderIn(BaseModel):
    priority:str='High'; assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_finish:Optional[str]=None; notes:str=Field(default='',max_length=1000)

class RcmStrategyIn(BaseModel):
    asset_fmea_id:int; functional_failure:str=Field(min_length=3,max_length=1200); consequence_classification:str; strategy_type:str
    task_description:str=Field(min_length=3,max_length=2000); justification:str=Field(min_length=10,max_length=3000); interval_days:Optional[int]=Field(default=None,ge=1,le=3650)
    linked_cbm_rule_id:Optional[int]=None; linked_pm_plan_id:Optional[int]=None; owner_id:Optional[int]=None; review_due_date:Optional[str]=None
class RcmStrategyPatch(BaseModel):
    functional_failure:Optional[str]=Field(default=None,min_length=3,max_length=1200); consequence_classification:Optional[str]=None; strategy_type:Optional[str]=None
    task_description:Optional[str]=Field(default=None,min_length=3,max_length=2000); justification:Optional[str]=Field(default=None,min_length=10,max_length=3000); interval_days:Optional[int]=Field(default=None,ge=1,le=3650)
    linked_cbm_rule_id:Optional[int]=None; linked_pm_plan_id:Optional[int]=None; owner_id:Optional[int]=None; review_due_date:Optional[str]=None
class RcmActionIn(BaseModel):
    notes:str=Field(default='',max_length=1000)
class RcmReviewIn(BaseModel):
    outcome:str; notes:str=Field(min_length=10,max_length=2000); review_due_date:Optional[str]=None

class IntegrationKeyIn(BaseModel):
    name:str=Field(min_length=3,max_length=120); expires_at:Optional[str]=None
class AlarmSuppressionIn(BaseModel):
    site_id:Optional[int]=None; asset_id:Optional[int]=None; channel_id:Optional[int]=None; reason:str=Field(min_length=3,max_length=500); start_at:str; end_at:str
class AlarmShelfIn(BaseModel):
    reason:str=Field(min_length=10,max_length=500); duration_minutes:int=Field(ge=5,le=1440)
class AssetTopologyLinkIn(BaseModel):
    upstream_asset_id:int; downstream_asset_id:int; relation_type:str=Field(default='Feeds',min_length=3,max_length=80); notes:str=Field(default='',max_length=500)
class IncidentTransitionIn(BaseModel):
    notes:str=''
class IncidentWorkOrderIn(BaseModel):
    assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_finish:Optional[str]=None; notes:str=''
class AlarmWorkOrderIn(BaseModel):
    assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_finish:Optional[str]=None; notes:str=''
class DispatchIn(BaseModel):
    technician_user_id:int; eta_minutes:Optional[int]=Field(default=None,ge=0,le=1440); notes:str=''
class DispatchTransitionIn(BaseModel):
    action:str; notes:str=''
class FieldSyncOperationIn(BaseModel):
    operation_id:str=Field(min_length=8,max_length=160)
    entity_type:str=Field(min_length=3,max_length=40)
    entity_id:int=Field(gt=0)
    operation_type:str=Field(min_length=3,max_length=40)
    base_hash:str=Field(default='',max_length=128)
    payload:dict=Field(default_factory=dict)
    client_created_at:Optional[str]=None
class FieldSyncPushIn(BaseModel):
    client_id:str=Field(min_length=8,max_length=128)
    device_name:str=Field(default='',max_length=120)
    operations:list[FieldSyncOperationIn]=Field(min_length=1,max_length=100)
class FieldSyncResolveIn(BaseModel):
    resolution:str=Field(pattern='^(discard|retry)$')
    expected_server_hash:str=Field(default='',max_length=128)

@app.get('/api/health')
def health():
    with db() as conn:
        conn.execute('SELECT 1').fetchone()
        r=conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone();schema=r[0] if r and r[0] is not None else 0
        last=conn.execute('SELECT run_no,status,finished_at FROM job_runs ORDER BY id DESC LIMIT 1').fetchone()
    return {'status':'ok','application':APP_NAME,'version':APP_VERSION,'database_backend':DB_BACKEND,'schema_version':schema,'automation_interval_minutes':AUTOMATION_INTERVAL_MINUTES,'last_automation_run':dict(last) if last else None}

@app.get('/api/health/ready')
def health_ready():
    with db() as conn:
        counts={'users':conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],'assets':conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]}
    return {'status':'ready','database_backend':DB_BACKEND,'schema_version':SCHEMA_VERSION,'checks':counts}

# ---------- auth ----------
@app.post('/api/auth/login')
def login(body:LoginIn, request:Request):
    key=_login_key(request,body.username)
    if _login_is_blocked(key): raise HTTPException(429,'Too many failed login attempts. Try again later.')
    with db() as conn:
        r=conn.execute('SELECT u.*,r.code role,r.name role_name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.username=? AND u.active=1',(body.username,)).fetchone()
        if not r or not verify_password(body.password,r['password_hash']):
            _login_failure(key)
            raise HTTPException(401,'Invalid username or password')
        _login_success(key)
        token=secrets.token_urlsafe(36);expires_at=(datetime.now()+timedelta(hours=SESSION_HOURS)).isoformat(timespec='seconds')
        conn.execute('INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',(token,r['id'],now(),expires_at))
        audit(conn,r['id'],'LOGIN','Authentication',r['username'],'','Successful login')
        return {'token':token,'expires_at':expires_at,'user':{'id':r['id'],'username':r['username'],'full_name':r['full_name'],'email':r['email'],'department':r['department'],'phone':r['phone'],'role':r['role'],'role_name':r['role_name']}}

@app.post('/api/auth/logout')
def logout(authorization:Optional[str]=Header(None), user=Depends(current_user)):
    token=authorization.split(' ',1)[1]
    with db() as conn: conn.execute('DELETE FROM sessions WHERE token=?',(token,)); audit(conn,user['id'],'LOGOUT','Authentication',user['username'])
    return {'ok':True}
@app.get('/api/auth/me')
def me(user=Depends(current_user)): return user
@app.get('/api/auth/me/permissions')
def my_permissions(user=Depends(current_user)):
    perms=effective_permissions(user)
    return {'role':user['role'],'permissions':perms,'allowed':[p['code'] for p in perms if p['allowed']]}
@app.patch('/api/auth/profile')
def update_profile(body:ProfilePatch,user=Depends(current_user)):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    if not changes:return user
    with db() as conn:
        old=get_or_404(conn,'SELECT full_name,email,department,phone FROM users WHERE id=?',(user['id'],),'User not found')
        sets=','.join(f'{k}=?' for k in changes)
        try:
            conn.execute(f'UPDATE users SET {sets} WHERE id=?',(*changes.values(),user['id']))
        except Exception as exc:
            if 'UNIQUE' in str(exc).upper(): raise HTTPException(409,'Email is already in use')
            raise
        audit(conn,user['id'],'UPDATE','Profile',user['username'],old,changes)
        r=conn.execute('SELECT u.id,u.username,u.full_name,u.email,u.department,u.phone,r.code role,r.name role_name,u.active FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(user['id'],)).fetchone()
        return dict(r)

@app.post('/api/auth/change-password')
def change_password(body:PasswordChange,authorization:Optional[str]=Header(None),user=Depends(current_user)):
    if not any(c.isupper() for c in body.new_password) or not any(c.islower() for c in body.new_password) or not any(c.isdigit() for c in body.new_password) or body.new_password.isalnum():
        raise HTTPException(400,'Password must include upper, lower, number and special character')
    with db() as conn:
        r=get_or_404(conn,'SELECT password_hash FROM users WHERE id=?',(user['id'],),'User not found')
        if not verify_password(body.current_password,r['password_hash']): raise HTTPException(400,'Current password is incorrect')
        if verify_password(body.new_password,r['password_hash']): raise HTTPException(400,'New password must be different')
        conn.execute('UPDATE users SET password_hash=? WHERE id=?',(hash_password(body.new_password),user['id']))
        token=authorization.split(' ',1)[1] if authorization else ''
        conn.execute('DELETE FROM sessions WHERE user_id=? AND token<>?',(user['id'],token))
        audit(conn,user['id'],'PASSWORD_CHANGE','Authentication',user['username'],'','Password updated; other sessions revoked')
    return {'ok':True,'other_sessions_revoked':True}

@app.get('/api/auth/sessions')
def list_sessions(authorization:Optional[str]=Header(None),user=Depends(current_user)):
    current=authorization.split(' ',1)[1] if authorization else ''
    with db() as conn:
        data=rows(conn.execute('SELECT token,created_at,expires_at FROM sessions WHERE user_id=? ORDER BY created_at DESC',(user['id'],)))
        return [{'session_id':x['token'][:10],'created_at':x['created_at'],'expires_at':x['expires_at'],'current':x['token']==current} for x in data]

@app.post('/api/auth/sessions/revoke-others')
def revoke_other_sessions(authorization:Optional[str]=Header(None),user=Depends(current_user)):
    current=authorization.split(' ',1)[1] if authorization else ''
    with db() as conn:
        cur=conn.execute('DELETE FROM sessions WHERE user_id=? AND token<>?',(user['id'],current))
        audit(conn,user['id'],'SESSION_REVOKE','Authentication',user['username'],'',f'{cur.rowcount} session(s) revoked')
        return {'ok':True,'revoked':cur.rowcount}


# ---------- approvals / workflow ----------
APPROVAL_SELECT="""SELECT ap.*,req.full_name requested_by_name,dec.full_name decided_by_name,ass.full_name assigned_user_name,
sig.evidence_no,sig.signed_at,sig.evidence_hash,sig.credential_verified,sig.delegated_authority,sig.intent_statement
FROM approval_requests ap
JOIN users req ON req.id=ap.requested_by
LEFT JOIN users dec ON dec.id=ap.decided_by
LEFT JOIN users ass ON ass.id=ap.assigned_user_id
LEFT JOIN approval_signature_evidence sig ON sig.approval_id=ap.id"""

@app.get('/api/approvals')
def list_approvals(status:str='Pending',module:str='',user=Depends(current_user)):
    sql=APPROVAL_SELECT+' WHERE 1=1';args=[]
    if status:sql+=' AND ap.status=?';args.append(status)
    if module:sql+=' AND ap.module=?';args.append(module)
    if user['role'] not in ('admin','maintenance_manager','executive'):
        sql+=""" AND (ap.assigned_user_id=? OR ap.assigned_role=? OR ap.requested_by=? OR EXISTS (
          SELECT 1 FROM approval_delegations d WHERE d.delegator_user_id=ap.assigned_user_id AND d.delegate_user_id=?
          AND d.active=1 AND d.start_at<=? AND d.end_at>=? AND (d.module='*' OR d.module=ap.module)
        ))""";stamp=now();args += [user['id'],user['role'],user['id'],user['id'],stamp,stamp]
    sql+=" ORDER BY CASE ap.status WHEN 'Pending' THEN 0 ELSE 1 END,ap.id DESC"
    with db() as conn:
        result=rows(conn.execute(sql,args))
        for a in result:a['delegated_to_me']=bool(a['status']=='Pending' and _delegation_active(conn,a,user['id']))
        return result

@app.post('/api/approvals/{approval_id}/decision')
def decide_approval(approval_id:int,body:ApprovalDecisionIn,user=Depends(current_user)):
    decision=body.decision.lower().strip()
    if decision not in ('approve','reject'):raise HTTPException(400,'Decision must be approve or reject')
    with db() as conn:
        if not has_permission(user,'approvals.decide'):
            raise HTTPException(403,'Missing permission: approvals.decide')
        ap=get_or_404(conn,'SELECT * FROM approval_requests WHERE id=?',(approval_id,),'Approval request not found')
        if ap['status']!='Pending':raise HTTPException(409,'Approval request is already decided')
        delegated=_delegation_active(conn,ap,user['id'])
        allowed=user['role'] in ('admin','maintenance_manager') or ap['assigned_user_id']==user['id'] or (ap['assigned_role'] and ap['assigned_role']==user['role']) or delegated
        if not allowed:raise HTTPException(403,'This approval is not assigned to your role or user')
        target='Approved' if decision=='approve' else 'Rejected'
        if ap['record_type']=='work_order':
            rec=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(ap['record_id'],),'Work order not found')
            if rec['status']!='Submitted':raise HTTPException(409,f"Work order is {rec['status']}, not Submitted")
        elif ap['record_type']=='purchase_requisition':
            rec=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(ap['record_id'],),'Purchase requisition not found')
            if rec['status']!='Submitted':raise HTTPException(409,f"Purchase requisition is {rec['status']}, not Submitted")
        elif ap['record_type']=='alarm_shelf':
            rec=get_or_404(conn,'SELECT sh.*,oa.alarm_no,oa.status alarm_status,oa.severity FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id WHERE sh.id=?',(ap['record_id'],),'Alarm shelf request not found')
            if rec['status']!='Pending':raise HTTPException(409,f"Alarm shelf request is {rec['status']}, not Pending")
            if ap['requested_by']==user['id']:raise HTTPException(403,'Alarm shelving requires approval by a different authorized user')
        elif ap['record_type']=='rcm_strategy':
            rec=_rcm_strategy_record(conn,ap['record_id'])
            if rec['status']!='Review':raise HTTPException(409,f"RCM strategy is {rec['status']}, not Review")
            if ap['requested_by']==user['id']:raise HTTPException(403,'RCM strategy approval requires a different authorized user')
            if not has_permission(user,'reliability.rcm.approve'):raise HTTPException(403,'Missing permission: reliability.rcm.approve')
        else:
            raise HTTPException(400,'Unsupported approval record type')

        expected_intent=_approval_expected_intent(decision,ap['record_code'])
        if body.signer_intent.strip()!=expected_intent:
            raise HTTPException(400,f'Electronic signature intent must exactly match: {expected_intent}')
        pwd=conn.execute('SELECT password_hash FROM users WHERE id=? AND active=1',(user['id'],)).fetchone()
        if not pwd or not body.current_password or not verify_password(body.current_password,pwd['password_hash']):
            raise HTTPException(401,'Electronic signature re-authentication failed')

        if ap['record_type']=='work_order':
            conn.execute('UPDATE work_orders SET status=?,updated_at=? WHERE id=?',(target,now(),rec['id']))
            workflow_event(conn,'Work Management','work_order',rec['id'],rec['wo_no'],decision.upper(),rec['status'],target,user['id'],body.comments)
            audit(conn,user['id'],decision.upper(),'Work Management',rec['wo_no'],rec['status'],target)
        elif ap['record_type']=='purchase_requisition':
            conn.execute('UPDATE purchase_requisitions SET status=?,approved_at=? WHERE id=?',(target,now() if target=='Approved' else None,rec['id']))
            workflow_event(conn,'Procurement','purchase_requisition',rec['id'],rec['pr_no'],decision.upper(),rec['status'],target,user['id'],body.comments)
            audit(conn,user['id'],decision.upper(),'Procurement',rec['pr_no'],rec['status'],target)
        elif ap['record_type']=='alarm_shelf':
            stamp=now()
            if target=='Approved':
                end=(_dt(stamp)+timedelta(minutes=rec['duration_minutes'])).isoformat(timespec='seconds')
                conn.execute("UPDATE alarm_shelves SET status='Approved',approved_by=?,approved_at=?,start_at=?,end_at=?,decision_comments=? WHERE id=?",(user['id'],stamp,stamp,end,body.comments,rec['id']))
                emit_event(conn,'operations.alarm_shelf.approved','alarm_shelf',rec['shelf_no'],{'shelf_no':rec['shelf_no'],'alarm_no':rec['alarm_no'],'end_at':end})
            else:
                conn.execute("UPDATE alarm_shelves SET status='Rejected',rejected_by=?,rejected_at=?,decision_comments=? WHERE id=?",(user['id'],stamp,body.comments,rec['id']))
                emit_event(conn,'operations.alarm_shelf.rejected','alarm_shelf',rec['shelf_no'],{'shelf_no':rec['shelf_no'],'alarm_no':rec['alarm_no']})
            audit(conn,user['id'],decision.upper()+' ALARM SHELF','Utilities Operations',rec['shelf_no'],rec['status'],target)
        elif ap['record_type']=='rcm_strategy':
            stamp=now();strategy_status='Approved' if target=='Approved' else 'Draft'
            if target=='Approved':
                conn.execute("UPDATE rcm_strategies SET status='Approved',approved_by=?,approved_at=?,last_decision_comments=?,updated_by=?,updated_at=? WHERE id=?",(user['id'],stamp,body.comments,user['id'],stamp,rec['id']))
                emit_event(conn,'maintenance.reliability.rcm_approved','rcm_strategy',rec['strategy_no'],{'strategy_no':rec['strategy_no'],'fmea_no':rec['fmea_no'],'strategy_type':rec['strategy_type']})
            else:
                conn.execute("UPDATE rcm_strategies SET status='Draft',approved_by=NULL,approved_at=NULL,last_decision_comments=?,updated_by=?,updated_at=? WHERE id=?",(body.comments,user['id'],stamp,rec['id']))
                emit_event(conn,'maintenance.reliability.rcm_rejected','rcm_strategy',rec['strategy_no'],{'strategy_no':rec['strategy_no'],'fmea_no':rec['fmea_no']})
            workflow_event(conn,'Reliability','rcm_strategy',rec['id'],rec['strategy_no'],decision.upper(),rec['status'],strategy_status,user['id'],body.comments)
            audit(conn,user['id'],decision.upper()+' RCM','Reliability',rec['strategy_no'],rec['status'],strategy_status)

        resolve_approval(conn,ap['module'],ap['record_type'],ap['record_id'],decision,user['id'],body.comments)
        snapshot=_approval_target_snapshot(conn,ap)
        evidence=_record_approval_signature(conn,ap,target,user,body.signer_intent.strip(),body.comments,delegated,snapshot)
        audit(conn,user['id'],'E-SIGN '+decision.upper(),'Approvals',ap['approval_no'],'',{'evidence_no':evidence['evidence_no'],'record_code':ap['record_code'],'evidence_hash':evidence['evidence_hash']})
        module_link='commandcenter' if ap['record_type']=='alarm_shelf' else ('work' if ap['record_type']=='work_order' else ('reliability' if ap['record_type']=='rcm_strategy' else 'procurement'))
        notify(conn,'Approval decision',f"{ap['record_code']} was {target.lower()} and electronically signed",'Info',ap['requested_by'],None,module_link,ap['record_code'])
        return {'ok':True,'status':target,'record_code':ap['record_code'],'signature_evidence':evidence}

@app.get('/api/approvals/{approval_id}/signature-evidence')
def approval_signature_evidence(approval_id:int,user=Depends(current_user)):
    with db() as conn:
        ap=get_or_404(conn,'SELECT * FROM approval_requests WHERE id=?',(approval_id,),'Approval request not found')
        visible=user['role'] in ('admin','maintenance_manager','executive') or ap['requested_by']==user['id'] or ap['assigned_user_id']==user['id'] or (ap['assigned_role'] and ap['assigned_role']==user['role']) or ap.get('decided_by')==user['id'] or _delegation_active(conn,ap,user['id'])
        if not visible:raise HTTPException(403,'Approval evidence is not visible to this user')
        evidence=get_or_404(conn,'SELECT * FROM approval_signature_evidence WHERE approval_id=?',(approval_id,),'Electronic signature evidence not found')
        evidence['payload']=json.loads(evidence['payload_json'])
        evidence.pop('payload_json',None)
        return evidence

@app.get('/api/approval-signatures/verify')
def approval_signature_integrity(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return verify_approval_signature_chain(conn)

@app.get('/api/approval-delegations')
def list_approval_delegations(user=Depends(current_user)):
    with db() as conn:
        sql="""SELECT d.*,src.full_name delegator_name,src.username delegator_username,dst.full_name delegate_name,dst.username delegate_username,creator.full_name created_by_name
        FROM approval_delegations d JOIN users src ON src.id=d.delegator_user_id JOIN users dst ON dst.id=d.delegate_user_id JOIN users creator ON creator.id=d.created_by"""
        args=[]
        if user['role']!='admin':sql+=' WHERE d.delegator_user_id=? OR d.delegate_user_id=?';args=[user['id'],user['id']]
        sql+=' ORDER BY d.active DESC,d.end_at DESC,d.id DESC'
        return rows(conn.execute(sql,args))

@app.post('/api/approval-delegations')
def create_approval_delegation(body:ApprovalDelegationIn,user=Depends(current_user)):
    if body.delegate_user_id==user['id']:raise HTTPException(400,'You cannot delegate approvals to yourself')
    try:start=_dt(body.start_at);end=_dt(body.end_at)
    except Exception:raise HTTPException(400,'Invalid delegation date/time')
    if end<=start:raise HTTPException(400,'Delegation end must be after start')
    if (end-start).days>366:raise HTTPException(400,'Delegation cannot exceed 366 days')
    with db() as conn:
        delegate=get_or_404(conn,'SELECT u.id,u.active,u.full_name FROM users u WHERE u.id=?',(body.delegate_user_id,),'Delegate user not found')
        if not delegate['active']:raise HTTPException(409,'Delegate user is inactive')
        cur=conn.execute('INSERT INTO approval_delegations(delegator_user_id,delegate_user_id,module,start_at,end_at,active,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)',(user['id'],body.delegate_user_id,body.module or '*',start.isoformat(timespec='seconds'),end.isoformat(timespec='seconds'),user['id'],now()))
        audit(conn,user['id'],'DELEGATE','Approvals',str(cur.lastrowid),'',{'delegate':delegate['full_name'],'module':body.module or '*','start_at':body.start_at,'end_at':body.end_at})
        notify(conn,'Approval delegation',f"{user['full_name']} delegated approvals to you through {end.date().isoformat()}",'Info',body.delegate_user_id,None,'approvals',str(cur.lastrowid))
        return {'id':cur.lastrowid,'active':True}

@app.patch('/api/approval-delegations/{delegation_id}/deactivate')
def deactivate_approval_delegation(delegation_id:int,user=Depends(current_user)):
    with db() as conn:
        d=get_or_404(conn,'SELECT * FROM approval_delegations WHERE id=?',(delegation_id,),'Delegation not found')
        if user['role']!='admin' and d['delegator_user_id']!=user['id']:raise HTTPException(403,'Only the delegator or administrator can deactivate this delegation')
        conn.execute('UPDATE approval_delegations SET active=0 WHERE id=?',(delegation_id,));audit(conn,user['id'],'DEACTIVATE DELEGATION','Approvals',str(delegation_id),1,0);return {'ok':True}

@app.get('/api/assets/health')
def asset_health_portfolio(site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        sql='SELECT a.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id';args=[]
        if site_id is not None:sql+=' WHERE l.site_id=?';args=[site_id]
        health=[_asset_health(conn,r['id']) for r in rows(conn.execute(sql,args))]
        health.sort(key=lambda x:x['score'])
        avg=round(sum(x['score'] for x in health)/max(len(health),1),1)
        bands={}
        for x in health:bands[x['risk_band']]=bands.get(x['risk_band'],0)+1
        return {'average_score':avg,'bands':bands,'assets':health}

@app.get('/api/assets/{asset_id}/health')
def asset_health_detail(asset_id:int,user=Depends(current_user)):
    with db() as conn:
        current=_asset_health(conn,asset_id);history=rows(conn.execute('SELECT score,risk_band,factors_json,calculated_at FROM asset_health_snapshots WHERE asset_id=? ORDER BY id DESC LIMIT 30',(asset_id,)))
        for h in history:
            try:h['factors']=json.loads(h.pop('factors_json'))
            except:h['factors']={}
        return {'current':current,'history':history}

@app.post('/api/assets/health/recalculate')
def recalculate_asset_health(user=Depends(require_roles(*WRITE_ROLES))):
    with db() as conn:
        result=[_save_asset_health(conn,r['id'],user['id']) for r in rows(conn.execute('SELECT id FROM assets ORDER BY id'))]
        audit(conn,user['id'],'RECALCULATE','Asset Health','portfolio','',{'assets':len(result)})
        emit_event(conn,'asset_health.recalculated','portfolio','all',{'assets':len(result),'average_score':round(sum(x['score'] for x in result)/max(len(result),1),1)})
        return {'count':len(result),'average_score':round(sum(x['score'] for x in result)/max(len(result),1),1),'assets':result}

@app.get('/api/planning/maintenance-forecast')
def maintenance_forecast(horizon_days:int=Query(90,ge=7,le=365),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:return _maintenance_forecast(conn,horizon_days,site_id)

@app.get('/api/workforce/crafts')
def workforce_crafts(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT * FROM crafts ORDER BY name'))

@app.get('/api/workforce/shifts')
def workforce_shifts(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT * FROM shift_templates ORDER BY shift_code'))

@app.get('/api/workforce/technicians')
def workforce_technicians(site_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT tp.*,u.username,u.full_name,u.email,c.craft_code,c.name craft_name,s.site_code,s.name site_name
      FROM technician_profiles tp JOIN users u ON u.id=tp.user_id LEFT JOIN crafts c ON c.id=tp.craft_id LEFT JOIN sites s ON s.id=tp.home_site_id WHERE 1=1""";args=[]
    if site_id is not None:sql+=' AND tp.home_site_id=?';args.append(site_id)
    sql+=' ORDER BY u.full_name'
    with db() as conn:
        techs=rows(conn.execute(sql,args));today=date.today()
        for t in techs:
            t['active_shifts']=rows(conn.execute("""SELECT tsa.*,st.shift_code,st.name shift_name,st.start_time,st.end_time,st.paid_hours FROM technician_shift_assignments tsa JOIN shift_templates st ON st.id=tsa.shift_id WHERE tsa.user_id=? AND tsa.active=1 ORDER BY tsa.effective_from DESC""",(t['user_id'],)))
            t['current_absences']=rows(conn.execute("SELECT * FROM technician_absences WHERE user_id=? AND status='Approved' AND start_date<=? AND end_date>=? ORDER BY start_date",(t['user_id'],today.isoformat(),today.isoformat())))
        return techs

@app.put('/api/workforce/technicians/{user_id}')
def upsert_technician_profile(user_id:int,body:TechnicianProfileIn,user=Depends(require_roles('admin','maintenance_manager','planner'))):
    with db() as conn:
        target=get_or_404(conn,"SELECT u.id,u.full_name,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?",(user_id,),'User not found')
        if target['role']!='technician':raise HTTPException(409,'Workforce profiles can only be assigned to technician users')
        existing=conn.execute('SELECT * FROM technician_profiles WHERE user_id=?',(user_id,)).fetchone();vals=body.model_dump()
        if existing:
            conn.execute('UPDATE technician_profiles SET craft_id=?,home_site_id=?,weekly_hours=?,efficiency_pct=?,active=?,updated_at=? WHERE user_id=?',(body.craft_id,body.home_site_id,body.weekly_hours,body.efficiency_pct,int(body.active),now(),user_id));action='UPDATE'
        else:
            conn.execute('INSERT INTO technician_profiles(user_id,craft_id,home_site_id,weekly_hours,efficiency_pct,active,updated_at) VALUES(?,?,?,?,?,?,?)',(user_id,body.craft_id,body.home_site_id,body.weekly_hours,body.efficiency_pct,int(body.active),now()));action='CREATE'
        audit(conn,user['id'],action,'Workforce',str(user_id),dict(existing) if existing else '',vals);return {'ok':True}

@app.post('/api/workforce/technicians/{user_id}/shift-assignments')
def add_shift_assignment(user_id:int,body:ShiftAssignmentIn,user=Depends(require_roles('admin','maintenance_manager','planner'))):
    try:
        start=date.fromisoformat(body.effective_from[:10]);end=date.fromisoformat(body.effective_to[:10]) if body.effective_to else None
        if end and end<start:raise ValueError
    except Exception:raise HTTPException(400,'Invalid shift effective date range')
    with db() as conn:
        get_or_404(conn,'SELECT id FROM technician_profiles WHERE user_id=? AND active=1',(user_id,),'Active technician profile not found');get_or_404(conn,'SELECT id FROM shift_templates WHERE id=? AND active=1',(body.shift_id,),'Shift not found')
        cur=conn.execute('INSERT INTO technician_shift_assignments(user_id,shift_id,effective_from,effective_to,days_of_week,active) VALUES(?,?,?,?,?,?)',(user_id,body.shift_id,start.isoformat(),end.isoformat() if end else None,body.days_of_week,int(body.active)))
        audit(conn,user['id'],'ASSIGN SHIFT','Workforce',str(user_id),'',body.model_dump());return {'id':cur.lastrowid}

@app.get('/api/workforce/absences')
def workforce_absences(site_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT a.*,u.full_name,u.username,s.site_code,s.name site_name FROM technician_absences a JOIN users u ON u.id=a.user_id LEFT JOIN technician_profiles tp ON tp.user_id=u.id LEFT JOIN sites s ON s.id=tp.home_site_id WHERE 1=1""";args=[]
    if site_id is not None:sql+=' AND tp.home_site_id=?';args.append(site_id)
    sql+=' ORDER BY a.start_date DESC,a.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/workforce/absences')
def create_absence(body:AbsenceIn,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor'))):
    try:
        start=date.fromisoformat(body.start_date[:10]);end=date.fromisoformat(body.end_date[:10])
        if end<start:raise ValueError
    except Exception:raise HTTPException(400,'Invalid absence date range')
    if body.status not in ('Approved','Pending','Rejected','Cancelled'):raise HTTPException(400,'Invalid absence status')
    with db() as conn:
        get_or_404(conn,'SELECT id FROM technician_profiles WHERE user_id=?',(body.user_id,),'Technician profile not found')
        cur=conn.execute('INSERT INTO technician_absences(user_id,start_date,end_date,absence_type,hours_per_day,status,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(body.user_id,start.isoformat(),end.isoformat(),body.absence_type,body.hours_per_day,body.status,body.notes,user['id'],now()))
        audit(conn,user['id'],'CREATE ABSENCE','Workforce',str(cur.lastrowid),'',body.model_dump());return {'id':cur.lastrowid}

@app.get('/api/workforce/capacity')
def workforce_capacity(weeks:int=Query(8,ge=1,le=52),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        start=_forecast_bucket_start(date.today());out=[]
        for i in range(weeks):
            ws=start+timedelta(days=7*i);cap=_workforce_week_capacity(conn,ws,site_id);out.append({'week_start':ws.isoformat(),**cap})
        return {'site_id':site_id,'weeks':out,'total_capacity_hours':round(sum(x['capacity_hours'] for x in out),1)}

@app.get('/api/reliability/assets')
def reliability_assets(period_days:int=Query(365,ge=30,le=3650),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:return {'period_days':period_days,'assets':_asset_reliability_rows(conn,period_days,site_id)}

@app.get('/api/reliability/sites')
def reliability_sites(period_days:int=Query(365,ge=30,le=3650),user=Depends(current_user)):
    with db() as conn:return {'period_days':period_days,'sites':_site_reliability_rows(conn,period_days)}

@app.get('/api/workflow-events')
def list_workflow_events(module:str='',record_type:str='',record_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT e.*,u.full_name actor_name FROM workflow_events e JOIN users u ON u.id=e.actor_id WHERE 1=1""";args=[]
    if module:sql+=' AND e.module=?';args.append(module)
    if record_type:sql+=' AND e.record_type=?';args.append(record_type)
    if record_id is not None:sql+=' AND e.record_id=?';args.append(record_id)
    sql+=' ORDER BY e.id DESC LIMIT 300'
    with db() as conn:return rows(conn.execute(sql,args))

# ---------- launchpad / dashboard ----------
@app.get('/api/launchpad')
def launchpad(user=Depends(current_user)):
    apps=[
      ('assets','Asset Management','Enterprise asset registry & hierarchy','AS'),('work','Work Management','Plan, assign and execute work','WO'),('maintenance','Preventive Maintenance','Calendar, meter and condition plans','PM'),('workforce','Workforce Planning','Crafts, shifts, absences and capacity','WF'),('inventory','Inventory','Spares, warehouses and transactions','IN'),('procurement','Procurement','PR, approval, PO and receipt','PO'),('approvals','Approval Center','Unified operational approval queue','AP'),('operations','Utilities Operations','Electrical, water and infrastructure','OP'),('commandcenter','Utility Command Center','Correlated incidents, outages, dispatch and telemetry quality','CC'),('telemetry','Telemetry & Alarms','SCADA-style readings, thresholds and alarm response','TM'),('field','Field Service','Technician mobile workspace','FS'),('dispatch','Technician Dispatch','Dispatch board, ETA and field arrival','DP'),('map','GIS / Locations','Sites, assets, work and alerts','GI'),('inspections','Inspection Management','Digital inspection forms','IP'),('hse','Safety & HSE','Incidents, hazards and actions','HS'),('contracts','Contracts','Utility service and supply agreements','CT'),('vendors','Vendors','Supplier and OEM management','VN'),('projects','Projects','Budgets, progress and milestones','PJ'),('documents','Documents','Technical records and attachments','DC'),('analytics','Analytics','Reliability, cost and performance','AN'),('automation','Automation & Reports','Scheduled controls, exports, backups and observability','AU'),('administration','Administration','Users, RBAC and audit','AD')]
    return [{'code':a[0],'name':a[1],'description':a[2],'icon':a[3]} for a in apps]

@app.get('/api/dashboard')
def dashboard(site_id:Optional[int]=None,as_of:Optional[str]=None,user=Depends(current_user)):
    asof=as_of or date.today().isoformat()
    where='';args=[]
    if site_id:
        where=' AND l.site_id=?';args=[site_id]
    with db() as conn:
        asset_total=conn.execute('SELECT COUNT(*) FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE 1=1'+where,args).fetchone()[0]
        operating=conn.execute("SELECT COUNT(*) FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE a.status='Operating'"+where,args).fetchone()[0]
        maintenance=conn.execute("SELECT COUNT(*) FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE a.status IN ('Under Maintenance','Restricted')"+where,args).fetchone()[0]
        critical=conn.execute("SELECT COUNT(*) FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE a.criticality='Critical'"+where,args).fetchone()[0]
        openwo=conn.execute("SELECT COUNT(*) FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id WHERE w.status NOT IN ('Completed','Closed','Cancelled')"+where,args).fetchone()[0]
        emergency=conn.execute("SELECT COUNT(*) FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id WHERE (w.priority='Emergency' OR w.work_type='Emergency') AND w.status NOT IN ('Closed','Cancelled')"+where,args).fetchone()[0]
        overdue=conn.execute("SELECT COUNT(*) FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id WHERE w.target_finish IS NOT NULL AND w.target_finish<? AND w.status NOT IN ('Completed','Closed','Cancelled')"+where,[asof]+args).fetchone()[0]
        completed=conn.execute("SELECT COUNT(*) FROM work_orders w LEFT JOIN locations l ON l.id=w.location_id WHERE w.status IN ('Completed','Closed')"+where,args).fetchone()[0]
        rel_assets=_asset_reliability_rows(conn,365,site_id);rel_failures=sum(x['failures'] for x in rel_assets);rel_downtime=sum(x['downtime_hours'] for x in rel_assets);rel_period=sum(x['period_hours'] for x in rel_assets);rel_uptime=max(0.0,rel_period-rel_downtime)
        avg_repair=(rel_downtime/rel_failures) if rel_failures else 0;mtbf=(rel_uptime/rel_failures) if rel_failures else None
        inv_value=conn.execute('SELECT SUM(current_stock*unit_price) FROM inventory_items').fetchone()[0] or 0
        low=conn.execute('SELECT COUNT(*) FROM inventory_items WHERE current_stock-reserved_stock<=reorder_point').fetchone()[0]
        po_pending=conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status NOT IN ('Received','Cancelled')").fetchone()[0]
        tech=conn.execute("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='technician' AND u.active=1").fetchone()[0]
        incidents=conn.execute("SELECT COUNT(*) FROM safety_incidents WHERE status NOT IN ('Closed','Cancelled')").fetchone()[0]
        open_outages=conn.execute("SELECT COUNT(*) FROM asset_outages WHERE status='Open'").fetchone()[0]
        active_dispatches=conn.execute("SELECT COUNT(*) FROM dispatch_assignments WHERE status IN ('Dispatched','Accepted','En Route','On Site')").fetchone()[0]
        alarm_sql="SELECT COUNT(*) FROM operational_alarms WHERE status IN ('Open','Acknowledged')";alarm_args=[]
        crit_alarm_sql=alarm_sql+" AND severity='Critical'";crit_alarm_args=[]
        if site_id:
            alarm_sql+=' AND site_id=?';alarm_args=[site_id];crit_alarm_sql+=' AND site_id=?';crit_alarm_args=[site_id]
        active_alarms=conn.execute(alarm_sql,alarm_args).fetchone()[0];critical_alarms=conn.execute(crit_alarm_sql,crit_alarm_args).fetchone()[0]
        inc_sql="SELECT COUNT(*) FROM alarm_incidents WHERE status IN ('Open','Acknowledged')";inc_args=[]
        if site_id:inc_sql+=' AND site_id=?';inc_args=[site_id]
        open_alarm_incidents=conn.execute(inc_sql,inc_args).fetchone()[0]
        data_quality=_telemetry_quality_summary(conn,24,site_id)
        pm_total=conn.execute('SELECT COUNT(*) FROM maintenance_plans WHERE active=1').fetchone()[0]
        pm_over=conn.execute("SELECT COUNT(*) FROM maintenance_plans WHERE active=1 AND trigger_type='Calendar' AND next_due IS NOT NULL AND next_due<?",(asof,)).fetchone()[0]
        pm_compliance=round(100*(pm_total-pm_over)/pm_total,1) if pm_total else 100
        downtime=conn.execute("SELECT SUM(actual_hours) FROM work_orders WHERE work_type LIKE 'Corrective%' AND status IN ('Completed','Closed')").fetchone()[0] or 0
        statuses=rows(conn.execute('SELECT status,COUNT(*) count FROM work_orders GROUP BY status'))
        health=rows(conn.execute('SELECT condition,COUNT(*) count FROM assets GROUP BY condition'))
        costs=rows(conn.execute('SELECT a.asset_no,a.name,COALESCE(SUM(w.actual_cost),0) cost FROM assets a LEFT JOIN work_orders w ON w.asset_id=a.id GROUP BY a.id ORDER BY cost DESC LIMIT 8'))
        recent=rows(conn.execute('''SELECT al.*,u.full_name FROM audit_logs al JOIN users u ON u.id=al.user_id ORDER BY al.id DESC LIMIT 10'''))
        health_scores=[]
        asset_ids=rows(conn.execute('SELECT a.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE 1=1'+where,args))
        for x in asset_ids:health_scores.append(_asset_health(conn,x['id'])['score'])
        portfolio_health=round(sum(health_scores)/max(len(health_scores),1),1)
        forecast=_maintenance_forecast(conn,90,site_id)
        return {'kpis':{'total_assets':asset_total,'operating_assets':operating,'assets_under_maintenance':maintenance,'critical_assets':critical,'open_work_orders':openwo,'emergency_work_orders':emergency,'overdue_work_orders':overdue,'completed_work_orders':completed,'pm_compliance':pm_compliance,'mttr':round(avg_repair,1),'mtbf':round(mtbf,1) if mtbf is not None else None,'inventory_value':round(inv_value,2),'low_stock_items':low,'pending_purchase_orders':po_pending,'active_technicians':forecast['technicians'],'safety_incidents':incidents,'open_outages':open_outages,'active_dispatches':active_dispatches,'active_alarms':active_alarms,'critical_alarms':critical_alarms,'open_alarm_incidents':open_alarm_incidents,'telemetry_good_percent':data_quality['good_percent'],'telemetry_bad_quality':data_quality['bad'],'maintenance_cost':round(sum(x['cost'] for x in costs),2),'utility_performance':round(100*operating/max(asset_total,1),1),'asset_health_score':portfolio_health,'forecast_demand_hours_90d':forecast['summary']['demand_hours'],'forecast_peak_utilization':forecast['summary']['peak_utilization_pct'],'parts_shortage_jobs_90d':forecast['summary']['parts_shortage_jobs']},'wo_by_status':statuses,'asset_health':health,'cost_by_asset':costs,'recent_activity':recent}

# ---------- reference / operations / map ----------
@app.get('/api/reference')
def reference(user=Depends(current_user)):
    with db() as conn:
        return {'sites':rows(conn.execute('SELECT * FROM sites ORDER BY name')),'locations':rows(conn.execute('SELECT l.*,s.name site_name FROM locations l JOIN sites s ON s.id=l.site_id ORDER BY s.name,l.name')),'asset_types':rows(conn.execute('SELECT * FROM asset_types ORDER BY name')),'vendors':rows(conn.execute('SELECT * FROM vendors ORDER BY name')),'users':rows(conn.execute('SELECT u.id,u.username,u.full_name,u.department,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.active=1 ORDER BY u.full_name')),'warehouses':rows(conn.execute('SELECT w.*,s.name site_name FROM warehouses w JOIN sites s ON s.id=w.site_id ORDER BY w.name'))}
@app.get('/api/operations')
def operations(user=Depends(current_user)):
    with db() as conn:
        by_domain=rows(conn.execute('''SELECT at.utility_domain domain,COUNT(*) assets,SUM(CASE WHEN a.condition IN ('Warning','Poor','Critical') THEN 1 ELSE 0 END) attention FROM assets a LEFT JOIN asset_types at ON at.id=a.asset_type_id GROUP BY at.utility_domain'''))
        sites=rows(conn.execute('''SELECT s.*,COUNT(DISTINCT a.id) assets,COUNT(DISTINCT CASE WHEN w.status NOT IN ('Closed','Completed','Cancelled') THEN w.id END) open_work FROM sites s LEFT JOIN locations l ON l.site_id=s.id LEFT JOIN assets a ON a.location_id=l.id LEFT JOIN work_orders w ON w.location_id=l.id GROUP BY s.id ORDER BY s.name'''))
        alarms=rows(conn.execute("SELECT a.asset_no,a.name,a.condition,a.status,s.name site_name,l.name location_name FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.condition IN ('Warning','Poor','Critical') OR a.status NOT IN ('Operating','Standby') ORDER BY CASE a.condition WHEN 'Critical' THEN 3 WHEN 'Poor' THEN 2 ELSE 1 END DESC"))
        outages=rows(conn.execute("""SELECT o.*,a.asset_no,a.name asset_name,s.name site_name,w.wo_no FROM asset_outages o JOIN assets a ON a.id=o.asset_id LEFT JOIN sites s ON s.id=o.site_id LEFT JOIN work_orders w ON w.id=o.work_order_id WHERE o.status='Open' ORDER BY o.start_at DESC"""))
        intelligence=_operations_intelligence(conn,None)
        op_alarms=rows(conn.execute("""SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,s.name site_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id LEFT JOIN sites s ON s.id=oa.site_id WHERE oa.status IN ('Open','Acknowledged') ORDER BY CASE oa.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,oa.last_seen_at DESC LIMIT 20"""))
        return {'domains':by_domain,'sites':sites,'alerts':alarms,'open_outages':outages,'intelligence':intelligence,'operational_alarms':op_alarms}
@app.get('/api/map')
def map_data(user=Depends(current_user)):
    with db() as conn:
        sites=rows(conn.execute('''SELECT s.*,COUNT(DISTINCT a.id) assets,COUNT(DISTINCT CASE WHEN w.status NOT IN ('Closed','Completed','Cancelled') THEN w.id END) open_work FROM sites s LEFT JOIN locations l ON l.site_id=s.id LEFT JOIN assets a ON a.location_id=l.id LEFT JOIN work_orders w ON w.location_id=l.id GROUP BY s.id'''))
        for s in sites:
            s['asset_list']=rows(conn.execute('''SELECT a.id,a.asset_no,a.name,a.condition,a.status FROM assets a JOIN locations l ON l.id=a.location_id WHERE l.site_id=? ORDER BY a.asset_no''',(s['id'],)))
            s['technicians']=rows(conn.execute("SELECT u.id,u.full_name FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='technician' AND u.active=1"))
        return sites

# ---------- utility telemetry & operational alarms ----------
@app.get('/api/integrations/api-keys')
def integration_api_keys(user=Depends(require_permission('integration.keys.manage','admin'))):
    with db() as conn:
        return rows(conn.execute("""SELECT k.id,k.key_no,k.name,k.scope,k.active,k.created_at,k.last_used_at,k.expires_at,u.full_name created_by_name
          FROM integration_api_keys k JOIN users u ON u.id=k.created_by ORDER BY k.id DESC"""))

@app.post('/api/integrations/api-keys')
def create_integration_api_key(body:IntegrationKeyIn,user=Depends(require_permission('integration.keys.manage','admin'))):
    if body.expires_at:
        try:
            if _dt(body.expires_at)<=datetime.now():raise HTTPException(422,'API key expiry must be in the future')
        except HTTPException:raise
        except Exception:raise HTTPException(422,'Invalid API key expiry')
    raw='euas_'+secrets.token_urlsafe(32);digest=hashlib.sha256(raw.encode()).hexdigest()
    with db() as conn:
        no=next_no(conn,'integration_api_keys','key_no','KEY-',7001)
        cur=conn.execute("INSERT INTO integration_api_keys(key_no,name,key_hash,scope,active,created_by,created_at,expires_at) VALUES(?,?,?,'telemetry:write',1,?,?,?)",(no,body.name,digest,user['id'],now(),body.expires_at))
        audit(conn,user['id'],'CREATE API KEY','Integrations',no,'',{'name':body.name,'scope':'telemetry:write','expires_at':body.expires_at})
        return {'id':cur.lastrowid,'key_no':no,'name':body.name,'scope':'telemetry:write','api_key':raw,'warning':'This plaintext key is shown once. Store it securely.'}

@app.post('/api/integrations/api-keys/{key_id}/revoke')
def revoke_integration_api_key(key_id:int,user=Depends(require_permission('integration.keys.manage','admin'))):
    with db() as conn:
        key=get_or_404(conn,'SELECT * FROM integration_api_keys WHERE id=?',(key_id,),'Integration API key not found')
        if not key['active']:return {'ok':True,'active':False}
        conn.execute('UPDATE integration_api_keys SET active=0 WHERE id=?',(key_id,));audit(conn,user['id'],'REVOKE API KEY','Integrations',key['key_no'],'Active','Revoked');return {'ok':True,'active':False}

@app.get('/api/telemetry/channels')
def telemetry_channels(asset_id:Optional[int]=None,site_id:Optional[int]=None,q:str='',user=Depends(current_user)):
    sql="SELECT tc.*,a.asset_no,a.name asset_name,s.id site_id,s.site_code,s.name site_name FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE 1=1";args=[]
    if asset_id is not None:sql+=' AND tc.asset_id=?';args.append(asset_id)
    if site_id is not None:sql+=' AND s.id=?';args.append(site_id)
    if q:
        like=f'%{q}%';sql+=' AND (tc.channel_code LIKE ? OR tc.name LIKE ? OR a.asset_no LIKE ?)';args += [like,like,like]
    sql+=' ORDER BY a.asset_no,tc.channel_code'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/telemetry/channels')
def create_telemetry_channel(body:TelemetryChannelIn,user=Depends(require_permission('telemetry.configure','admin','asset_manager','maintenance_manager','planner'))):
    with db() as conn:
        get_or_404(conn,'SELECT id,asset_no FROM assets WHERE id=?',(body.asset_id,),'Asset not found')
        code=(body.channel_code or next_no(conn,'telemetry_channels','channel_code','TEL-',1001)).strip().upper()
        if conn.execute('SELECT id FROM telemetry_channels WHERE channel_code=?',(code,)).fetchone():raise HTTPException(409,'Telemetry channel code already exists')
        cur=conn.execute('INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,source_system,warning_low,critical_low,warning_high,critical_high,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,body.asset_id,body.name,body.metric_type,body.unit,body.source_system,body.warning_low,body.critical_low,body.warning_high,body.critical_high,1 if body.active else 0,now(),now()))
        audit(conn,user['id'],'CREATE','Utilities Operations',code,'',body.model_dump());return {'id':cur.lastrowid,'channel_code':code}

@app.patch('/api/telemetry/channels/{channel_id}')
def update_telemetry_channel(channel_id:int,body:TelemetryChannelPatch,user=Depends(require_permission('telemetry.configure','admin','asset_manager','maintenance_manager','planner'))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    if 'active' in changes:changes['active']=1 if changes['active'] else 0
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM telemetry_channels WHERE id=?',(channel_id,),'Telemetry channel not found')
        if changes:
            conn.execute('UPDATE telemetry_channels SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),channel_id));audit(conn,user['id'],'UPDATE','Utilities Operations',old['channel_code'],old,changes)
        return {'ok':True}

@app.post('/api/telemetry/ingest')
def ingest_telemetry(body:TelemetryIngestIn,user=Depends(telemetry_ingest_principal)):
    summary={'accepted':0,'duplicates':0,'bad_quality':0,'quality_ignored':0,'suppressed':0,'alarms_opened':0,'alarms_updated':0,'alarms_cleared':0,'normal':0,'cbm_events_opened':0,'cbm_events_resolved':0,'cbm_work_orders_created':0,'results':[]}
    with db() as conn:
        if body.idempotency_key:
            previous=conn.execute('SELECT * FROM telemetry_ingest_batches WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
            if previous:
                return {'batch_no':previous['batch_no'],'idempotent_replay':True,'accepted':previous['accepted_count'],'duplicates':previous['duplicate_count'],
                        'bad_quality':previous['bad_quality_count'],'suppressed':previous['suppressed_count'],'alarms_opened':previous['alarms_opened'],
                        'alarms_updated':previous['alarms_updated'],'alarms_cleared':previous['alarms_cleared'],'cbm_events_opened':previous['cbm_events_opened'],
                        'cbm_events_resolved':previous['cbm_events_resolved'],'cbm_work_orders_created':previous['cbm_work_orders_created'],'normal':0,'results':[]}
        batch_no=next_no(conn,'telemetry_ingest_batches','batch_no','TIB-',70001)
        cur=conn.execute("""INSERT INTO telemetry_ingest_batches(batch_no,source_system,idempotency_key,received_count,ingested_by,started_at)
          VALUES(?,?,?,?,?,?)""",(batch_no,body.source_system or 'API',body.idempotency_key,len(body.readings),user['id'],now()))
        batch_id=cur.lastrowid
        for reading in body.readings:
            code=reading.channel_code.strip().upper()
            c=get_or_404(conn,'SELECT * FROM telemetry_channels WHERE channel_code=? AND active=1',(code,),f'Telemetry channel {reading.channel_code} not found or inactive')
            captured=reading.captured_at or now();source=reading.source or body.source_system or c['source_system'] or 'Manual';quality=_normalize_quality(reading.quality)
            if reading.external_id:
                duplicate=conn.execute('SELECT id FROM telemetry_readings WHERE channel_id=? AND external_id=?',(c['id'],reading.external_id)).fetchone()
                if duplicate:
                    summary['duplicates']+=1;summary['results'].append({'channel_code':code,'value':reading.value,'action':'duplicate','external_id':reading.external_id});continue
            reading_cur=conn.execute('INSERT INTO telemetry_readings(channel_id,value,quality,source,captured_at,ingested_at,ingested_by,external_id,batch_id) VALUES(?,?,?,?,?,?,?,?,?)',(c['id'],reading.value,quality,source,captured,now(),user['id'],reading.external_id,batch_id))
            conn.execute('UPDATE telemetry_channels SET last_value=?,last_quality=?,last_reading_at=?,updated_at=? WHERE id=?',(reading.value,quality,captured,now(),c['id']))
            summary['accepted']+=1
            cbm_results=[]
            if quality!='Good':
                if quality=='Bad':summary['bad_quality']+=1
                summary['quality_ignored']+=1
                result={'action':'quality_ignored','alarm_id':None,'alarm_no':None,'severity':None,'quality':quality}
            else:
                result=_evaluate_telemetry_alarm(conn,dict(c),float(reading.value),captured,user['id'])
                cbm_results=_evaluate_cbm_rules(conn,dict(c),float(reading.value),captured,reading_cur.lastrowid,user['id'])
                summary['cbm_events_opened']+=sum(1 for x in cbm_results if x['action']=='opened')
                summary['cbm_events_resolved']+=sum(1 for x in cbm_results if x['action']=='resolved')
                summary['cbm_work_orders_created']+=sum(1 for x in cbm_results if x.get('work_order'))
            summary['results'].append({'channel_code':c['channel_code'],'value':reading.value,'quality':quality,'external_id':reading.external_id,'cbm':cbm_results,**result})
            if result['action'] in ('opened','updated','cleared'):
                summary['alarms_'+result['action']]+=1
            elif result['action']=='suppressed':summary['suppressed']+=1
            elif result['action']=='normal':summary['normal']+=1
        conn.execute("""UPDATE telemetry_ingest_batches SET accepted_count=?,duplicate_count=?,bad_quality_count=?,alarms_opened=?,alarms_updated=?,alarms_cleared=?,suppressed_count=?,cbm_events_opened=?,cbm_events_resolved=?,cbm_work_orders_created=?,completed_at=? WHERE id=?""",
          (summary['accepted'],summary['duplicates'],summary['bad_quality'],summary['alarms_opened'],summary['alarms_updated'],summary['alarms_cleared'],summary['suppressed'],summary['cbm_events_opened'],summary['cbm_events_resolved'],summary['cbm_work_orders_created'],now(),batch_id))
        emit_event(conn,'operations.telemetry.ingested','telemetry',batch_no,{'batch_no':batch_no,'accepted':summary['accepted'],'duplicates':summary['duplicates'],'bad_quality':summary['bad_quality'],'suppressed':summary['suppressed'],'alarms_opened':summary['alarms_opened'],'alarms_updated':summary['alarms_updated'],'alarms_cleared':summary['alarms_cleared'],'cbm_events_opened':summary['cbm_events_opened'],'cbm_work_orders_created':summary['cbm_work_orders_created']})
        audit(conn,user['id'],'INGEST TELEMETRY','Utilities Operations',batch_no,'',{'accepted':summary['accepted'],'duplicates':summary['duplicates'],'bad_quality':summary['bad_quality'],'suppressed':summary['suppressed'],'alarms_opened':summary['alarms_opened'],'alarms_cleared':summary['alarms_cleared']})
        return {'batch_no':batch_no,'idempotent_replay':False,**summary}

@app.get('/api/telemetry/readings')
def telemetry_readings(channel_id:Optional[int]=None,asset_id:Optional[int]=None,hours:int=Query(24,ge=1,le=8760),limit:int=Query(500,ge=1,le=5000),user=Depends(current_user)):
    cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
    sql="SELECT tr.*,tc.channel_code,tc.name channel_name,tc.metric_type,tc.unit,a.asset_no,a.name asset_name FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id JOIN assets a ON a.id=tc.asset_id WHERE tr.captured_at>=?";args=[cutoff]
    if channel_id is not None:sql+=' AND tc.id=?';args.append(channel_id)
    if asset_id is not None:sql+=' AND a.id=?';args.append(asset_id)
    sql+=' ORDER BY tr.captured_at DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/telemetry/batches')
def telemetry_batches(limit:int=Query(100,ge=1,le=500),user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','executive'))):
    with db() as conn:return rows(conn.execute("""SELECT b.*,u.full_name ingested_by_name FROM telemetry_ingest_batches b
      LEFT JOIN users u ON u.id=b.ingested_by ORDER BY b.id DESC LIMIT ?""",(limit,)))

@app.get('/api/telemetry/quality')
def telemetry_quality(hours:int=Query(24,ge=1,le=8760),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        q=_telemetry_quality_summary(conn,hours,site_id)
        cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
        sql="""SELECT tr.source,COUNT(*) count FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id
          JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id
          WHERE tr.captured_at>=?""";args=[cutoff]
        if site_id is not None:sql+=' AND s.id=?';args.append(site_id)
        sql+=' GROUP BY tr.source ORDER BY count DESC';q['sources']=rows(conn.execute(sql,args));return q

@app.get('/api/telemetry/series')
def telemetry_series(channel_id:int,hours:int=Query(24,ge=1,le=8760),bucket_minutes:int=Query(60,ge=1,le=1440),user=Depends(current_user)):
    with db() as conn:
        channel=get_or_404(conn,"""SELECT tc.*,a.asset_no,a.name asset_name FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id WHERE tc.id=?""",(channel_id,),'Telemetry channel not found')
        return {'channel':channel,'hours':hours,'bucket_minutes':bucket_minutes,'points':_telemetry_series(conn,channel_id,hours,bucket_minutes)}

def _validate_cbm_rule_payload(operator, threshold_low, threshold_high, severity, action_type, work_priority):
    if operator not in ('>=','>','<=','<','between','outside'):raise HTTPException(422,'CBM operator must be one of >=, >, <=, <, between, outside')
    if operator in ('>=','>','<=','<') and threshold_low is None:raise HTTPException(422,'threshold_low is required for this CBM operator')
    if operator in ('between','outside'):
        if threshold_low is None or threshold_high is None:raise HTTPException(422,'Both threshold_low and threshold_high are required for range CBM rules')
        if float(threshold_high)<=float(threshold_low):raise HTTPException(422,'threshold_high must be greater than threshold_low')
    if severity not in ('Info','Warning','Critical'):raise HTTPException(422,'CBM severity must be Info, Warning or Critical')
    if action_type not in ('Recommendation','WorkOrder'):raise HTTPException(422,'CBM action_type must be Recommendation or WorkOrder')
    if work_priority not in ('Low','Medium','High','Critical','Emergency'):raise HTTPException(422,'Invalid CBM work priority')

@app.get('/api/reliability/failure-modes')
def failure_modes(active_only:bool=False,user=Depends(current_user)):
    sql="""SELECT fm.*,p.mode_no parent_mode_no,p.name parent_name,
      (SELECT COUNT(*) FROM failure_modes c WHERE c.parent_id=fm.id) child_count,
      (SELECT COUNT(*) FROM asset_fmea af WHERE af.failure_mode_id=fm.id AND af.status<>'Retired') asset_links
      FROM failure_modes fm LEFT JOIN failure_modes p ON p.id=fm.parent_id WHERE 1=1""";args=[]
    if active_only:sql+=' AND fm.active=1'
    sql+=' ORDER BY COALESCE(fm.parent_id,0),fm.id'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/reliability/failure-modes')
def create_failure_mode(body:FailureModeIn,user=Depends(require_permission('reliability.fmea.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        if body.parent_id:get_or_404(conn,'SELECT id FROM failure_modes WHERE id=?',(body.parent_id,),'Parent failure mode not found')
        no=next_no(conn,'failure_modes','mode_no','FM-',1001)
        cur=conn.execute('''INSERT INTO failure_modes(mode_no,parent_id,name,category,description,active,created_by,created_at,updated_by,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)''',(no,body.parent_id,body.name,body.category,body.description,1 if body.active else 0,user['id'],now(),user['id'],now()))
        audit(conn,user['id'],'CREATE FAILURE MODE','Reliability',no,'',body.model_dump())
        emit_event(conn,'maintenance.reliability.failure_mode_created','failure_mode',no,{'mode_no':no,'name':body.name,'parent_id':body.parent_id})
        return {'id':cur.lastrowid,'mode_no':no}

@app.patch('/api/reliability/failure-modes/{mode_id}')
def update_failure_mode(mode_id:int,body:FailureModePatch,user=Depends(require_permission('reliability.fmea.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    changes={k:v for k,v in body.model_dump(exclude_unset=True).items()}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM failure_modes WHERE id=?',(mode_id,),'Failure mode not found')
        if 'parent_id' in changes:
            if changes['parent_id'] is not None:get_or_404(conn,'SELECT id FROM failure_modes WHERE id=?',(changes['parent_id'],),'Parent failure mode not found')
            if _failure_mode_cycle(conn,mode_id,changes['parent_id']):raise HTTPException(409,'Failure-mode hierarchy cannot contain a cycle')
        if 'active' in changes:changes['active']=1 if changes['active'] else 0
        if changes:
            conn.execute('UPDATE failure_modes SET '+','.join(f'{k}=?' for k in changes)+',updated_by=?,updated_at=? WHERE id=?',(*changes.values(),user['id'],now(),mode_id))
            audit(conn,user['id'],'UPDATE FAILURE MODE','Reliability',old['mode_no'],old,changes)
            emit_event(conn,'maintenance.reliability.failure_mode_updated','failure_mode',old['mode_no'],{'mode_no':old['mode_no'],'changes':changes})
        return dict(conn.execute('SELECT * FROM failure_modes WHERE id=?',(mode_id,)).fetchone())

@app.get('/api/reliability/fmea')
def fmea_records(asset_id:Optional[int]=None,status:str='',risk_band:str='',limit:int=Query(500,ge=1,le=2000),user=Depends(current_user)):
    sql="""SELECT f.*,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,fm.category failure_mode_category,
      p.mode_no parent_mode_no,p.name parent_failure_mode,ou.full_name owner_name,ru.full_name last_reviewed_by_name,
      (SELECT COUNT(*) FROM fmea_reviews fr WHERE fr.asset_fmea_id=f.id) review_count,
      (SELECT COUNT(*) FROM work_orders w WHERE w.asset_fmea_id=f.id AND w.status NOT IN ('Closed','Cancelled','Rejected')) open_work_count
      FROM asset_fmea f JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id
      LEFT JOIN failure_modes p ON p.id=fm.parent_id LEFT JOIN users ou ON ou.id=f.owner_id LEFT JOIN users ru ON ru.id=f.last_reviewed_by WHERE 1=1""";args=[]
    if asset_id is not None:sql+=' AND f.asset_id=?';args.append(asset_id)
    if status:sql+=' AND f.status=?';args.append(status)
    if risk_band:sql+=' AND f.risk_band=?';args.append(risk_band)
    sql+=' ORDER BY f.rpn DESC,f.id DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/reliability/summary')
def reliability_fmea_summary(user=Depends(current_user)):
    today=date.today().isoformat()
    with db() as conn:
        bands={r['risk_band']:int(r['count']) for r in conn.execute("SELECT risk_band,COUNT(*) count FROM asset_fmea WHERE status<>'Retired' GROUP BY risk_band")}
        active=int(conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status='Active'").fetchone()[0])
        overdue=int(conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status<>'Retired' AND review_due_date IS NOT NULL AND review_due_date<?",(today,)).fetchone()[0])
        linked_cbm=int(conn.execute('SELECT COUNT(*) FROM cbm_rules WHERE asset_fmea_id IS NOT NULL AND active=1').fetchone()[0])
        open_work=int(conn.execute("SELECT COUNT(*) FROM work_orders WHERE asset_fmea_id IS NOT NULL AND status NOT IN ('Closed','Cancelled','Rejected')").fetchone()[0])
        top=rows(conn.execute("""SELECT f.id,f.fmea_no,f.rpn,f.risk_band,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name
          FROM asset_fmea f JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id WHERE f.status<>'Retired' ORDER BY f.rpn DESC,f.id DESC LIMIT 10"""))
        return {'active_records':active,'overdue_reviews':overdue,'linked_active_cbm_rules':linked_cbm,'open_fmea_work_orders':open_work,'risk_bands':{k:bands.get(k,0) for k in ('Critical','High','Medium','Low')},'top_risk':top}

@app.post('/api/reliability/fmea')
def create_fmea(body:FmeaIn,user=Depends(require_permission('reliability.fmea.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    if body.status not in ('Draft','Active','Mitigated','Retired'):raise HTTPException(422,'Invalid FMEA status')
    rpn,band=_fmea_risk(body.severity,body.occurrence,body.detectability)
    with db() as conn:
        get_or_404(conn,'SELECT id FROM assets WHERE id=?',(body.asset_id,),'Asset not found')
        mode=get_or_404(conn,'SELECT * FROM failure_modes WHERE id=?',(body.failure_mode_id,),'Failure mode not found')
        if not mode['active']:raise HTTPException(409,'Inactive failure mode cannot be linked to a new FMEA record')
        if body.owner_id:get_or_404(conn,'SELECT id FROM users WHERE id=? AND active=1',(body.owner_id,),'FMEA owner not found or inactive')
        if conn.execute('SELECT id FROM asset_fmea WHERE asset_id=? AND failure_mode_id=?',(body.asset_id,body.failure_mode_id)).fetchone():raise HTTPException(409,'This asset is already linked to the selected failure mode')
        no=next_no(conn,'asset_fmea','fmea_no','FMEA-',1001)
        cur=conn.execute('''INSERT INTO asset_fmea(fmea_no,asset_id,failure_mode_id,function_description,failure_effect,failure_cause,current_controls,recommended_action,severity,occurrence,detectability,rpn,risk_band,status,owner_id,review_due_date,created_by,created_at,updated_by,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,body.asset_id,body.failure_mode_id,body.function_description,body.failure_effect,body.failure_cause,body.current_controls,body.recommended_action,body.severity,body.occurrence,body.detectability,rpn,band,body.status,body.owner_id,body.review_due_date,user['id'],now(),user['id'],now()))
        audit(conn,user['id'],'CREATE FMEA','Reliability',no,'',body.model_dump()|{'rpn':rpn,'risk_band':band})
        emit_event(conn,'maintenance.reliability.fmea_created','asset_fmea',no,{'fmea_no':no,'asset_id':body.asset_id,'mode_no':mode['mode_no'],'rpn':rpn,'risk_band':band})
        return {'id':cur.lastrowid,'fmea_no':no,'rpn':rpn,'risk_band':band}

@app.patch('/api/reliability/fmea/{fmea_id}')
def update_fmea(fmea_id:int,body:FmeaPatch,user=Depends(require_permission('reliability.fmea.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    changes={k:v for k,v in body.model_dump(exclude_unset=True).items()}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM asset_fmea WHERE id=?',(fmea_id,),'FMEA record not found')
        merged={**dict(old),**changes}
        if merged['status'] not in ('Draft','Active','Mitigated','Retired'):raise HTTPException(422,'Invalid FMEA status')
        if merged.get('owner_id'):get_or_404(conn,'SELECT id FROM users WHERE id=? AND active=1',(merged['owner_id'],),'FMEA owner not found or inactive')
        rpn,band=_fmea_risk(merged['severity'],merged['occurrence'],merged['detectability'])
        changes['rpn']=rpn;changes['risk_band']=band
        conn.execute('UPDATE asset_fmea SET '+','.join(f'{k}=?' for k in changes)+',updated_by=?,updated_at=? WHERE id=?',(*changes.values(),user['id'],now(),fmea_id))
        audit(conn,user['id'],'UPDATE FMEA','Reliability',old['fmea_no'],old,changes)
        emit_event(conn,'maintenance.reliability.fmea_updated','asset_fmea',old['fmea_no'],{'fmea_no':old['fmea_no'],'rpn':rpn,'risk_band':band,'changes':changes})
        return dict(conn.execute('SELECT * FROM asset_fmea WHERE id=?',(fmea_id,)).fetchone())

@app.post('/api/reliability/fmea/{fmea_id}/review')
def review_fmea(fmea_id:int,body:FmeaReviewIn,user=Depends(require_permission('reliability.fmea.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    rpn,band=_fmea_risk(body.severity,body.occurrence,body.detectability);stamp=now()
    if body.status is not None and body.status not in ('Draft','Active','Mitigated','Retired'):raise HTTPException(422,'Invalid FMEA status')
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM asset_fmea WHERE id=?',(fmea_id,),'FMEA record not found')
        no=next_no(conn,'fmea_reviews','review_no','FREV-',1001)
        conn.execute('''INSERT INTO fmea_reviews(review_no,asset_fmea_id,old_severity,old_occurrence,old_detectability,old_rpn,new_severity,new_occurrence,new_detectability,new_rpn,notes,reviewed_by,reviewed_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,fmea_id,old['severity'],old['occurrence'],old['detectability'],old['rpn'],body.severity,body.occurrence,body.detectability,rpn,body.notes,user['id'],stamp))
        status_value=body.status if body.status is not None else old['status']
        due=body.review_due_date if body.review_due_date is not None else old['review_due_date']
        conn.execute('''UPDATE asset_fmea SET severity=?,occurrence=?,detectability=?,rpn=?,risk_band=?,status=?,review_due_date=?,last_reviewed_at=?,last_reviewed_by=?,updated_by=?,updated_at=? WHERE id=?''',(body.severity,body.occurrence,body.detectability,rpn,band,status_value,due,stamp,user['id'],user['id'],stamp,fmea_id))
        audit(conn,user['id'],'REVIEW FMEA','Reliability',old['fmea_no'],{'severity':old['severity'],'occurrence':old['occurrence'],'detectability':old['detectability'],'rpn':old['rpn']},{'severity':body.severity,'occurrence':body.occurrence,'detectability':body.detectability,'rpn':rpn,'risk_band':band,'notes':body.notes})
        emit_event(conn,'maintenance.reliability.fmea_reviewed','asset_fmea',old['fmea_no'],{'fmea_no':old['fmea_no'],'review_no':no,'old_rpn':old['rpn'],'new_rpn':rpn,'risk_band':band})
        return {'review_no':no,'fmea_no':old['fmea_no'],'rpn':rpn,'risk_band':band}

@app.get('/api/reliability/fmea/{fmea_id}/reviews')
def fmea_reviews(fmea_id:int,user=Depends(current_user)):
    with db() as conn:
        get_or_404(conn,'SELECT id FROM asset_fmea WHERE id=?',(fmea_id,),'FMEA record not found')
        return rows(conn.execute('''SELECT fr.*,u.full_name reviewed_by_name FROM fmea_reviews fr JOIN users u ON u.id=fr.reviewed_by WHERE fr.asset_fmea_id=? ORDER BY fr.id DESC''',(fmea_id,)))

@app.post('/api/reliability/fmea/{fmea_id}/work-order')
def fmea_to_work(fmea_id:int,body:FmeaWorkOrderIn,user=Depends(require_permission('work.write','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    if body.priority not in ('Low','Medium','High','Critical','Emergency'):raise HTTPException(422,'Invalid work priority')
    with db() as conn:
        rec=_fmea_record(conn,fmea_id)
        existing=one(conn.execute("SELECT id,wo_no,status FROM work_orders WHERE asset_fmea_id=? AND status NOT IN ('Closed','Cancelled','Rejected') ORDER BY id DESC LIMIT 1",(fmea_id,)))
        if existing:raise HTTPException(409,detail={'message':'An active work order already exists for this FMEA record','work_order':existing['wo_no'],'status':existing['status']})
        asset=get_or_404(conn,'SELECT id,location_id FROM assets WHERE id=?',(rec['asset_id'],),'Asset not found')
        no=next_no(conn,'work_orders','wo_no','WO-',10026);finish=body.target_finish or (date.today()+timedelta(days=1 if body.priority in ('Critical','Emergency') else 5)).isoformat()
        title=f"FMEA: {rec['failure_mode_name']} — {rec['asset_no']}"
        desc=f"Generated from {rec['fmea_no']} / {rec['mode_no']}. Current RPN {rec['rpn']} ({rec['risk_band']}). Effect: {rec['failure_effect']}. Cause: {rec['failure_cause']}. {body.notes}".strip()
        instructions=(rec.get('recommended_action') or 'Perform the reliability action defined by the FMEA review and capture completion evidence.').strip()
        cur=conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,asset_fmea_id,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,instructions,created_at,updated_at)
          VALUES(?,?,?,?,?,?,'Submitted','Reliability / FMEA',?,?,?,?,?,?,?,?,?,?,?)''',(no,title,desc,rec['asset_id'],asset.get('location_id'),body.priority,rec['mode_no'],fmea_id,user['id'],body.assigned_to,body.supervisor_id,date.today().isoformat(),finish,2.0,instructions,now(),now()))
        _ensure_work_sla(conn,cur.lastrowid)
        create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f"Approve {no} — {title}",user['id'],assigned_user_id=body.supervisor_id,assigned_role=None if body.supervisor_id else 'maintenance_manager')
        workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'FMEA GENERATED','', 'Submitted',user['id'],rec['fmea_no'])
        audit(conn,user['id'],'CREATE FMEA WORK','Reliability',rec['fmea_no'],'',{'work_order':no,'rpn':rec['rpn'],'risk_band':rec['risk_band']})
        emit_event(conn,'maintenance.reliability.fmea_work_created','asset_fmea',rec['fmea_no'],{'fmea_no':rec['fmea_no'],'work_order':no,'asset_id':rec['asset_id'],'rpn':rec['rpn']})
        return {'id':cur.lastrowid,'wo_no':no,'status':'Submitted'}

@app.get('/api/reliability/rcm')
def rcm_strategies(asset_id:Optional[int]=None,asset_fmea_id:Optional[int]=None,status:str='',strategy_type:str='',user=Depends(current_user)):
    sql="""SELECT r.*,f.fmea_no,f.asset_id,f.rpn,f.risk_band,f.status fmea_status,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,
      ou.full_name owner_name,ap.full_name approved_by_name,ac.full_name activated_by_name,cb.rule_no linked_cbm_rule_no,cb.name linked_cbm_rule_name,
      pm.pm_no linked_pm_no,pm.name linked_pm_name,(SELECT COUNT(*) FROM rcm_strategy_reviews rr WHERE rr.strategy_id=r.id) review_count
      FROM rcm_strategies r JOIN asset_fmea f ON f.id=r.asset_fmea_id JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id
      LEFT JOIN users ou ON ou.id=r.owner_id LEFT JOIN users ap ON ap.id=r.approved_by LEFT JOIN users ac ON ac.id=r.activated_by
      LEFT JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id LEFT JOIN maintenance_plans pm ON pm.id=r.linked_pm_plan_id WHERE 1=1""";args=[]
    if asset_id is not None:sql+=' AND f.asset_id=?';args.append(asset_id)
    if asset_fmea_id is not None:sql+=' AND r.asset_fmea_id=?';args.append(asset_fmea_id)
    if status:sql+=' AND r.status=?';args.append(status)
    if strategy_type:sql+=' AND r.strategy_type=?';args.append(strategy_type)
    sql+=" ORDER BY CASE r.status WHEN 'Review' THEN 0 WHEN 'Active' THEN 1 WHEN 'Approved' THEN 2 WHEN 'Draft' THEN 3 ELSE 4 END,f.rpn DESC,r.id DESC"
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/reliability/rcm/summary')
def rcm_summary(user=Depends(current_user)):
    today=date.today().isoformat()
    with db() as conn:
        eligible=int(conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status<>'Retired'").fetchone()[0])
        covered=int(conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status IN ('Approved','Active')").fetchone()[0])
        active=int(conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status='Active'").fetchone()[0])
        in_review=int(conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status='Review'").fetchone()[0])
        overdue=int(conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status='Active' AND review_due_date IS NOT NULL AND review_due_date<?",(today,)).fetchone()[0])
        critical_uncovered=int(conn.execute("""SELECT COUNT(*) FROM asset_fmea f WHERE f.status<>'Retired' AND f.risk_band='Critical' AND NOT EXISTS(
          SELECT 1 FROM rcm_strategies r WHERE r.asset_fmea_id=f.id AND r.status IN ('Approved','Active'))""").fetchone()[0])
        critical_assets=int(conn.execute("SELECT COUNT(DISTINCT asset_id) FROM asset_fmea WHERE status<>'Retired' AND risk_band='Critical'").fetchone()[0])
        critical_cbm_assets=int(conn.execute("""SELECT COUNT(DISTINCT f.asset_id) FROM asset_fmea f JOIN rcm_strategies r ON r.asset_fmea_id=f.id
          JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id WHERE f.status<>'Retired' AND f.risk_band='Critical' AND r.status='Active' AND r.strategy_type='Condition-Based' AND cb.active=1""").fetchone()[0])
        by_type={r['strategy_type']:int(r['count']) for r in conn.execute("SELECT strategy_type,COUNT(*) count FROM rcm_strategies WHERE status='Active' GROUP BY strategy_type")}
        return {'eligible_fmea':eligible,'covered_fmea':covered,'strategy_coverage_pct':round(100*covered/max(eligible,1),1),'active_strategies':active,'in_review':in_review,
                'overdue_reviews':overdue,'critical_uncovered':critical_uncovered,'critical_asset_count':critical_assets,
                'critical_cbm_coverage_pct':round(100*critical_cbm_assets/max(critical_assets,1),1),'active_by_type':by_type}

@app.post('/api/reliability/rcm')
def create_rcm_strategy(body:RcmStrategyIn,user=Depends(require_permission('reliability.rcm.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        fmea=_fmea_record(conn,body.asset_fmea_id,active_required=True)
        if conn.execute('SELECT id FROM rcm_strategies WHERE asset_fmea_id=?',(body.asset_fmea_id,)).fetchone():raise HTTPException(409,'This FMEA record already has an RCM strategy')
        if body.owner_id:get_or_404(conn,'SELECT id FROM users WHERE id=? AND active=1',(body.owner_id,),'RCM owner not found or inactive')
        data=body.model_dump();_validate_rcm_payload(conn,fmea,data,False)
        due=body.review_due_date or _rcm_default_review_due(fmea);no=next_no(conn,'rcm_strategies','strategy_no','RCM-',1001);stamp=now()
        cur=conn.execute("""INSERT INTO rcm_strategies(strategy_no,asset_fmea_id,functional_failure,consequence_classification,strategy_type,task_description,justification,interval_days,linked_cbm_rule_id,linked_pm_plan_id,status,owner_id,review_due_date,created_by,created_at,updated_by,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,'Draft',?,?,?,?,?,?)""",(no,body.asset_fmea_id,body.functional_failure,body.consequence_classification,body.strategy_type,body.task_description,body.justification,body.interval_days,body.linked_cbm_rule_id,body.linked_pm_plan_id,body.owner_id,due,user['id'],stamp,user['id'],stamp))
        workflow_event(conn,'Reliability','rcm_strategy',cur.lastrowid,no,'CREATE','', 'Draft',user['id'],body.justification)
        audit(conn,user['id'],'CREATE RCM','Reliability',no,'',data|{'review_due_date':due});emit_event(conn,'maintenance.reliability.rcm_created','rcm_strategy',no,{'strategy_no':no,'fmea_no':fmea['fmea_no'],'strategy_type':body.strategy_type,'risk_band':fmea['risk_band']})
        return {'id':cur.lastrowid,'strategy_no':no,'status':'Draft','review_due_date':due}

@app.patch('/api/reliability/rcm/{strategy_id}')
def update_rcm_strategy(strategy_id:int,body:RcmStrategyPatch,user=Depends(require_permission('reliability.rcm.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    changes={k:v for k,v in body.model_dump(exclude_unset=True).items()}
    with db() as conn:
        old=_rcm_strategy_record(conn,strategy_id)
        if old['status']!='Draft':raise HTTPException(409,'Only Draft RCM strategies can be edited; use the review workflow for approved or active strategies')
        merged={**dict(old),**changes};fmea=_fmea_record(conn,old['asset_fmea_id'],active_required=True)
        if merged.get('owner_id'):get_or_404(conn,'SELECT id FROM users WHERE id=? AND active=1',(merged['owner_id'],),'RCM owner not found or inactive')
        _validate_rcm_payload(conn,fmea,merged,False)
        if changes:
            conn.execute('UPDATE rcm_strategies SET '+','.join(f'{k}=?' for k in changes)+',updated_by=?,updated_at=? WHERE id=?',(*changes.values(),user['id'],now(),strategy_id))
            audit(conn,user['id'],'UPDATE RCM','Reliability',old['strategy_no'],old,changes);emit_event(conn,'maintenance.reliability.rcm_updated','rcm_strategy',old['strategy_no'],{'strategy_no':old['strategy_no'],'changes':changes})
        return _rcm_strategy_record(conn,strategy_id)

@app.post('/api/reliability/rcm/{strategy_id}/submit')
def submit_rcm_strategy(strategy_id:int,body:RcmActionIn,user=Depends(require_permission('reliability.rcm.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        rec=_rcm_strategy_record(conn,strategy_id)
        if rec['status']!='Draft':raise HTTPException(409,f"RCM strategy is {rec['status']}, not Draft")
        fmea=_fmea_record(conn,rec['asset_fmea_id'],active_required=True);_validate_rcm_payload(conn,fmea,rec,True)
        conn.execute("UPDATE rcm_strategies SET status='Review',updated_by=?,updated_at=? WHERE id=?",(user['id'],now(),strategy_id))
        ap=create_approval(conn,'Reliability','rcm_strategy',strategy_id,rec['strategy_no'],f"Approve {rec['strategy_no']} — {rec['strategy_type']} for {rec['asset_no']}",user['id'],assigned_role='maintenance_manager')
        workflow_event(conn,'Reliability','rcm_strategy',strategy_id,rec['strategy_no'],'SUBMIT',rec['status'],'Review',user['id'],body.notes)
        audit(conn,user['id'],'SUBMIT RCM','Reliability',rec['strategy_no'],rec['status'],'Review');emit_event(conn,'maintenance.reliability.rcm_submitted','rcm_strategy',rec['strategy_no'],{'strategy_no':rec['strategy_no'],'approval_no':ap['approval_no'],'fmea_no':rec['fmea_no']})
        return {'strategy_no':rec['strategy_no'],'status':'Review','approval_no':ap['approval_no']}

@app.post('/api/reliability/rcm/{strategy_id}/activate')
def activate_rcm_strategy(strategy_id:int,body:RcmActionIn,user=Depends(require_permission('reliability.rcm.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        rec=_rcm_strategy_record(conn,strategy_id)
        if rec['status']!='Approved':raise HTTPException(409,f"RCM strategy is {rec['status']}, not Approved")
        fmea=_fmea_record(conn,rec['asset_fmea_id'],active_required=True);_validate_rcm_payload(conn,fmea,rec,True);stamp=now()
        conn.execute("UPDATE rcm_strategies SET status='Active',activated_by=?,activated_at=?,updated_by=?,updated_at=? WHERE id=?",(user['id'],stamp,user['id'],stamp,strategy_id))
        workflow_event(conn,'Reliability','rcm_strategy',strategy_id,rec['strategy_no'],'ACTIVATE','Approved','Active',user['id'],body.notes);audit(conn,user['id'],'ACTIVATE RCM','Reliability',rec['strategy_no'],'Approved','Active')
        emit_event(conn,'maintenance.reliability.rcm_activated','rcm_strategy',rec['strategy_no'],{'strategy_no':rec['strategy_no'],'strategy_type':rec['strategy_type'],'review_due_date':rec['review_due_date']})
        return {'strategy_no':rec['strategy_no'],'status':'Active'}

@app.post('/api/reliability/rcm/{strategy_id}/review')
def review_rcm_strategy(strategy_id:int,body:RcmReviewIn,user=Depends(require_permission('reliability.rcm.manage','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    outcome=body.outcome.strip().title()
    if outcome not in ('Continue','Revise','Retire'):raise HTTPException(422,'RCM review outcome must be Continue, Revise or Retire')
    with db() as conn:
        rec=_rcm_strategy_record(conn,strategy_id)
        if rec['status'] not in ('Active','Approved'):raise HTTPException(409,'Only Approved or Active RCM strategies can be formally reviewed')
        fmea=_fmea_record(conn,rec['asset_fmea_id'],active_required=False);stamp=now();due=body.review_due_date or _rcm_default_review_due(fmea)
        target=rec['status'] if outcome=='Continue' else ('Draft' if outcome=='Revise' else 'Retired')
        no=next_no(conn,'rcm_strategy_reviews','review_no','RREV-',1001)
        conn.execute('''INSERT INTO rcm_strategy_reviews(review_no,strategy_id,old_status,outcome,notes,previous_review_due_date,next_review_due_date,reviewed_by,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?)''',(no,strategy_id,rec['status'],outcome,body.notes,rec.get('review_due_date'),None if target=='Retired' else due,user['id'],stamp))
        if target=='Draft':
            conn.execute("UPDATE rcm_strategies SET status='Draft',review_due_date=?,approved_by=NULL,approved_at=NULL,activated_by=NULL,activated_at=NULL,last_decision_comments=?,updated_by=?,updated_at=? WHERE id=?",(due,body.notes,user['id'],stamp,strategy_id))
        elif target=='Retired':
            conn.execute("UPDATE rcm_strategies SET status='Retired',review_due_date=NULL,retired_by=?,retired_at=?,last_decision_comments=?,updated_by=?,updated_at=? WHERE id=?",(user['id'],stamp,body.notes,user['id'],stamp,strategy_id))
        else:
            conn.execute("UPDATE rcm_strategies SET review_due_date=?,last_decision_comments=?,updated_by=?,updated_at=? WHERE id=?",(due,body.notes,user['id'],stamp,strategy_id))
        workflow_event(conn,'Reliability','rcm_strategy',strategy_id,rec['strategy_no'],'REVIEW',rec['status'],target,user['id'],body.notes);audit(conn,user['id'],'REVIEW RCM','Reliability',rec['strategy_no'],rec['status'],{'outcome':outcome,'status':target,'review_due_date':None if target=='Retired' else due})
        emit_event(conn,'maintenance.reliability.rcm_reviewed','rcm_strategy',rec['strategy_no'],{'strategy_no':rec['strategy_no'],'review_no':no,'outcome':outcome,'status':target})
        return {'review_no':no,'strategy_no':rec['strategy_no'],'status':target,'review_due_date':None if target=='Retired' else due}

@app.get('/api/reliability/rcm/{strategy_id}/reviews')
def rcm_strategy_reviews(strategy_id:int,user=Depends(current_user)):
    with db() as conn:
        _rcm_strategy_record(conn,strategy_id)
        return rows(conn.execute('''SELECT rr.*,u.full_name reviewed_by_name FROM rcm_strategy_reviews rr JOIN users u ON u.id=rr.reviewed_by WHERE rr.strategy_id=? ORDER BY rr.id DESC''',(strategy_id,)))

@app.get('/api/cbm/rules')
def cbm_rules(channel_id:Optional[int]=None,asset_id:Optional[int]=None,active_only:bool=False,user=Depends(current_user)):
    sql="""SELECT r.*,tc.channel_code,tc.name channel_name,tc.metric_type,tc.unit,a.asset_no,a.name asset_name,
      st.consecutive_hits,st.last_value,st.last_quality,st.last_evaluated_at,st.last_triggered_at,st.active_event_id,
      cu.full_name created_by_name,uu.full_name updated_by_name,f.fmea_no,f.rpn fmea_rpn,f.risk_band fmea_risk_band,fm.mode_no failure_mode_no,fm.name failure_mode_name
      FROM cbm_rules r JOIN telemetry_channels tc ON tc.id=r.channel_id JOIN assets a ON a.id=tc.asset_id
      LEFT JOIN cbm_rule_state st ON st.rule_id=r.id LEFT JOIN users cu ON cu.id=r.created_by LEFT JOIN users uu ON uu.id=r.updated_by
      LEFT JOIN asset_fmea f ON f.id=r.asset_fmea_id LEFT JOIN failure_modes fm ON fm.id=f.failure_mode_id WHERE 1=1""";args=[]
    if channel_id is not None:sql+=' AND r.channel_id=?';args.append(channel_id)
    if asset_id is not None:sql+=' AND a.id=?';args.append(asset_id)
    if active_only:sql+=' AND r.active=1'
    sql+=' ORDER BY r.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/cbm/rules')
def create_cbm_rule(body:CbmRuleIn,user=Depends(require_permission('cbm.rules.manage','admin','asset_manager','maintenance_manager','planner'))):
    _validate_cbm_rule_payload(body.operator,body.threshold_low,body.threshold_high,body.severity,body.action_type,body.work_priority)
    with db() as conn:
        channel=get_or_404(conn,'SELECT * FROM telemetry_channels WHERE id=?',(body.channel_id,),'Telemetry channel not found')
        if body.asset_fmea_id:_fmea_record(conn,body.asset_fmea_id,channel['asset_id'])
        no=next_no(conn,'cbm_rules','rule_no','CBR-',1001)
        cur=conn.execute("""INSERT INTO cbm_rules(rule_no,name,channel_id,operator,threshold_low,threshold_high,consecutive_readings,cooldown_minutes,severity,action_type,work_priority,instructions,asset_fmea_id,active,created_by,created_at,updated_by,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(no,body.name,body.channel_id,body.operator,body.threshold_low,body.threshold_high,body.consecutive_readings,body.cooldown_minutes,body.severity,body.action_type,body.work_priority,body.instructions,body.asset_fmea_id,1 if body.active else 0,user['id'],now(),user['id'],now()))
        conn.execute('INSERT INTO cbm_rule_state(rule_id,consecutive_hits) VALUES(?,0)',(cur.lastrowid,))
        audit(conn,user['id'],'CREATE CBM RULE','Condition-Based Maintenance',no,'',body.model_dump())
        emit_event(conn,'maintenance.cbm.rule_created','cbm_rule',no,{'rule_no':no,'channel_code':channel['channel_code'],'action_type':body.action_type,'operator':body.operator})
        return {'id':cur.lastrowid,'rule_no':no}

@app.patch('/api/cbm/rules/{rule_id}')
def update_cbm_rule(rule_id:int,body:CbmRulePatch,user=Depends(require_permission('cbm.rules.manage','admin','asset_manager','maintenance_manager','planner'))):
    changes={k:v for k,v in body.model_dump(exclude_unset=True).items()}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM cbm_rules WHERE id=?',(rule_id,),'CBM rule not found')
        merged={**old,**changes}
        _validate_cbm_rule_payload(merged['operator'],merged.get('threshold_low'),merged.get('threshold_high'),merged['severity'],merged['action_type'],merged['work_priority'])
        channel=get_or_404(conn,'SELECT asset_id FROM telemetry_channels WHERE id=?',(old['channel_id'],),'Telemetry channel not found')
        if merged.get('asset_fmea_id'):_fmea_record(conn,merged['asset_fmea_id'],channel['asset_id'])
        if 'active' in changes:changes['active']=1 if changes['active'] else 0
        if changes:
            conn.execute('UPDATE cbm_rules SET '+','.join(f'{k}=?' for k in changes)+',updated_by=?,updated_at=? WHERE id=?',(*changes.values(),user['id'],now(),rule_id))
            if any(k in changes for k in ('operator','threshold_low','threshold_high','consecutive_readings','active')):
                conn.execute('UPDATE cbm_rule_state SET consecutive_hits=0 WHERE rule_id=?',(rule_id,))
            audit(conn,user['id'],'UPDATE CBM RULE','Condition-Based Maintenance',old['rule_no'],old,changes)
            emit_event(conn,'maintenance.cbm.rule_updated','cbm_rule',old['rule_no'],{'rule_no':old['rule_no'],'changes':changes})
        return dict(conn.execute('SELECT * FROM cbm_rules WHERE id=?',(rule_id,)).fetchone())

@app.post('/api/cbm/rules/{rule_id}/test')
def test_cbm_rule(rule_id:int,value:float,user=Depends(current_user)):
    with db() as conn:
        rule=get_or_404(conn,'SELECT r.*,tc.channel_code,tc.unit FROM cbm_rules r JOIN telemetry_channels tc ON tc.id=r.channel_id WHERE r.id=?',(rule_id,),'CBM rule not found')
        return {'rule_no':rule['rule_no'],'value':value,'matches':bool(_cbm_condition(rule,value)),'condition':_cbm_rule_threshold_text(rule),'consecutive_required':rule['consecutive_readings'],'side_effects':False}

@app.get('/api/cbm/events')
def cbm_events(status:str='',asset_id:Optional[int]=None,rule_id:Optional[int]=None,limit:int=Query(200,ge=1,le=1000),user=Depends(current_user)):
    sql="""SELECT e.*,r.rule_no,r.name rule_name,r.action_type,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,w.wo_no,
      ack.full_name acknowledged_by_name,f.fmea_no,f.rpn fmea_rpn,f.risk_band fmea_risk_band,fm.mode_no failure_mode_no,fm.name failure_mode_name FROM cbm_events e JOIN cbm_rules r ON r.id=e.rule_id JOIN telemetry_channels tc ON tc.id=e.channel_id
      JOIN assets a ON a.id=e.asset_id LEFT JOIN work_orders w ON w.id=e.work_order_id LEFT JOIN users ack ON ack.id=e.acknowledged_by
      LEFT JOIN asset_fmea f ON f.id=e.asset_fmea_id LEFT JOIN failure_modes fm ON fm.id=f.failure_mode_id WHERE 1=1""";args=[]
    if status:sql+=' AND e.status=?';args.append(status)
    if asset_id is not None:sql+=' AND e.asset_id=?';args.append(asset_id)
    if rule_id is not None:sql+=' AND e.rule_id=?';args.append(rule_id)
    sql+=' ORDER BY e.id DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/cbm/events/{event_id}/acknowledge')
def acknowledge_cbm_event(event_id:int,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        event=get_or_404(conn,'SELECT * FROM cbm_events WHERE id=?',(event_id,),'CBM event not found')
        if event['status']=='Resolved':raise HTTPException(409,'CBM event is already resolved')
        if event['status']=='Acknowledged':return {'ok':True,'status':'Acknowledged'}
        conn.execute("UPDATE cbm_events SET status='Acknowledged',acknowledged_by=?,acknowledged_at=? WHERE id=?",(user['id'],now(),event_id))
        audit(conn,user['id'],'ACKNOWLEDGE CBM EVENT','Condition-Based Maintenance',event['event_no'],event['status'],'Acknowledged')
        return {'ok':True,'status':'Acknowledged'}

@app.post('/api/cbm/events/{event_id}/resolve')
def resolve_cbm_event(event_id:int,body:CbmEventResolveIn,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        event=get_or_404(conn,'SELECT * FROM cbm_events WHERE id=?',(event_id,),'CBM event not found')
        if event['status']=='Resolved':return {'ok':True,'status':'Resolved'}
        conn.execute("UPDATE cbm_events SET status='Resolved',resolved_at=?,resolution_reason=? WHERE id=?",(now(),body.reason,event_id))
        conn.execute('UPDATE cbm_rule_state SET active_event_id=NULL,consecutive_hits=0 WHERE rule_id=?',(event['rule_id'],))
        audit(conn,user['id'],'RESOLVE CBM EVENT','Condition-Based Maintenance',event['event_no'],event['status'],{'status':'Resolved','reason':body.reason})
        emit_event(conn,'maintenance.cbm.event_resolved','cbm_event',event['event_no'],{'event_no':event['event_no'],'reason':body.reason,'manual':True})
        return {'ok':True,'status':'Resolved'}

@app.get('/api/alarm-suppressions')
def alarm_suppressions(active_only:bool=False,site_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT sp.*,s.site_code,s.name site_name,a.asset_no,a.name asset_name,tc.channel_code,tc.name channel_name,u.full_name created_by_name
      FROM alarm_suppressions sp LEFT JOIN sites s ON s.id=sp.site_id LEFT JOIN assets a ON a.id=sp.asset_id
      LEFT JOIN telemetry_channels tc ON tc.id=sp.channel_id LEFT JOIN users u ON u.id=sp.created_by WHERE 1=1""";args=[]
    if active_only:sql+=' AND sp.active=1 AND sp.start_at<=? AND sp.end_at>=?';args += [now(),now()]
    if site_id is not None:sql+=' AND (sp.site_id=? OR s.id=?)';args += [site_id,site_id]
    sql+=' ORDER BY sp.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/alarm-suppressions')
def create_alarm_suppression(body:AlarmSuppressionIn,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    if not any((body.site_id,body.asset_id,body.channel_id)):raise HTTPException(422,'Suppression requires a site, asset or telemetry channel scope')
    try:start=_dt(body.start_at);end=_dt(body.end_at)
    except Exception:raise HTTPException(422,'Invalid suppression start/end date-time')
    if end<=start:raise HTTPException(422,'Suppression end must be after start')
    with db() as conn:
        if body.site_id:get_or_404(conn,'SELECT id FROM sites WHERE id=?',(body.site_id,),'Site not found')
        if body.asset_id:get_or_404(conn,'SELECT id FROM assets WHERE id=?',(body.asset_id,),'Asset not found')
        if body.channel_id:get_or_404(conn,'SELECT id FROM telemetry_channels WHERE id=?',(body.channel_id,),'Telemetry channel not found')
        no=next_no(conn,'alarm_suppressions','suppression_no','SUP-',65001)
        cur=conn.execute("""INSERT INTO alarm_suppressions(suppression_no,site_id,asset_id,channel_id,reason,start_at,end_at,active,created_by,created_at)
          VALUES(?,?,?,?,?,?,?,1,?,?)""",(no,body.site_id,body.asset_id,body.channel_id,body.reason,start.isoformat(timespec='seconds'),end.isoformat(timespec='seconds'),user['id'],now()))
        audit(conn,user['id'],'CREATE SUPPRESSION','Utilities Operations',no,'',body.model_dump());emit_event(conn,'operations.alarm_suppression.created','alarm_suppression',no,{'suppression_no':no,'reason':body.reason,'start_at':body.start_at,'end_at':body.end_at})
        return {'id':cur.lastrowid,'suppression_no':no}

@app.post('/api/alarm-suppressions/{suppression_id}/deactivate')
def deactivate_alarm_suppression(suppression_id:int,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        sp=get_or_404(conn,'SELECT * FROM alarm_suppressions WHERE id=?',(suppression_id,),'Suppression not found')
        if not sp['active']:return {'ok':True,'active':False}
        conn.execute('UPDATE alarm_suppressions SET active=0 WHERE id=?',(suppression_id,));audit(conn,user['id'],'DEACTIVATE SUPPRESSION','Utilities Operations',sp['suppression_no'],'Active','Inactive');return {'ok':True,'active':False}

@app.get('/api/alarm-shelves')
def alarm_shelves(status:str='',alarm_id:Optional[int]=None,active_only:bool=False,site_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT sh.*,oa.alarm_no,oa.severity,oa.status alarm_status,a.asset_no,a.name asset_name,tc.channel_code,tc.name channel_name,
      req.full_name requested_by_name,apr.full_name approved_by_name,rej.full_name rejected_by_name,rev.full_name revoked_by_name
      FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id JOIN assets a ON a.id=oa.asset_id JOIN telemetry_channels tc ON tc.id=oa.channel_id
      JOIN users req ON req.id=sh.requested_by LEFT JOIN users apr ON apr.id=sh.approved_by LEFT JOIN users rej ON rej.id=sh.rejected_by LEFT JOIN users rev ON rev.id=sh.revoked_by WHERE 1=1""";args=[]
    if status:sql+=' AND sh.status=?';args.append(status)
    if alarm_id is not None:sql+=' AND sh.alarm_id=?';args.append(alarm_id)
    if active_only:sql+=" AND sh.status='Approved' AND sh.start_at<=? AND sh.end_at>? AND oa.status IN ('Open','Acknowledged')";args += [now(),now()]
    if site_id is not None:sql+=' AND oa.site_id=?';args.append(site_id)
    sql+=' ORDER BY sh.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/alarms/{alarm_id}/shelf')
def request_alarm_shelf(alarm_id:int,body:AlarmShelfIn,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        alarm=get_or_404(conn,'SELECT * FROM operational_alarms WHERE id=?',(alarm_id,),'Alarm not found')
        if alarm['status'] not in ('Open','Acknowledged'):raise HTTPException(409,'Only active alarms can be shelved')
        if alarm['severity']=='Critical' and body.duration_minutes>120:raise HTTPException(422,'Critical alarms can be shelved for at most 120 minutes')
        existing=conn.execute("SELECT * FROM alarm_shelves WHERE alarm_id=? AND (status='Pending' OR (status='Approved' AND end_at>?)) ORDER BY id DESC LIMIT 1",(alarm_id,now())).fetchone()
        if existing:raise HTTPException(409,f"Alarm already has {existing['status'].lower()} shelf request {existing['shelf_no']}")
        no=next_no(conn,'alarm_shelves','shelf_no','SHF-',68001)
        cur=conn.execute("INSERT INTO alarm_shelves(shelf_no,alarm_id,reason,duration_minutes,status,requested_by,requested_at) VALUES(?,?,?,?,'Pending',?,?)",(no,alarm_id,body.reason,body.duration_minutes,user['id'],now()))
        create_approval(conn,'Utilities Operations','alarm_shelf',cur.lastrowid,no,f"Approve alarm shelf {no} — {alarm['alarm_no']}",user['id'],assigned_role='maintenance_manager')
        emit_event(conn,'operations.alarm_shelf.requested','alarm_shelf',no,{'shelf_no':no,'alarm_no':alarm['alarm_no'],'duration_minutes':body.duration_minutes,'severity':alarm['severity']})
        audit(conn,user['id'],'REQUEST ALARM SHELF','Utilities Operations',no,'',{'alarm_no':alarm['alarm_no'],'duration_minutes':body.duration_minutes,'reason':body.reason})
        return {'id':cur.lastrowid,'shelf_no':no,'status':'Pending','approval_required':True}

@app.post('/api/alarm-shelves/{shelf_id}/revoke')
def revoke_alarm_shelf(shelf_id:int,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        sh=get_or_404(conn,'SELECT sh.*,oa.alarm_no FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id WHERE sh.id=?',(shelf_id,),'Alarm shelf not found')
        if sh['status'] in ('Expired','Revoked','Rejected'):return {'ok':True,'status':sh['status']}
        if sh['status']=='Pending':raise HTTPException(409,'Pending shelf requests must be approved or rejected in Approval Center')
        conn.execute("UPDATE alarm_shelves SET status='Revoked',revoked_by=?,revoked_at=? WHERE id=?",(user['id'],now(),shelf_id))
        emit_event(conn,'operations.alarm_shelf.revoked','alarm_shelf',sh['shelf_no'],{'shelf_no':sh['shelf_no'],'alarm_no':sh['alarm_no']});audit(conn,user['id'],'REVOKE ALARM SHELF','Utilities Operations',sh['shelf_no'],'Approved','Revoked')
        return {'ok':True,'status':'Revoked'}

@app.get('/api/asset-topology')
def asset_topology(active_only:bool=True,site_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT t.*,ua.asset_no upstream_asset_no,ua.name upstream_asset_name,da.asset_no downstream_asset_no,da.name downstream_asset_name,
      us.id upstream_site_id,us.name upstream_site_name,ds.id downstream_site_id,ds.name downstream_site_name,u.full_name created_by_name
      FROM asset_topology_links t JOIN assets ua ON ua.id=t.upstream_asset_id JOIN assets da ON da.id=t.downstream_asset_id
      LEFT JOIN locations ul ON ul.id=ua.location_id LEFT JOIN sites us ON us.id=ul.site_id
      LEFT JOIN locations dl ON dl.id=da.location_id LEFT JOIN sites ds ON ds.id=dl.site_id LEFT JOIN users u ON u.id=t.created_by WHERE 1=1""";args=[]
    if active_only:sql+=' AND t.active=1'
    if site_id is not None:sql+=' AND (us.id=? OR ds.id=?)';args += [site_id,site_id]
    sql+=' ORDER BY t.link_no'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/asset-topology')
def create_asset_topology(body:AssetTopologyLinkIn,user=Depends(require_permission('telemetry.configure','admin','asset_manager','maintenance_manager'))):
    if body.upstream_asset_id==body.downstream_asset_id:raise HTTPException(422,'Topology link must connect two different assets')
    with db() as conn:
        up=get_or_404(conn,'SELECT id,asset_no,name FROM assets WHERE id=?',(body.upstream_asset_id,),'Upstream asset not found')
        down_asset=get_or_404(conn,'SELECT id,asset_no,name FROM assets WHERE id=?',(body.downstream_asset_id,),'Downstream asset not found')
        dup=conn.execute('SELECT * FROM asset_topology_links WHERE upstream_asset_id=? AND downstream_asset_id=? AND active=1',(body.upstream_asset_id,body.downstream_asset_id)).fetchone()
        if dup:raise HTTPException(409,f"Active topology link {dup['link_no']} already exists")
        _,directed,_=_topology_graph(conn)
        if _graph_distance(directed,body.downstream_asset_id,body.upstream_asset_id,50) is not None:raise HTTPException(409,'Topology link would create a directed cycle')
        no=next_no(conn,'asset_topology_links','link_no','TPL-',1001)
        cur=conn.execute('INSERT INTO asset_topology_links(link_no,upstream_asset_id,downstream_asset_id,relation_type,active,notes,created_by,created_at) VALUES(?,?,?,?,1,?,?,?)',(no,body.upstream_asset_id,body.downstream_asset_id,body.relation_type.strip(),body.notes.strip(),user['id'],now()))
        emit_event(conn,'operations.topology.link_created','asset_topology_link',no,{'link_no':no,'upstream_asset':up['asset_no'],'downstream_asset':down_asset['asset_no'],'relation_type':body.relation_type})
        audit(conn,user['id'],'CREATE TOPOLOGY LINK','Utilities Operations',no,'',body.model_dump())
        return {'id':cur.lastrowid,'link_no':no,'active':True}

@app.post('/api/asset-topology/{link_id}/deactivate')
def deactivate_asset_topology(link_id:int,user=Depends(require_permission('telemetry.configure','admin','asset_manager','maintenance_manager'))):
    with db() as conn:
        link=get_or_404(conn,'SELECT * FROM asset_topology_links WHERE id=?',(link_id,),'Topology link not found')
        if not link['active']:return {'ok':True,'active':False}
        conn.execute('UPDATE asset_topology_links SET active=0 WHERE id=?',(link_id,))
        emit_event(conn,'operations.topology.link_deactivated','asset_topology_link',link['link_no'],{'link_no':link['link_no']})
        audit(conn,user['id'],'DEACTIVATE TOPOLOGY LINK','Utilities Operations',link['link_no'],'Active','Inactive')
        return {'ok':True,'active':False}

@app.get('/api/alarm-incidents')
def alarm_incidents(status:str='',severity:str='',site_id:Optional[int]=None,asset_id:Optional[int]=None,limit:int=Query(200,ge=1,le=1000),user=Depends(current_user)):
    sql="""SELECT i.*,s.site_code,s.name site_name,a.asset_no,a.name asset_name,rc.asset_no root_cause_asset_no,rc.name root_cause_asset_name,w.wo_no,ack.full_name acknowledged_by_name,res.full_name resolved_by_name
      FROM alarm_incidents i LEFT JOIN sites s ON s.id=i.site_id LEFT JOIN assets a ON a.id=i.asset_id LEFT JOIN assets rc ON rc.id=i.root_cause_asset_id
      LEFT JOIN work_orders w ON w.id=i.work_order_id LEFT JOIN users ack ON ack.id=i.acknowledged_by LEFT JOIN users res ON res.id=i.resolved_by WHERE 1=1""";args=[]
    if status:sql+=' AND i.status=?';args.append(status)
    if severity:sql+=' AND i.severity=?';args.append(severity)
    if site_id is not None:sql+=' AND i.site_id=?';args.append(site_id)
    if asset_id is not None:sql+=' AND i.asset_id=?';args.append(asset_id)
    sql+=" ORDER BY CASE i.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,i.last_seen_at DESC LIMIT ?";args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/alarm-incidents/{incident_id}')
def alarm_incident_detail(incident_id:int,user=Depends(current_user)):
    with db() as conn:
        incident=get_or_404(conn,"""SELECT i.*,s.site_code,s.name site_name,a.asset_no,a.name asset_name,rc.asset_no root_cause_asset_no,rc.name root_cause_asset_name,w.wo_no
          FROM alarm_incidents i LEFT JOIN sites s ON s.id=i.site_id LEFT JOIN assets a ON a.id=i.asset_id LEFT JOIN assets rc ON rc.id=i.root_cause_asset_id LEFT JOIN work_orders w ON w.id=i.work_order_id WHERE i.id=?""",(incident_id,),'Alarm incident not found')
        members,active,severity=_incident_member_summary(conn,incident_id);incident['alarms']=members;incident['active_alarm_count']=len(active);incident['derived_severity']=severity;return incident

@app.post('/api/alarm-incidents/{incident_id}/acknowledge')
def acknowledge_alarm_incident(incident_id:int,body:IncidentTransitionIn,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        inc=get_or_404(conn,'SELECT * FROM alarm_incidents WHERE id=?',(incident_id,),'Alarm incident not found')
        if inc['status']=='Resolved':raise HTTPException(409,'Incident is already resolved')
        conn.execute("UPDATE alarm_incidents SET status='Acknowledged',acknowledged_at=?,acknowledged_by=?,updated_at=? WHERE id=?",(now(),user['id'],now(),incident_id))
        audit(conn,user['id'],'ACKNOWLEDGE INCIDENT','Utilities Operations',inc['incident_no'],inc['status'],'Acknowledged');return {'ok':True,'status':'Acknowledged'}

@app.post('/api/alarm-incidents/{incident_id}/resolve')
def resolve_alarm_incident(incident_id:int,body:IncidentTransitionIn,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        inc=get_or_404(conn,'SELECT * FROM alarm_incidents WHERE id=?',(incident_id,),'Alarm incident not found');members,active,_=_incident_member_summary(conn,incident_id)
        if active:raise HTTPException(409,f'{len(active)} member alarm(s) are still active')
        conn.execute("UPDATE alarm_incidents SET status='Resolved',resolved_at=?,resolved_by=?,updated_at=? WHERE id=?",(now(),user['id'],now(),incident_id))
        emit_event(conn,'operations.incident.resolved','alarm_incident',inc['incident_no'],{'incident_no':inc['incident_no'],'notes':body.notes});audit(conn,user['id'],'RESOLVE INCIDENT','Utilities Operations',inc['incident_no'],inc['status'],'Resolved');return {'ok':True,'status':'Resolved'}

@app.post('/api/alarm-incidents/{incident_id}/work-order')
def incident_create_work_order(incident_id:int,body:IncidentWorkOrderIn,user=Depends(require_permission('work.write','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        inc=get_or_404(conn,"""SELECT i.*,a.asset_no anchor_asset_no,a.location_id anchor_location_id,rc.asset_no root_asset_no,rc.location_id root_location_id
          FROM alarm_incidents i JOIN assets a ON a.id=i.asset_id LEFT JOIN assets rc ON rc.id=i.root_cause_asset_id WHERE i.id=?""",(incident_id,),'Alarm incident not found')
        work_asset_id=inc.get('root_cause_asset_id') or inc['asset_id'];work_asset_no=inc.get('root_asset_no') or inc['anchor_asset_no'];work_location_id=inc.get('root_location_id') or inc['anchor_location_id']
        if inc.get('work_order_id'):
            w=conn.execute('SELECT id,wo_no FROM work_orders WHERE id=?',(inc['work_order_id'],)).fetchone();return {'id':w['id'],'wo_no':w['wo_no'],'existing':True}
        members,active,severity=_incident_member_summary(conn,incident_id);priority='Critical' if severity=='Critical' else 'High';no=next_no(conn,'work_orders','wo_no','WO-',10026)
        finish=body.target_finish or (date.today()+timedelta(days=1 if priority=='Critical' else 2)).isoformat();channels=', '.join(sorted({x['channel_code'] for x in members}));title=f"Investigate {inc['incident_no']} — {work_asset_no} operational incident"
        desc=f"Correlated operational incident containing {len(members)} alarm(s). Channels: {channels}. Deterministic root-cause candidate: {work_asset_no} ({inc.get('root_cause_score') or 0:.1f}%). {inc.get('root_cause_reason') or ''}";instructions=body.notes or 'Validate telemetry, inspect the asset, identify root cause and restore normal operation.'
        cur=conn.execute("""INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,instructions,created_at,updated_at)
          VALUES(?,?,?,?,?,?,'Submitted','Corrective Maintenance',?,?,?,?,?,?,?,?,?,?)""",(no,title,desc,work_asset_id,work_location_id,priority,f"INCIDENT-{inc['incident_no']}",user['id'],body.assigned_to,body.supervisor_id,date.today().isoformat(),finish,4,instructions,now(),now()))
        conn.execute('UPDATE alarm_incidents SET work_order_id=?,updated_at=? WHERE id=?',(cur.lastrowid,now(),incident_id));_ensure_work_sla(conn,cur.lastrowid)
        create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f"Approve {no} — {title}",user['id'],assigned_user_id=body.supervisor_id,assigned_role=None if body.supervisor_id else 'maintenance_manager')
        workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'INCIDENT GENERATED','', 'Submitted',user['id'],inc['incident_no']);emit_event(conn,'operations.incident.work_order_created','alarm_incident',inc['incident_no'],{'incident_no':inc['incident_no'],'work_order':no});audit(conn,user['id'],'CREATE WORK FROM INCIDENT','Utilities Operations',inc['incident_no'],'',no)
        return {'id':cur.lastrowid,'wo_no':no,'existing':False}

@app.get('/api/alarms')
def alarms(status:str='',severity:str='',asset_id:Optional[int]=None,site_id:Optional[int]=None,limit:int=Query(200,ge=1,le=1000),user=Depends(current_user)):
    sql="SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,s.site_code,s.name site_name,w.wo_no,ack.full_name acknowledged_by_name,cl.full_name closed_by_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id LEFT JOIN sites s ON s.id=oa.site_id LEFT JOIN work_orders w ON w.id=oa.work_order_id LEFT JOIN users ack ON ack.id=oa.acknowledged_by LEFT JOIN users cl ON cl.id=oa.closed_by WHERE 1=1";args=[]
    if status:sql+=' AND oa.status=?';args.append(status)
    if severity:sql+=' AND oa.severity=?';args.append(severity)
    if asset_id is not None:sql+=' AND oa.asset_id=?';args.append(asset_id)
    if site_id is not None:sql+=' AND oa.site_id=?';args.append(site_id)
    sql+=" ORDER BY CASE oa.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,oa.id DESC LIMIT ?";args.append(limit)
    with db() as conn:
        data=rows(conn.execute(sql,args));stamp=now()
        for alarm in data:
            sh=conn.execute("SELECT id,shelf_no,end_at FROM alarm_shelves WHERE alarm_id=? AND status='Approved' AND start_at<=? AND end_at>? ORDER BY id DESC LIMIT 1",(alarm['id'],stamp,stamp)).fetchone()
            alarm['shelved']=bool(sh);alarm['shelf_id']=sh['id'] if sh else None;alarm['shelf_no']=sh['shelf_no'] if sh else None;alarm['shelf_end_at']=sh['end_at'] if sh else None
        return data

@app.post('/api/alarms/{alarm_id}/acknowledge')
def acknowledge_alarm(alarm_id:int,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        a=get_or_404(conn,'SELECT * FROM operational_alarms WHERE id=?',(alarm_id,),'Alarm not found')
        if a['status'] not in ('Open','Acknowledged'):raise HTTPException(409,f"Alarm is {a['status']}")
        conn.execute("UPDATE operational_alarms SET status='Acknowledged',acknowledged_at=?,acknowledged_by=? WHERE id=?",(now(),user['id'],alarm_id));_correlate_alarm(conn,alarm_id,user['id']);audit(conn,user['id'],'ACKNOWLEDGE ALARM','Utilities Operations',a['alarm_no'],a['status'],'Acknowledged');return {'ok':True,'status':'Acknowledged'}

@app.post('/api/alarms/{alarm_id}/close')
def close_alarm(alarm_id:int,user=Depends(require_permission('alarms.operate','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        a=get_or_404(conn,'SELECT * FROM operational_alarms WHERE id=?',(alarm_id,),'Alarm not found')
        if a['status']=='Closed':return {'ok':True,'status':'Closed'}
        conn.execute("UPDATE operational_alarms SET status='Closed',closed_at=?,closed_by=? WHERE id=?",(now(),user['id'],alarm_id));_refresh_incidents_for_alarm(conn,alarm_id,user['id']);emit_event(conn,'operations.alarm.closed','alarm',a['alarm_no'],{'alarm_no':a['alarm_no'],'asset_id':a['asset_id']});audit(conn,user['id'],'CLOSE ALARM','Utilities Operations',a['alarm_no'],a['status'],'Closed');return {'ok':True,'status':'Closed'}

@app.post('/api/alarms/{alarm_id}/work-order')
def alarm_create_work_order(alarm_id:int,body:AlarmWorkOrderIn,user=Depends(require_permission('work.write','admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        alarm=get_or_404(conn,'SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.location_id FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id WHERE oa.id=?',(alarm_id,),'Alarm not found')
        if alarm.get('work_order_id'):
            w=conn.execute('SELECT id,wo_no FROM work_orders WHERE id=?',(alarm['work_order_id'],)).fetchone();return {'id':w['id'],'wo_no':w['wo_no'],'existing':True}
        no=next_no(conn,'work_orders','wo_no','WO-',10026);priority='Critical' if alarm['severity']=='Critical' else 'High';finish=body.target_finish or (date.today()+timedelta(days=1 if priority=='Critical' else 2)).isoformat();title=f"Investigate {alarm['channel_name']} alarm";desc=f"Generated from {alarm['alarm_no']} on {alarm['asset_no']}. {alarm['message']}"
        cur=conn.execute("INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,instructions,created_at,updated_at) VALUES(?,?,?,?,?,?,'Submitted','Corrective Maintenance',?,?,?,?,?,?,?,?,?,?)",(no,title,desc,alarm['asset_id'],alarm['location_id'],priority,f"ALARM-{alarm['channel_code']}",user['id'],body.assigned_to,body.supervisor_id,date.today().isoformat(),finish,2,body.notes or f"Validate {alarm['channel_name']} reading and investigate root cause.",now(),now()))
        conn.execute('UPDATE operational_alarms SET work_order_id=? WHERE id=?',(cur.lastrowid,alarm_id));_ensure_work_sla(conn,cur.lastrowid);create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f"Approve {no} — {title}",user['id'],assigned_user_id=body.supervisor_id,assigned_role=None if body.supervisor_id else 'maintenance_manager');workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'ALARM GENERATED','', 'Submitted',user['id'],alarm['alarm_no']);emit_event(conn,'operations.alarm.work_order_created','alarm',alarm['alarm_no'],{'alarm_no':alarm['alarm_no'],'work_order':no});audit(conn,user['id'],'CREATE WORK FROM ALARM','Utilities Operations',alarm['alarm_no'],'',no);return {'id':cur.lastrowid,'wo_no':no,'existing':False}

@app.get('/api/operations/intelligence')
def operations_intelligence(site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        result=_operations_intelligence(conn,site_id)
        sql="SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,s.name site_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id LEFT JOIN sites s ON s.id=oa.site_id WHERE oa.status IN ('Open','Acknowledged')";args=[]
        if site_id is not None:sql+=' AND oa.site_id=?';args.append(site_id)
        sql+=" ORDER BY CASE oa.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,oa.last_seen_at DESC LIMIT 20"
        result['alarms']=rows(conn.execute(sql,args))
        csql="SELECT tc.channel_code,tc.name,tc.metric_type,tc.unit,tc.last_value,tc.last_quality,tc.last_reading_at,a.asset_no,a.name asset_name,s.id site_id FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE tc.active=1";cargs=[]
        if site_id is not None:csql+=' AND s.id=?';cargs.append(site_id)
        csql+=' ORDER BY tc.last_reading_at DESC LIMIT 50';result['channels']=rows(conn.execute(csql,cargs));return result

@app.get('/api/operations/command-center')
def operations_command_center(site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        intel=_operations_intelligence(conn,site_id)
        incident_sql="""SELECT i.*,a.asset_no,a.name asset_name,rc.asset_no root_cause_asset_no,rc.name root_cause_asset_name,s.name site_name,w.wo_no FROM alarm_incidents i
          LEFT JOIN assets a ON a.id=i.asset_id LEFT JOIN assets rc ON rc.id=i.root_cause_asset_id LEFT JOIN sites s ON s.id=i.site_id LEFT JOIN work_orders w ON w.id=i.work_order_id
          WHERE i.status IN ('Open','Acknowledged')""";incident_args=[]
        if site_id is not None:incident_sql+=' AND i.site_id=?';incident_args.append(site_id)
        incident_sql+=" ORDER BY CASE i.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,i.last_seen_at DESC LIMIT 20"
        incidents=rows(conn.execute(incident_sql,incident_args))
        outage_sql="""SELECT o.*,a.asset_no,a.name asset_name,s.name site_name FROM asset_outages o JOIN assets a ON a.id=o.asset_id
          LEFT JOIN sites s ON s.id=o.site_id WHERE o.status='Open'""";outage_args=[]
        if site_id is not None:outage_sql+=' AND o.site_id=?';outage_args.append(site_id)
        outages=rows(conn.execute(outage_sql,outage_args));lost_capacity=sum(float(x.get('lost_capacity') or 0) for x in outages)
        dispatch_sql="""SELECT d.*,w.wo_no,w.title,u.full_name technician_name,a.asset_no FROM dispatch_assignments d
          JOIN work_orders w ON w.id=d.work_order_id JOIN users u ON u.id=d.technician_user_id LEFT JOIN assets a ON a.id=w.asset_id
          LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id
          WHERE d.status IN ('Dispatched','Accepted','En Route','On Site')""";dispatch_args=[]
        if site_id is not None:dispatch_sql+=' AND s.id=?';dispatch_args.append(site_id)
        dispatch=rows(conn.execute(dispatch_sql,dispatch_args))
        quality=_telemetry_quality_summary(conn,24,site_id)
        sup_sql="""SELECT sp.*,a.asset_no,tc.channel_code,s.name site_name FROM alarm_suppressions sp LEFT JOIN assets a ON a.id=sp.asset_id
          LEFT JOIN telemetry_channels tc ON tc.id=sp.channel_id LEFT JOIN sites s ON s.id=sp.site_id
          WHERE sp.active=1 AND sp.start_at<=? AND sp.end_at>=?""";sup_args=[now(),now()]
        if site_id is not None:sup_sql+=' AND (sp.site_id=? OR sp.site_id IS NULL)';sup_args.append(site_id)
        suppressions=rows(conn.execute(sup_sql,sup_args))
        alarm_sql="""SELECT oa.*,a.asset_no,tc.channel_code,tc.name channel_name,tc.unit FROM operational_alarms oa
          JOIN assets a ON a.id=oa.asset_id JOIN telemetry_channels tc ON tc.id=oa.channel_id WHERE oa.status IN ('Open','Acknowledged')""";alarm_args=[]
        if site_id is not None:alarm_sql+=' AND oa.site_id=?';alarm_args.append(site_id)
        alarm_sql+=" ORDER BY CASE oa.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,oa.last_seen_at DESC LIMIT 30"
        alarms=rows(conn.execute(alarm_sql,alarm_args))
        shelf_sql="""SELECT sh.*,oa.alarm_no,oa.site_id,oa.severity,a.asset_no,tc.channel_code,req.full_name requested_by_name,apr.full_name approved_by_name
          FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id JOIN assets a ON a.id=oa.asset_id JOIN telemetry_channels tc ON tc.id=oa.channel_id
          JOIN users req ON req.id=sh.requested_by LEFT JOIN users apr ON apr.id=sh.approved_by
          WHERE sh.status='Approved' AND sh.start_at<=? AND sh.end_at>? AND oa.status IN ('Open','Acknowledged')""";shelf_args=[now(),now()]
        if site_id is not None:shelf_sql+=' AND oa.site_id=?';shelf_args.append(site_id)
        shelves=rows(conn.execute(shelf_sql,shelf_args));shelf_by_alarm={x['alarm_id']:x for x in shelves}
        for alarm in alarms:
            sh=shelf_by_alarm.get(alarm['id']);alarm['shelved']=bool(sh);alarm['shelf_no']=sh['shelf_no'] if sh else None;alarm['shelf_end_at']=sh['end_at'] if sh else None
        actionable=[x for x in alarms if not x['shelved']]
        topology_sql="""SELECT t.*,ua.asset_no upstream_asset_no,da.asset_no downstream_asset_no,us.id upstream_site_id,ds.id downstream_site_id
          FROM asset_topology_links t JOIN assets ua ON ua.id=t.upstream_asset_id JOIN assets da ON da.id=t.downstream_asset_id
          LEFT JOIN locations ul ON ul.id=ua.location_id LEFT JOIN sites us ON us.id=ul.site_id LEFT JOIN locations dl ON dl.id=da.location_id LEFT JOIN sites ds ON ds.id=dl.site_id WHERE t.active=1""";topology_args=[]
        if site_id is not None:topology_sql+=' AND (us.id=? OR ds.id=?)';topology_args += [site_id,site_id]
        topology_links=rows(conn.execute(topology_sql+' ORDER BY t.link_no',topology_args))
        topology_incidents=sum(1 for x in incidents if x.get('correlation_mode')=='Topology')
        return {'summary':intel|{'open_outages':len(outages),'active_dispatches':len(dispatch),'lost_capacity_total':round(lost_capacity,2),'active_alarm_shelves':len(shelves),'actionable_alarms':len(actionable),'active_topology_links':len(topology_links),'topology_correlated_incidents':topology_incidents},
                'data_quality':quality,'incidents':incidents,'alarms':alarms,'actionable_alarms':actionable,'shelves':shelves,'outages':outages,'dispatch':dispatch,'suppressions':suppressions,'topology_links':topology_links}

# ---------- assets ----------
ASSET_SELECT='''SELECT a.*,at.name asset_type,at.utility_domain,l.name location_name,l.location_code,s.id site_id,s.name site_name,p.asset_no parent_asset_no,p.name parent_asset_name,u.full_name responsible_person,v.name vendor_name FROM assets a LEFT JOIN asset_types at ON at.id=a.asset_type_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id LEFT JOIN assets p ON p.id=a.parent_asset_id LEFT JOIN users u ON u.id=a.responsible_user_id LEFT JOIN vendors v ON v.id=a.vendor_id'''
@app.get('/api/assets')
def list_assets(q:str='',condition:str='',status:str='',site_id:Optional[int]=None,sort:str='asset_no',user=Depends(current_user)):
    allowed={'asset_no':'a.asset_no','name':'a.name','condition':'a.condition','criticality':'a.criticality','current_value':'a.current_value'}; order=allowed.get(sort,'a.asset_no')
    sql=ASSET_SELECT+' WHERE 1=1';args=[]
    if q: sql+=' AND (a.asset_no LIKE ? OR a.name LIKE ? OR a.serial_no LIKE ? OR l.name LIKE ?)';like=f'%{q}%';args += [like]*4
    if condition: sql+=' AND a.condition=?';args.append(condition)
    if status: sql+=' AND a.status=?';args.append(status)
    if site_id: sql+=' AND s.id=?';args.append(site_id)
    sql+=f' ORDER BY {order}'
    with db() as conn:return rows(conn.execute(sql,args))
@app.get('/api/assets/{asset_id}')
def get_asset(asset_id:int,user=Depends(current_user)):
    with db() as conn:
        a=get_or_404(conn,ASSET_SELECT+' WHERE a.id=?',(asset_id,),'Asset not found')
        a['children']=rows(conn.execute('SELECT id,asset_no,name,condition,status FROM assets WHERE parent_asset_id=? ORDER BY asset_no',(asset_id,)))
        a['meters']=rows(conn.execute('SELECT * FROM meters WHERE asset_id=?',(asset_id,)))
        a['work_history']=rows(conn.execute('SELECT id,wo_no,title,status,priority,work_type,actual_finish,actual_cost FROM work_orders WHERE asset_id=? ORDER BY id DESC',(asset_id,)))
        a['inspections']=rows(conn.execute('SELECT id,inspection_no,template_name,status,result,inspected_at FROM inspections WHERE asset_id=? ORDER BY id DESC',(asset_id,)))
        a['documents']=rows(conn.execute('SELECT id,document_no,title,category,file_name,uploaded_at FROM documents WHERE asset_id=? ORDER BY id DESC',(asset_id,)))
        a['outages']=rows(conn.execute('SELECT * FROM asset_outages WHERE asset_id=? ORDER BY start_at DESC',(asset_id,)))
        a['telemetry_channels']=rows(conn.execute('SELECT * FROM telemetry_channels WHERE asset_id=? ORDER BY channel_code',(asset_id,)))
        a['operational_alarms']=rows(conn.execute("SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id WHERE oa.asset_id=? ORDER BY oa.id DESC",(asset_id,)))
        a['topology_upstream']=rows(conn.execute('''SELECT t.*,ua.asset_no upstream_asset_no,ua.name upstream_asset_name FROM asset_topology_links t JOIN assets ua ON ua.id=t.upstream_asset_id WHERE t.downstream_asset_id=? AND t.active=1 ORDER BY t.link_no''',(asset_id,)))
        a['topology_downstream']=rows(conn.execute('''SELECT t.*,da.asset_no downstream_asset_no,da.name downstream_asset_name FROM asset_topology_links t JOIN assets da ON da.id=t.downstream_asset_id WHERE t.upstream_asset_id=? AND t.active=1 ORDER BY t.link_no''',(asset_id,)))
        a['cost_ledger']=rows(conn.execute('''SELECT c.*,w.wo_no,u.full_name posted_by_name FROM maintenance_cost_ledger c LEFT JOIN work_orders w ON w.id=c.work_order_id LEFT JOIN users u ON u.id=c.posted_by WHERE c.asset_id=? ORDER BY c.id DESC''',(asset_id,)))
        a['cost_summary']=rows(conn.execute('SELECT cost_type,COUNT(*) entries,COALESCE(SUM(amount),0) amount FROM maintenance_cost_ledger WHERE asset_id=? GROUP BY cost_type ORDER BY amount DESC',(asset_id,)))
        a['lifetime_maintenance_cost']=sum(float(x['amount']) for x in a['cost_summary'])
        return a
@app.get('/api/assets/{asset_id}/timeline')
def asset_timeline(asset_id:int,user=Depends(current_user)):
    with db() as conn:
        a=get_or_404(conn,'SELECT id,asset_no,name FROM assets WHERE id=?',(asset_id,),'Asset not found')
        events=[]
        for w in rows(conn.execute('SELECT id,wo_no,title,status,priority,created_at,actual_finish,actual_cost FROM work_orders WHERE asset_id=?',(asset_id,))):
            events.append({'at':w['created_at'],'type':'Work Order','code':w['wo_no'],'title':w['title'],'detail':f"Created · {w['priority']} · {w['status']}",'module':'work','id':w['id']})
            if w['actual_finish']:events.append({'at':w['actual_finish'],'type':'Maintenance','code':w['wo_no'],'title':w['title'],'detail':f"Completed · cost {float(w['actual_cost']):.2f}",'module':'work','id':w['id']})
        for i in rows(conn.execute('SELECT id,inspection_no,template_name,status,result,created_at,inspected_at FROM inspections WHERE asset_id=?',(asset_id,))):
            events.append({'at':i['inspected_at'] or i['created_at'],'type':'Inspection','code':i['inspection_no'],'title':i['template_name'],'detail':f"{i['result'] or i['status']}",'module':'inspections','id':i['id']})
        for m in rows(conn.execute('''SELECT mr.id,m.meter_code,m.unit,mr.reading,mr.reading_at,u.full_name FROM meter_readings mr JOIN meters m ON m.id=mr.meter_id JOIN users u ON u.id=mr.entered_by WHERE m.asset_id=?''',(asset_id,))):
            events.append({'at':m['reading_at'],'type':'Meter','code':m['meter_code'],'title':f"Meter reading {m['reading']} {m['unit']}",'detail':m['full_name'],'module':'assets','id':asset_id})
        for d in rows(conn.execute('SELECT id,document_no,title,category,uploaded_at FROM documents WHERE asset_id=?',(asset_id,))):
            events.append({'at':d['uploaded_at'],'type':'Document','code':d['document_no'],'title':d['title'],'detail':d['category'],'module':'documents','id':d['id']})
        for c in rows(conn.execute('SELECT id,entry_no,cost_type,amount,reference,posted_at FROM maintenance_cost_ledger WHERE asset_id=?',(asset_id,))):
            events.append({'at':c['posted_at'],'type':'Cost','code':c['entry_no'],'title':c['cost_type'],'detail':f"{float(c['amount']):.2f} · {c['reference']}",'module':'analytics','id':c['id']})
        for aev in rows(conn.execute('SELECT oa.id,oa.alarm_no,oa.severity,oa.status,oa.message,oa.opened_at,tc.channel_code FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id WHERE oa.asset_id=?',(asset_id,))):
            events.append({'at':aev['opened_at'],'type':'Operational Alarm','code':aev['alarm_no'],'title':aev['message'],'detail':f"{aev['severity']} · {aev['status']} · {aev['channel_code']}",'module':'telemetry','id':aev['id']})
        events.sort(key=lambda x:x['at'] or '',reverse=True)
        return {'asset':a,'events':events}

def _asset_dossier(conn,asset_id:int):
    a=get_or_404(conn,ASSET_SELECT+' WHERE a.id=?',(asset_id,),'Asset not found')
    return {
      'asset':a,
      'children':rows(conn.execute('SELECT asset_no,name,condition,status FROM assets WHERE parent_asset_id=? ORDER BY asset_no',(asset_id,))),
      'work_orders':rows(conn.execute('SELECT wo_no,title,status,priority,work_type,actual_start,actual_finish,actual_hours,actual_cost FROM work_orders WHERE asset_id=? ORDER BY id DESC',(asset_id,))),
      'inspections':rows(conn.execute('SELECT inspection_no,template_name,status,result,inspected_at,remarks FROM inspections WHERE asset_id=? ORDER BY id DESC',(asset_id,))),
      'documents':rows(conn.execute('SELECT document_no,title,category,file_name,uploaded_at FROM documents WHERE asset_id=? ORDER BY id DESC',(asset_id,))),
      'costs':rows(conn.execute('SELECT entry_no,cost_type,amount,quantity,reference,posted_at FROM maintenance_cost_ledger WHERE asset_id=? ORDER BY id DESC',(asset_id,))),
      'meter_readings':rows(conn.execute('''SELECT m.meter_code,m.meter_type,m.unit,mr.reading,mr.reading_at FROM meter_readings mr JOIN meters m ON m.id=mr.meter_id WHERE m.asset_id=? ORDER BY mr.id DESC''',(asset_id,)))
    }

@app.post('/api/assets/{asset_id}/dossier')
def generate_asset_dossier(asset_id:int,user=Depends(current_user)):
    with db() as conn:
        payload=_asset_dossier(conn,asset_id);a=payload['asset'];serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
        digest=hashlib.sha256(serialized.encode()).hexdigest();no=next_no(conn,'report_snapshots','report_no','RPT-',10001)
        cur=conn.execute('INSERT INTO report_snapshots(report_no,report_type,scope_type,scope_id,title,snapshot_json,content_hash,generated_by,generated_at) VALUES(?,?,?,?,?,?,?,?,?)',(no,'Asset Dossier','asset',a['asset_no'],f"{a['asset_no']} — {a['name']} Asset Dossier",serialized,digest,user['id'],now()))
        audit(conn,user['id'],'GENERATE REPORT','Reports',no,'',{'scope':a['asset_no'],'sha256':digest})
        return {'id':cur.lastrowid,'report_no':no,'title':f"{a['asset_no']} — {a['name']} Asset Dossier",'content_hash':digest}

@app.get('/api/reports/snapshots')
def report_snapshots(scope_type:str='',scope_id:str='',limit:int=Query(100,ge=1,le=500),user=Depends(current_user)):
    sql='''SELECT r.id,r.report_no,r.report_type,r.scope_type,r.scope_id,r.title,r.content_hash,r.generated_at,u.full_name generated_by_name FROM report_snapshots r JOIN users u ON u.id=r.generated_by WHERE 1=1''';args=[]
    if scope_type:sql+=' AND r.scope_type=?';args.append(scope_type)
    if scope_id:sql+=' AND r.scope_id=?';args.append(scope_id)
    sql+=' ORDER BY r.id DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/reports/snapshots/{report_id}')
def report_snapshot(report_id:int,user=Depends(current_user)):
    with db() as conn:
        r=get_or_404(conn,'SELECT r.*,u.full_name generated_by_name FROM report_snapshots r JOIN users u ON u.id=r.generated_by WHERE r.id=?',(report_id,),'Report snapshot not found')
        r['snapshot']=json.loads(r.pop('snapshot_json'));return r

@app.get('/api/reports/snapshots/{report_id}/verify')
def verify_report_snapshot(report_id:int,user=Depends(current_user)):
    with db() as conn:r=get_or_404(conn,'SELECT id,report_no,snapshot_json,content_hash FROM report_snapshots WHERE id=?',(report_id,),'Report snapshot not found')
    actual=hashlib.sha256(r['snapshot_json'].encode()).hexdigest()
    return {'report_no':r['report_no'],'valid':hmac.compare_digest(actual,r['content_hash']),'stored_hash':r['content_hash'],'actual_hash':actual}

@app.get('/api/reports/snapshots/{report_id}/html')
def report_snapshot_html(report_id:int,user=Depends(current_user)):
    with db() as conn:r=get_or_404(conn,'SELECT * FROM report_snapshots WHERE id=?',(report_id,),'Report snapshot not found')
    d=json.loads(r['snapshot_json']);a=d['asset'];works=d['work_orders'];costs=d['costs'];total=sum(float(x['amount']) for x in costs)
    html=f'''<html><head><title>{r['report_no']}</title><style>body{{font-family:Arial;margin:40px;color:#172033}}h1{{color:#c9272c}}table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}small{{color:#666}}</style></head><body><h1>ELSEWEDY UTILITIES</h1><h2>{r['title']}</h2><p><b>Report:</b> {r['report_no']}<br><b>Snapshot:</b> {r['generated_at']}<br><b>SHA-256:</b> {r['content_hash']}</p><h3>Asset</h3><p><b>{a['asset_no']} — {a['name']}</b><br>{a.get('site_name') or ''} · {a.get('location_name') or ''}<br>Condition: {a['condition']} | Status: {a['status']} | Criticality: {a['criticality']}</p><h3>Maintenance Cost</h3><p><b>{total:.2f}</b></p><h3>Work History</h3><table><tr><th>WO</th><th>Description</th><th>Status</th><th>Hours</th><th>Cost</th></tr>{''.join(f"<tr><td>{x['wo_no']}</td><td>{x['title']}</td><td>{x['status']}</td><td>{x['actual_hours']}</td><td>{x['actual_cost']}</td></tr>" for x in works)}</table><small>Immutable EUAS report snapshot · Developed by Omar & Seif</small></body></html>'''
    return HTMLResponse(html)

@app.post('/api/assets')
def create_asset(body:AssetIn,user=Depends(require_permission('assets.write',*WRITE_ROLES))):
    with db() as conn:
        return create_asset_record(conn,body.model_dump(),user['id'])
@app.patch('/api/assets/{asset_id}')
def update_asset(asset_id:int,body:AssetPatch,user=Depends(require_permission('assets.write',*WRITE_ROLES))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    with db() as conn:
        try:return update_asset_record(conn,asset_id,changes,user['id'])
        except AssetNotFound as exc:raise HTTPException(404,str(exc))
@app.delete('/api/assets/{asset_id}')
def delete_asset(asset_id:int,user=Depends(require_permission('assets.write','admin','asset_manager'))):
    with db() as conn:
        try:return delete_asset_record(conn,asset_id,user['id'])
        except AssetNotFound as exc:raise HTTPException(404,str(exc))
        except AssetDeleteBlocked as exc:raise HTTPException(409,str(exc))
@app.get('/api/assets-export.csv')
def export_assets(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute(ASSET_SELECT+' ORDER BY a.asset_no'))
    out=io.StringIO(); fields=['asset_no','name','category','manufacturer','model','serial_no','criticality','condition','status','site_name','location_name','department','responsible_person','current_value','next_maintenance'];w=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(data)
    return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=EUAS_assets.csv'})
@app.post('/api/meters/{meter_id}/readings')
def add_meter_reading(meter_id:int,body:MeterReadingIn,user=Depends(require_permission('assets.write',*WORK_ROLES))):
    with db() as conn:
        m=get_or_404(conn,'SELECT m.*,a.asset_no FROM meters m JOIN assets a ON a.id=m.asset_id WHERE m.id=?',(meter_id,),'Meter not found')
        conn.execute('INSERT INTO meter_readings(meter_id,reading,reading_at,entered_by) VALUES(?,?,?,?)',(meter_id,body.reading,now(),user['id']));conn.execute('UPDATE meters SET current_reading=? WHERE id=?',(body.reading,meter_id));conn.execute('UPDATE assets SET meter_reading=?,updated_at=? WHERE id=?',(body.reading,now(),m['asset_id']));audit(conn,user['id'],'METER READING','Assets',m['asset_no'],m['current_reading'],body.reading)
        return {'ok':True}

# ---------- work management ----------
WO_SELECT='''SELECT w.*,a.asset_no,a.name asset_name,l.name location_name,s.name site_name,req.full_name requested_by_name,tech.full_name assigned_to_name,sup.full_name supervisor_name,sl.response_due sla_response_due,sl.resolution_due sla_resolution_due,sl.response_status sla_response_status,sl.resolution_status sla_resolution_status,sl.escalated_level sla_escalated_level FROM work_orders w LEFT JOIN assets a ON a.id=w.asset_id LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id LEFT JOIN users req ON req.id=w.requested_by LEFT JOIN users tech ON tech.id=w.assigned_to LEFT JOIN users sup ON sup.id=w.supervisor_id LEFT JOIN work_order_sla sl ON sl.work_order_id=w.id'''
@app.get('/api/work-orders')
def list_work(status:str='',priority:str='',q:str='',assigned_to:Optional[int]=None,site_id:Optional[int]=None,user=Depends(current_user)):
    sql=WO_SELECT+' WHERE 1=1';args=[]
    if status:sql+=' AND w.status=?';args.append(status)
    if priority:sql+=' AND w.priority=?';args.append(priority)
    if assigned_to:sql+=' AND w.assigned_to=?';args.append(assigned_to)
    if site_id:sql+=' AND s.id=?';args.append(site_id)
    if q:sql+=' AND (w.wo_no LIKE ? OR w.title LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ?)';like=f'%{q}%';args += [like]*4
    sql+=' ORDER BY CASE w.priority WHEN \'Emergency\' THEN 5 WHEN \'Critical\' THEN 4 WHEN \'High\' THEN 3 WHEN \'Medium\' THEN 2 ELSE 1 END DESC,w.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))
@app.get('/api/work-orders/{wo_id}')
def get_work(wo_id:int,user=Depends(current_user)):
    with db() as conn:
        w=get_or_404(conn,WO_SELECT+' WHERE w.id=?',(wo_id,),'Work order not found')
        w['tasks']=rows(conn.execute('SELECT * FROM work_order_tasks WHERE work_order_id=? ORDER BY sequence_no',(wo_id,)))
        w['labor']=rows(conn.execute('SELECT le.*,u.full_name FROM labor_entries le JOIN users u ON u.id=le.user_id WHERE le.work_order_id=? ORDER BY le.id',(wo_id,)))
        w['materials']=rows(conn.execute('SELECT wom.*,i.item_no,i.name FROM work_order_materials wom JOIN inventory_items i ON i.id=wom.inventory_item_id WHERE wom.work_order_id=?',(wo_id,)))
        w['required_materials']=rows(conn.execute('''SELECT r.*,i.item_no,i.name,i.unit,i.current_stock,i.reserved_stock,(i.current_stock-i.reserved_stock) available_stock FROM work_order_requirements r JOIN inventory_items i ON i.id=r.inventory_item_id WHERE r.work_order_id=? ORDER BY i.item_no''',(wo_id,)))
        w['parts_readiness']=_work_order_parts_readiness(conn,wo_id)
        w['reservations']=_reservation_rows(conn,wo_id)
        w['dispatches']=rows(conn.execute('''SELECT d.*,u.full_name technician_name,byu.full_name dispatched_by_name FROM dispatch_assignments d JOIN users u ON u.id=d.technician_user_id JOIN users byu ON byu.id=d.dispatched_by WHERE d.work_order_id=? ORDER BY d.id DESC''',(wo_id,)))
        w['outages']=rows(conn.execute('''SELECT o.*,a.asset_no,s.name site_name FROM asset_outages o JOIN assets a ON a.id=o.asset_id LEFT JOIN sites s ON s.id=o.site_id WHERE o.work_order_id=? ORDER BY o.id DESC''',(wo_id,)))
        w['craft_requirements']=rows(conn.execute('''SELECT r.*,c.craft_code,c.name craft_name FROM work_order_craft_requirements r JOIN crafts c ON c.id=r.craft_id WHERE r.work_order_id=? ORDER BY c.name''',(wo_id,)))
        w['documents']=rows(conn.execute('SELECT * FROM documents WHERE work_order_id=? ORDER BY id DESC',(wo_id,)))
        return w
@app.post('/api/work-orders')
def create_work(body:WorkOrderIn,user=Depends(require_permission('work.write',*WRITE_ROLES))):
    with db() as conn:
        linked_fmea=_fmea_record(conn,body.asset_fmea_id,body.asset_id) if body.asset_fmea_id else None
        asset_id=body.asset_id or (linked_fmea['asset_id'] if linked_fmea else None)
        no=next_no(conn,'work_orders','wo_no','WO-',10026);loc=body.location_id
        if asset_id and not loc:
            r=conn.execute('SELECT location_id FROM assets WHERE id=?',(asset_id,)).fetchone();loc=r['location_id'] if r else None
        failure_code=body.failure_code or (linked_fmea['mode_no'] if linked_fmea else '')
        cur=conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,asset_fmea_id,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,safety_requirements,instructions,checklist,estimated_cost,created_at,updated_at) VALUES(?,?,?,?,?,?, 'Draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,body.title,body.description,asset_id,loc,body.priority,body.work_type,failure_code,body.asset_fmea_id,user['id'],body.assigned_to,body.supervisor_id,body.target_start,body.target_finish,body.estimated_hours,body.safety_requirements,body.instructions,body.checklist,body.estimated_cost,now(),now()))
        checklist=[x.strip() for x in body.checklist.replace('\n',';').replace(',',';').split(';') if x.strip()]
        for seq,task in enumerate(checklist,1): conn.execute("INSERT INTO work_order_tasks(work_order_id,sequence_no,task,status) VALUES(?,?,?,'Pending')",(cur.lastrowid,seq,task))
        _ensure_work_sla(conn,cur.lastrowid)
        workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'CREATE','', 'Draft',user['id'])
        if body.assigned_to: notify(conn,'Work order assigned',f'{no} — {body.title}','High' if body.priority in ('High','Critical','Emergency') else 'Info',body.assigned_to,None,'work',no)
        audit(conn,user['id'],'CREATE','Work Management',no,'',body.model_dump());return {'id':cur.lastrowid,'wo_no':no}
@app.patch('/api/work-orders/{wo_id}')
def update_work(wo_id:int,body:WorkOrderPatch,user=Depends(require_permission('work.write',*WRITE_ROLES))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        if changes:
            conn.execute('UPDATE work_orders SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),wo_id));audit(conn,user['id'],'UPDATE','Work Management',old['wo_no'],old,changes)
            if 'priority' in changes:_ensure_work_sla(conn,wo_id,force=True)
            if 'assigned_to' in changes and changes['assigned_to']:notify(conn,'Work order assigned',f"{old['wo_no']} — {changes.get('title',old['title'])}",'Info',changes['assigned_to'],None,'work',old['wo_no'])
        return {'ok':True}
TRANSITIONS={'Draft':{'submit':'Submitted'},'Rejected':{'resubmit':'Submitted'},'Submitted':{'approve':'Approved'},'Approved':{'assign':'Assigned'},'Assigned':{'start':'In Progress'},'In Progress':{'pause':'Assigned','complete':'Completed'},'Completed':{'close':'Closed'}}
ACTION_ROLES={
    'approve':('admin','maintenance_manager','supervisor'),
    'assign':('admin','maintenance_manager','planner','supervisor'),
    'close':('admin','maintenance_manager','supervisor'),
    'submit':('admin','maintenance_manager','planner','supervisor'),
    'resubmit':('admin','maintenance_manager','planner','supervisor'),
}
@app.post('/api/work-orders/{wo_id}/transition')
def transition_work(wo_id:int,body:TransitionIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');action=body.action.lower();target=TRANSITIONS.get(w['status'],{}).get(action)
        if not target: raise HTTPException(409,f"Action '{body.action}' is not valid from {w['status']}")
        if action in ACTION_ROLES and user['role'] not in ACTION_ROLES[action]: raise HTTPException(403,f"Role {user['role']} cannot perform {action}")
        if user['role']=='technician' and action in ('start','pause','complete') and w['assigned_to']!=user['id']: raise HTTPException(403,'Technicians can only execute work assigned to them')
        if action=='assign' and not w['assigned_to']: raise HTTPException(409,'Assign a technician before moving to Assigned')
        fields={'status':target,'updated_at':now()}
        if action=='start':fields['actual_start']=now()
        if action=='complete':fields['actual_finish']=now();fields['completion_notes']=body.notes or w['completion_notes'];fields['technician_signature']=body.signature or w.get('technician_signature','')
        conn.execute('UPDATE work_orders SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),wo_id))
        if action=='start':_mark_sla_response(conn,wo_id,fields['actual_start'])
        if action=='complete':_mark_sla_resolution(conn,wo_id,fields['actual_finish'])
        if action in ('submit','resubmit'):
            create_approval(conn,'Work Management','work_order',wo_id,w['wo_no'],f"Approve {w['wo_no']} — {w['title']}",user['id'],assigned_user_id=w['supervisor_id'],assigned_role=None if w['supervisor_id'] else 'maintenance_manager')
        if action=='approve':resolve_approval(conn,'Work Management','work_order',wo_id,'approve',user['id'],body.notes)
        if target=='Closed' and w['asset_id']:
            conn.execute('UPDATE assets SET last_maintenance=?,updated_at=? WHERE id=?',(date.today().isoformat(),now(),w['asset_id']))
        workflow_event(conn,'Work Management','work_order',wo_id,w['wo_no'],action.upper(),w['status'],target,user['id'],body.notes)
        audit(conn,user['id'],action.upper(),'Work Management',w['wo_no'],w['status'],target);notify(conn,'Work order status changed',f"{w['wo_no']} is now {target}",'Info',w['requested_by'],None,'work',w['wo_no'])
        return {'ok':True,'status':target}
@app.get('/api/work-orders/{wo_id}/parts-readiness')
def work_parts_readiness(wo_id:int,user=Depends(current_user)):
    with db() as conn:
        get_or_404(conn,'SELECT id FROM work_orders WHERE id=?',(wo_id,),'Work order not found');return _work_order_parts_readiness(conn,wo_id)

@app.post('/api/work-orders/{wo_id}/requirements')
def add_work_requirement(wo_id:int,body:WorkRequirementIn,user=Depends(require_permission('inventory.transact','admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');i=get_or_404(conn,'SELECT id,item_no FROM inventory_items WHERE id=?',(body.item_id,),'Inventory item not found')
        existing=conn.execute('SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(wo_id,body.item_id)).fetchone()
        if existing:
            conn.execute("UPDATE work_order_requirements SET quantity=?,required_by=?,status='Required' WHERE id=?",(body.quantity,body.required_by,existing['id']));rid=existing['id'];old={'quantity':existing['quantity']}
        else:
            cur=conn.execute('INSERT INTO work_order_requirements(work_order_id,inventory_item_id,quantity,required_by,status) VALUES(?,?,?,?,?)',(wo_id,body.item_id,body.quantity,body.required_by,'Required'));rid=cur.lastrowid;old=''
        audit(conn,user['id'],'PLAN MATERIAL','Work Management',w['wo_no'],old,{'item':i['item_no'],'quantity':body.quantity,'required_by':body.required_by})
        return {'id':rid,'readiness':_work_order_parts_readiness(conn,wo_id)}

@app.delete('/api/work-orders/{wo_id}/requirements/{requirement_id}')
def delete_work_requirement(wo_id:int,requirement_id:int,user=Depends(require_permission('inventory.transact','admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');r=get_or_404(conn,'SELECT * FROM work_order_requirements WHERE id=? AND work_order_id=?',(requirement_id,wo_id),'Requirement not found')
        conn.execute('DELETE FROM work_order_requirements WHERE id=?',(requirement_id,));audit(conn,user['id'],'REMOVE MATERIAL PLAN','Work Management',w['wo_no'],r,'');return {'ok':True}

@app.get('/api/work-orders/{wo_id}/reservations')
def list_work_reservations(wo_id:int,user=Depends(current_user)):
    with db() as conn:
        get_or_404(conn,'SELECT id FROM work_orders WHERE id=?',(wo_id,),'Work order not found');return _reservation_rows(conn,wo_id)

@app.post('/api/work-orders/{wo_id}/reservations')
def reserve_work_material(wo_id:int,body:ReservationIn,user=Depends(require_permission('inventory.transact','admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');i=get_or_404(conn,'SELECT * FROM inventory_items WHERE id=?',(body.item_id,),'Inventory item not found')
        available=float(i['current_stock'])-float(i['reserved_stock'])
        if available<body.quantity:raise HTTPException(409,f'Insufficient unreserved stock ({available:g} {i["unit"]})')
        no=next_no(conn,'inventory_reservations','reservation_no','RSV-',20001)
        cur=conn.execute("INSERT INTO inventory_reservations(reservation_no,work_order_id,inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at,notes) VALUES(?,?,?,?,0,'Reserved',?,?,?)",(no,wo_id,body.item_id,body.quantity,user['id'],now(),body.notes))
        _sync_reserved_stock(conn,body.item_id)
        req=conn.execute('SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(wo_id,body.item_id)).fetchone()
        if req:
            reserved=conn.execute("SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')",(wo_id,body.item_id)).fetchone()[0] or 0
            if float(reserved)>=float(req['quantity']):conn.execute("UPDATE work_order_requirements SET status='Reserved' WHERE id=?",(req['id'],))
        audit(conn,user['id'],'RESERVE MATERIAL','Work Management',w['wo_no'],'',{'reservation':no,'item':i['item_no'],'quantity':body.quantity})
        notify(conn,'Material reserved',f'{no} reserved {body.quantity:g} {i["unit"]} of {i["item_no"]} for {w["wo_no"]}','Info',w.get('assigned_to'),None,'work',w['wo_no'])
        return {'id':cur.lastrowid,'reservation_no':no,'readiness':_work_order_parts_readiness(conn,wo_id)}

@app.post('/api/work-orders/{wo_id}/reserve-all')
def reserve_all_work_materials(wo_id:int,user=Depends(require_permission('inventory.transact','admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');created=[];shortages=[]
        reqs=rows(conn.execute('SELECT * FROM work_order_requirements WHERE work_order_id=? AND status<>\'Cancelled\'',(wo_id,)))
        for req in reqs:
            item=get_or_404(conn,'SELECT * FROM inventory_items WHERE id=?',(req['inventory_item_id'],),'Inventory item not found')
            issued=float(conn.execute('SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',(wo_id,item['id'])).fetchone()[0] or 0)
            already=float(conn.execute("SELECT COALESCE(SUM(quantity-issued_quantity),0) FROM inventory_reservations WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued')",(wo_id,item['id'])).fetchone()[0] or 0)
            need=max(0.0,float(req['quantity'])-issued-already);available=max(0.0,float(item['current_stock'])-float(item['reserved_stock']))
            if need<=0:continue
            if available+1e-9<need:shortages.append({'item_no':item['item_no'],'required':round(need,3),'available':round(available,3)});continue
            no=next_no(conn,'inventory_reservations','reservation_no','RSV-',20001)
            conn.execute("INSERT INTO inventory_reservations(reservation_no,work_order_id,inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at,notes) VALUES(?,?,?,?,0,'Reserved',?,?,?)",(no,wo_id,item['id'],need,user['id'],now(),'Reserve all planned materials'))
            _sync_reserved_stock(conn,item['id']);created.append(no)
        audit(conn,user['id'],'RESERVE ALL','Work Management',w['wo_no'],'',{'reservations':created,'shortages':shortages})
        return {'created':created,'shortages':shortages,'readiness':_work_order_parts_readiness(conn,wo_id)}

@app.post('/api/reservations/{reservation_id}/release')
def release_reservation(reservation_id:int,user=Depends(require_permission('inventory.transact','admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        r=get_or_404(conn,'SELECT r.*,w.wo_no,i.item_no FROM inventory_reservations r JOIN work_orders w ON w.id=r.work_order_id JOIN inventory_items i ON i.id=r.inventory_item_id WHERE r.id=?',(reservation_id,),'Reservation not found')
        if r['status'] not in ('Reserved','Partially Issued'):raise HTTPException(409,f"Reservation is {r['status']}")
        conn.execute("UPDATE inventory_reservations SET status='Released',released_at=? WHERE id=?",(now(),reservation_id));_sync_reserved_stock(conn,r['inventory_item_id'])
        req=conn.execute('SELECT id FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(r['work_order_id'],r['inventory_item_id'])).fetchone()
        if req:conn.execute("UPDATE work_order_requirements SET status='Required' WHERE id=?",(req['id'],))
        audit(conn,user['id'],'RELEASE RESERVATION','Inventory',r['reservation_no'],r['status'],'Released');return {'ok':True,'readiness':_work_order_parts_readiness(conn,r['work_order_id'])}

@app.post('/api/reservations/{reservation_id}/issue')
def issue_reservation(reservation_id:int,body:ReservationIssueIn,user=Depends(require_permission('inventory.transact',*INV_ROLES))):
    with db() as conn:
        r=get_or_404(conn,'SELECT r.*,w.wo_no,w.asset_id,w.assigned_to,i.item_no,i.name,i.unit,i.unit_price,i.current_stock FROM inventory_reservations r JOIN work_orders w ON w.id=r.work_order_id JOIN inventory_items i ON i.id=r.inventory_item_id WHERE r.id=?',(reservation_id,),'Reservation not found')
        if user['role']=='technician' and r.get('assigned_to')!=user['id']:raise HTTPException(403,'Technicians can only issue materials for work assigned to them')
        if r['status'] not in ('Reserved','Partially Issued'):raise HTTPException(409,f"Reservation is {r['status']}")
        remaining=max(0.0,float(r['quantity'])-float(r['issued_quantity']));qty=remaining if body.quantity is None else float(body.quantity)
        if qty>remaining+1e-9:raise HTTPException(409,f'Reservation only has {remaining:g} remaining')
        if float(r['current_stock'])<qty:raise HTTPException(409,'Physical stock is below reserved quantity')
        new_issued=float(r['issued_quantity'])+qty;new_status='Issued' if new_issued+1e-9>=float(r['quantity']) else 'Partially Issued';cost=qty*float(r['unit_price'])
        conn.execute('UPDATE inventory_items SET current_stock=current_stock-? WHERE id=?',(qty,r['inventory_item_id']))
        conn.execute('UPDATE inventory_reservations SET issued_quantity=?,status=? WHERE id=?',(new_issued,new_status,reservation_id));_sync_reserved_stock(conn,r['inventory_item_id'])
        conn.execute('INSERT INTO inventory_transactions(item_id,tx_type,quantity,work_order_id,reference,user_id,created_at) VALUES(?,?,?,?,?,?,?)',(r['inventory_item_id'],'ISSUE',-qty,r['work_order_id'],r['reservation_no'],user['id'],now()))
        conn.execute('INSERT INTO work_order_materials(work_order_id,inventory_item_id,quantity,unit_cost,issued_at,issued_by) VALUES(?,?,?,?,?,?)',(r['work_order_id'],r['inventory_item_id'],qty,r['unit_price'],now(),user['id']))
        conn.execute('UPDATE work_orders SET actual_cost=actual_cost+?,updated_at=? WHERE id=?',(cost,now(),r['work_order_id']))
        post_cost(conn,{'id':r['work_order_id'],'asset_id':r.get('asset_id'),'wo_no':r['wo_no']},'Material',cost,qty,r['item_no'],user['id'])
        req=conn.execute('SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(r['work_order_id'],r['inventory_item_id'])).fetchone()
        if req:
            issued=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',(r['work_order_id'],r['inventory_item_id'])).fetchone()[0] or 0
            conn.execute('UPDATE work_order_requirements SET status=? WHERE id=?',('Fulfilled' if float(issued)>=float(req['quantity']) else 'Required',req['id']))
        audit(conn,user['id'],'ISSUE RESERVATION','Inventory',r['reservation_no'],r['status'],{'status':new_status,'issued':qty});return {'ok':True,'status':new_status,'issued_quantity':qty,'readiness':_work_order_parts_readiness(conn,r['work_order_id'])}

@app.post('/api/work-orders/{wo_id}/craft-requirements')
def add_work_craft_requirement(wo_id:int,body:CraftRequirementIn,user=Depends(require_permission('work.write','admin','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');c=get_or_404(conn,'SELECT craft_code FROM crafts WHERE id=? AND active=1',(body.craft_id,),'Craft not found')
        existing=conn.execute('SELECT id FROM work_order_craft_requirements WHERE work_order_id=? AND craft_id=?',(wo_id,body.craft_id)).fetchone()
        if existing:conn.execute('UPDATE work_order_craft_requirements SET planned_hours=? WHERE id=?',(body.planned_hours,existing['id']));rid=existing['id']
        else:
            cur=conn.execute('INSERT INTO work_order_craft_requirements(work_order_id,craft_id,planned_hours) VALUES(?,?,?)',(wo_id,body.craft_id,body.planned_hours));rid=cur.lastrowid
        audit(conn,user['id'],'PLAN CRAFT','Work Management',w['wo_no'],'',{'craft':c['craft_code'],'planned_hours':body.planned_hours});return {'id':rid}

@app.post('/api/work-orders/{wo_id}/labor')
def add_labor(wo_id:int,body:LaborIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');uid=body.user_id or user['id'];work_date=body.work_date or date.today().isoformat();cost=body.hours*body.labor_rate
        conn.execute('INSERT INTO labor_entries(work_order_id,user_id,hours,labor_rate,notes,work_date) VALUES(?,?,?,?,?,?)',(wo_id,uid,body.hours,body.labor_rate,body.notes,work_date));conn.execute('UPDATE work_orders SET actual_hours=actual_hours+?,actual_cost=actual_cost+?,updated_at=? WHERE id=?',(body.hours,cost,now(),wo_id));post_cost(conn,w,'Labor',cost,body.hours,f'{body.hours:g} h × {body.labor_rate:g}',user['id']);audit(conn,user['id'],'ADD LABOR','Work Management',w['wo_no'],'',{'hours':body.hours,'user_id':uid,'cost':cost});return {'ok':True}
@app.post('/api/work-orders/{wo_id}/materials')
def add_material(wo_id:int,body:MaterialIn,user=Depends(require_permission('inventory.transact',*INV_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');i=get_or_404(conn,'SELECT * FROM inventory_items WHERE id=?',(body.item_id,),'Inventory item not found')
        if user['role']=='technician' and w.get('assigned_to')!=user['id']:raise HTTPException(403,'Technicians can only issue materials for work assigned to them')
        own=rows(conn.execute("SELECT * FROM inventory_reservations WHERE work_order_id=? AND inventory_item_id=? AND status IN ('Reserved','Partially Issued') ORDER BY id",(wo_id,body.item_id)))
        own_reserved=sum(max(0.0,float(r['quantity'])-float(r['issued_quantity'])) for r in own);unreserved=max(0.0,float(i['current_stock'])-float(i['reserved_stock']));accessible=own_reserved+unreserved
        if accessible+1e-9<body.quantity:raise HTTPException(409,f'Insufficient accessible stock ({accessible:g} {i["unit"]}; {own_reserved:g} reserved for this work order)')
        remaining=float(body.quantity)
        for r in own:
            if remaining<=0:break
            balance=max(0.0,float(r['quantity'])-float(r['issued_quantity']));take=min(balance,remaining)
            if take<=0:continue
            new_issued=float(r['issued_quantity'])+take;st='Issued' if new_issued+1e-9>=float(r['quantity']) else 'Partially Issued'
            conn.execute('UPDATE inventory_reservations SET issued_quantity=?,status=? WHERE id=?',(new_issued,st,r['id']));remaining-=take
        cost=body.quantity*float(i['unit_price']);conn.execute('UPDATE inventory_items SET current_stock=current_stock-? WHERE id=?',(body.quantity,body.item_id));_sync_reserved_stock(conn,body.item_id)
        conn.execute('INSERT INTO inventory_transactions(item_id,tx_type,quantity,work_order_id,reference,user_id,created_at) VALUES(?,?,?,?,?,?,?)',(body.item_id,'ISSUE',-body.quantity,wo_id,w['wo_no'],user['id'],now()));conn.execute('INSERT INTO work_order_materials(work_order_id,inventory_item_id,quantity,unit_cost,issued_at,issued_by) VALUES(?,?,?,?,?,?)',(wo_id,body.item_id,body.quantity,i['unit_price'],now(),user['id']));conn.execute('UPDATE work_orders SET actual_cost=actual_cost+?,updated_at=? WHERE id=?',(cost,now(),wo_id));post_cost(conn,w,'Material',cost,body.quantity,i['item_no'],user['id']);audit(conn,user['id'],'ISSUE MATERIAL','Work Management',w['wo_no'],'',{'item':i['item_no'],'qty':body.quantity,'cost':cost,'reservation_consumed':round(body.quantity-remaining,3)})
        req=conn.execute('SELECT id,quantity FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(wo_id,body.item_id)).fetchone()
        if req:
            issued=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM work_order_materials WHERE work_order_id=? AND inventory_item_id=?',(wo_id,body.item_id)).fetchone()[0] or 0
            conn.execute('UPDATE work_order_requirements SET status=? WHERE id=?',('Fulfilled' if float(issued)>=float(req['quantity']) else 'Required',req['id']))
        fresh=conn.execute('SELECT current_stock,reserved_stock,reorder_point FROM inventory_items WHERE id=?',(body.item_id,)).fetchone();new_stock=float(fresh['current_stock'])
        if new_stock-float(fresh['reserved_stock'])<=float(fresh['reorder_point']):notify(conn,'Inventory below reorder point',f"{i['item_no']} — {i['name']} has {new_stock:g} {i['unit']} remaining",'Warning',None,'storekeeper','inventory',i['item_no'])
        return {'ok':True,'stock':new_stock,'cost':cost,'readiness':_work_order_parts_readiness(conn,wo_id)}
@app.post('/api/work-orders/{wo_id}/notes')
def add_work_note(wo_id:int,body:NoteIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        entry=f"[{now()}] {user['full_name']}: {body.note}"; new=(w['comments']+'\n'+entry).strip()
        conn.execute('UPDATE work_orders SET comments=?,updated_at=? WHERE id=?',(new,now(),wo_id));audit(conn,user['id'],'ADD NOTE','Work Management',w['wo_no'],w['comments'],new);return {'ok':True}
@app.post('/api/work-orders/{wo_id}/tasks/{task_id}/toggle')
def toggle_work_task(wo_id:int,task_id:int,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');t=get_or_404(conn,'SELECT * FROM work_order_tasks WHERE id=? AND work_order_id=?',(task_id,wo_id),'Task not found');new='Pending' if t['status']=='Completed' else 'Completed';conn.execute('UPDATE work_order_tasks SET status=?,completed_at=? WHERE id=?',(new,now() if new=='Completed' else None,task_id));audit(conn,user['id'],'TASK '+new.upper(),'Work Management',w['wo_no'],t['status'],new);return {'ok':True,'status':new}
@app.post('/api/field/assets/{asset_id}/condition-meter')
def field_asset_update(asset_id:int,body:FieldAssetUpdate,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        a=get_or_404(conn,'SELECT * FROM assets WHERE id=?',(asset_id,),'Asset not found');changes={}
        if body.condition is not None:changes['condition']=body.condition
        if body.meter_reading is not None:changes['meter_reading']=body.meter_reading
        if not changes:return {'ok':True}
        conn.execute('UPDATE assets SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),asset_id));audit(conn,user['id'],'FIELD UPDATE','Assets',a['asset_no'],{k:a[k] for k in changes},changes);return {'ok':True}

@app.get('/api/work-orders/{wo_id}/report')
def work_report(wo_id:int,user=Depends(current_user)):
    with db() as conn:
        w=get_or_404(conn,WO_SELECT+' WHERE w.id=?',(wo_id,),'Work order not found');labor=rows(conn.execute('SELECT le.*,u.full_name FROM labor_entries le JOIN users u ON u.id=le.user_id WHERE work_order_id=?',(wo_id,)));mats=rows(conn.execute('SELECT wom.*,i.item_no,i.name FROM work_order_materials wom JOIN inventory_items i ON i.id=wom.inventory_item_id WHERE work_order_id=?',(wo_id,)))
    html=f'''<html><head><title>{w['wo_no']}</title><style>body{{font-family:Arial;margin:40px;color:#172033}}h1{{color:#c9272c}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}small{{color:#666}}</style></head><body><h1>ELSEWEDY UTILITIES</h1><h2>{w['wo_no']} — {w['title']}</h2><p><b>Asset:</b> {w.get('asset_no') or '-'} {w.get('asset_name') or ''}<br><b>Status:</b> {w['status']} | <b>Priority:</b> {w['priority']} | <b>Type:</b> {w['work_type']}<br><b>Assigned:</b> {w.get('assigned_to_name') or '-'} | <b>Supervisor:</b> {w.get('supervisor_name') or '-'}</p><h3>Description</h3><p>{w['description']}</p><h3>Instructions / Safety</h3><p>{w['instructions']}<br>{w['safety_requirements']}</p><h3>Labor</h3><table><tr><th>Person</th><th>Hours</th><th>Notes</th></tr>{''.join(f"<tr><td>{x['full_name']}</td><td>{x['hours']}</td><td>{x['notes']}</td></tr>" for x in labor)}</table><h3>Materials</h3><table><tr><th>Item</th><th>Qty</th><th>Unit Cost</th></tr>{''.join(f"<tr><td>{x['item_no']} {x['name']}</td><td>{x['quantity']}</td><td>{x['unit_cost']}</td></tr>" for x in mats)}</table><p><b>Actual Cost:</b> {w['actual_cost']:.2f}</p><small>Generated by EUAS — Developed by Omar & Seif</small></body></html>'''
    return HTMLResponse(html)

# ---------- PM ----------
@app.get('/api/maintenance-plans')
def list_pm(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT p.*,a.asset_no,a.name asset_name,a.meter_reading FROM maintenance_plans p JOIN assets a ON a.id=p.asset_id ORDER BY COALESCE(p.next_due,'9999-12-31'),p.pm_no'''))
@app.post('/api/maintenance-plans')
def create_pm(body:PMIn,user=Depends(require_permission('work.write',*WRITE_ROLES))):
    with db() as conn:
        no=next_no(conn,'maintenance_plans','pm_no','PM-',1000);cur=conn.execute('''INSERT INTO maintenance_plans(pm_no,name,asset_id,trigger_type,interval_days,meter_interval,next_due,priority,job_plan) VALUES(?,?,?,?,?,?,?,?,?)''',(no,body.name,body.asset_id,body.trigger_type,body.interval_days,body.meter_interval,body.next_due,body.priority,body.job_plan));audit(conn,user['id'],'CREATE','Preventive Maintenance',no,'',body.model_dump());return {'id':cur.lastrowid,'pm_no':no}
@app.post('/api/maintenance-plans/generate')
def generate_pm(user=Depends(require_permission('work.write',*WRITE_ROLES))):
    with db() as conn:
        generated=_generate_due_pm(conn,user['id'],date.today())
        return {'count':len(generated),'generated':generated}

# ---------- inventory ----------
@app.get('/api/inventory')
def list_inventory(q:str='',user=Depends(current_user)):
    sql='''SELECT i.*,w.name warehouse_name,w.warehouse_code,v.name vendor_name,(i.current_stock-i.reserved_stock) available_stock,CASE WHEN i.current_stock<=0 THEN 'Out of Stock' WHEN i.current_stock-i.reserved_stock<=i.reorder_point THEN 'Low Stock' WHEN i.max_level>0 AND i.current_stock>i.max_level THEN 'Overstock' ELSE 'Normal' END stock_status FROM inventory_items i JOIN warehouses w ON w.id=i.warehouse_id LEFT JOIN vendors v ON v.id=i.vendor_id WHERE 1=1''';args=[]
    if q:sql+=' AND (i.item_no LIKE ? OR i.name LIKE ? OR i.category LIKE ?)';like=f'%{q}%';args+=[like]*3
    sql+=' ORDER BY i.item_no'
    with db() as conn:return rows(conn.execute(sql,args))
@app.post('/api/inventory')
def create_inventory(body:InventoryIn,user=Depends(require_permission('inventory.transact','admin','storekeeper','maintenance_manager'))):
    with db() as conn:
        no=next_no(conn,'inventory_items','item_no','ITM-',1000);vals=body.model_dump();cols=list(vals);cur=conn.execute(f"INSERT INTO inventory_items(item_no,{','.join(cols)}) VALUES(?,{','.join('?'*len(cols))})",(no,*vals.values()));audit(conn,user['id'],'CREATE','Inventory',no,'',vals);return {'id':cur.lastrowid,'item_no':no}
@app.post('/api/inventory/{item_id}/transaction')
def inventory_tx(item_id:int,body:InventoryTxIn,user=Depends(require_permission('inventory.transact',*INV_ROLES))):
    with db() as conn:
        i=get_or_404(conn,'SELECT * FROM inventory_items WHERE id=?',(item_id,),'Item not found');tx=body.tx_type.upper();q=body.quantity
        if tx=='ISSUE':
            q=-abs(q)
            available=max(float(i['current_stock'])-float(i['reserved_stock']),0)
            if available<abs(q):raise HTTPException(409,'Insufficient unreserved stock; release or issue the work-order reservation first')
        elif tx=='RETURN' or tx=='RECEIPT':q=abs(q)
        elif tx=='ADJUSTMENT':q=body.quantity-i['current_stock']
        elif tx=='TRANSFER':
            if not body.to_warehouse_id:raise HTTPException(400,'Destination warehouse required')
            if body.to_warehouse_id==i['warehouse_id']:raise HTTPException(400,'Destination warehouse must be different')
            move=abs(q)
            available=max(float(i['current_stock'])-float(i['reserved_stock']),0)
            if available<move:raise HTTPException(409,'Insufficient unreserved stock; reserved material cannot be transferred')
            q=-move
            dest=conn.execute('SELECT * FROM inventory_items WHERE warehouse_id=? AND name=? AND category=?',(body.to_warehouse_id,i['name'],i['category'])).fetchone()
            if dest:
                conn.execute('UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',(move,dest['id']));dest_id=dest['id']
            else:
                dno=next_no(conn,'inventory_items','item_no','ITM-',1000);curd=conn.execute('''INSERT INTO inventory_items(item_no,name,description,category,warehouse_id,current_stock,reserved_stock,min_level,max_level,reorder_point,unit_price,unit,vendor_id,bin) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)''',(dno,i['name'],i['description'],i['category'],body.to_warehouse_id,move,i['min_level'],i['max_level'],i['reorder_point'],i['unit_price'],i['unit'],i['vendor_id'],i['bin']));dest_id=curd.lastrowid
            conn.execute('INSERT INTO inventory_transactions(item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,reference,user_id,created_at) VALUES(?,?,?,?,?,?,?,?)',(dest_id,'TRANSFER',move,i['warehouse_id'],body.to_warehouse_id,body.reference or i['item_no'],user['id'],now()))
        else:raise HTTPException(400,'Invalid transaction type')
        new=i['current_stock']+q;conn.execute('UPDATE inventory_items SET current_stock=? WHERE id=?',(new,item_id));conn.execute('INSERT INTO inventory_transactions(item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,work_order_id,reference,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(item_id,tx,q,i['warehouse_id'],body.to_warehouse_id,body.work_order_id,body.reference,user['id'],now()));audit(conn,user['id'],tx,'Inventory',i['item_no'],i['current_stock'],new)
        if new-i['reserved_stock']<=i['reorder_point']:notify(conn,'Inventory below reorder point',f"{i['item_no']} — {i['name']} is below reorder point",'Warning',None,'storekeeper','inventory',i['item_no'])
        return {'ok':True,'current_stock':new}
@app.get('/api/inventory/{item_id}/transactions')
def inventory_history(item_id:int,user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT t.*,u.full_name,w.wo_no FROM inventory_transactions t JOIN users u ON u.id=t.user_id LEFT JOIN work_orders w ON w.id=t.work_order_id WHERE t.item_id=? ORDER BY t.id DESC''',(item_id,)))
@app.post('/api/inventory/reorder-scan')
def reorder_scan(user=Depends(require_permission('inventory.transact','admin','storekeeper','maintenance_manager','procurement'))):
    with db() as conn:
        created=_run_reorder_scan(conn,user['id'])
        return {'count':len(created),'created':created}

# ---------- procurement ----------
@app.get('/api/procurement')
def procurement(user=Depends(current_user)):
    with db() as conn:
        prs=rows(conn.execute('''SELECT pr.*,u.full_name requester,s.name site_name FROM purchase_requisitions pr LEFT JOIN users u ON u.id=pr.requester_id LEFT JOIN sites s ON s.id=pr.site_id ORDER BY pr.id DESC'''))
        for pr in prs:pr['items']=rows(conn.execute('SELECT x.*,i.item_no FROM purchase_requisition_items x LEFT JOIN inventory_items i ON i.id=x.inventory_item_id WHERE x.pr_id=?',(pr['id'],)))
        pos=rows(conn.execute('''SELECT po.*,v.name vendor_name,pr.pr_no FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id LEFT JOIN purchase_requisitions pr ON pr.id=po.pr_id ORDER BY po.id DESC'''))
        quotes=rows(conn.execute('''SELECT q.*,v.name vendor_name,pr.pr_no FROM quotations q JOIN vendors v ON v.id=q.vendor_id JOIN purchase_requisitions pr ON pr.id=q.pr_id ORDER BY q.id DESC'''))
        return {'requisitions':prs,'purchase_orders':pos,'quotations':quotes}
@app.post('/api/procurement/requisitions')
def create_pr(body:PRIn,user=Depends(require_permission('procurement.write','admin','storekeeper','maintenance_manager','procurement','planner'))):
    with db() as conn:
        no=next_no(conn,'purchase_requisitions','pr_no','PR-',8001);total=sum(float(x.get('quantity',0))*float(x.get('estimated_unit_cost',0)) for x in body.items);cur=conn.execute('INSERT INTO purchase_requisitions(pr_no,title,requester_id,site_id,work_order_id,project_id,status,justification,total_estimate,created_at) VALUES(?,?,?,?,?,?,\'Draft\',?,?,?)',(no,body.title,user['id'],body.site_id,body.work_order_id,body.project_id,body.justification,total,now()))
        for x in body.items:conn.execute('INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,x.get('inventory_item_id'),x.get('description','Item'),x.get('quantity',1),x.get('estimated_unit_cost',0)))
        audit(conn,user['id'],'CREATE','Procurement',no,'',body.model_dump());return {'id':cur.lastrowid,'pr_no':no}
@app.post('/api/procurement/requisitions/{pr_id}/submit')
def submit_pr(pr_id:int,user=Depends(require_permission('procurement.write','admin','storekeeper','maintenance_manager','procurement','planner'))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(pr_id,),'PR not found')
        if pr['status'] not in ('Draft','Rejected'): raise HTTPException(409,'Only Draft or Rejected requisitions can be submitted')
        conn.execute("UPDATE purchase_requisitions SET status='Submitted' WHERE id=?",(pr_id,))
        create_approval(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],f"Approve {pr['pr_no']} — {pr['title']}",user['id'],assigned_role='procurement')
        workflow_event(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],'SUBMIT',pr['status'],'Submitted',user['id'])
        audit(conn,user['id'],'SUBMIT','Procurement',pr['pr_no'],pr['status'],'Submitted');return {'ok':True,'status':'Submitted'}

@app.post('/api/procurement/requisitions/{pr_id}/approve')
def approve_pr(pr_id:int,user=Depends(require_permission('procurement.write',*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(pr_id,),'PR not found')
        if pr['status']!='Submitted': raise HTTPException(409,'Purchase requisition must be Submitted before approval')
        conn.execute("UPDATE purchase_requisitions SET status='Approved',approved_at=? WHERE id=?",(now(),pr_id));resolve_approval(conn,'Procurement','purchase_requisition',pr_id,'approve',user['id']);workflow_event(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],'APPROVE',pr['status'],'Approved',user['id']);audit(conn,user['id'],'APPROVE','Procurement',pr['pr_no'],pr['status'],'Approved');return {'ok':True}
@app.post('/api/procurement/quotations')
def create_quote(body:QuoteIn,user=Depends(require_permission('procurement.write',*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT pr_no FROM purchase_requisitions WHERE id=?',(body.pr_id,),'PR not found');get_or_404(conn,'SELECT id FROM vendors WHERE id=?',(body.vendor_id,),'Vendor not found');no=next_no(conn,'quotations','quote_no','RFQ-',8101);cur=conn.execute("INSERT INTO quotations(quote_no,pr_id,vendor_id,amount,valid_until,status) VALUES(?,?,?,?,?,'Received')",(no,body.pr_id,body.vendor_id,body.amount,body.valid_until));audit(conn,user['id'],'ADD QUOTE','Procurement',no,'',{'pr':pr['pr_no'],'amount':body.amount});return {'id':cur.lastrowid,'quote_no':no}

@app.post('/api/procurement/purchase-orders')
def create_po(body:POIn,user=Depends(require_permission('procurement.write',*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(body.pr_id,),'PR not found')
        if pr['status']!='Approved':raise HTTPException(409,'Purchase requisition must be approved first')
        no=next_no(conn,'purchase_orders','po_no','PO-',9001);cur=conn.execute('INSERT INTO purchase_orders(po_no,pr_id,vendor_id,status,order_date,expected_delivery,total_cost,work_order_id,project_id) VALUES(?,?,?,\'Ordered\',?,?,?,?,?)',(no,body.pr_id,body.vendor_id,date.today().isoformat(),body.expected_delivery,pr['total_estimate'],pr['work_order_id'],pr['project_id']));conn.execute("UPDATE purchase_requisitions SET status='Ordered' WHERE id=?",(body.pr_id,));items=rows(conn.execute('SELECT * FROM purchase_requisition_items WHERE pr_id=?',(body.pr_id,)))
        for x in items:conn.execute('INSERT INTO purchase_order_items(po_id,inventory_item_id,description,quantity,unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,x['inventory_item_id'],x['description'],x['quantity'],x['estimated_unit_cost']))
        audit(conn,user['id'],'CREATE PO','Procurement',no,'',{'pr':pr['pr_no']});return {'id':cur.lastrowid,'po_no':no}
@app.post('/api/procurement/purchase-orders/{po_id}/receive')
def receive_po(po_id:int,user=Depends(require_permission('procurement.write','admin','procurement','storekeeper'))):
    with db() as conn:
        po=get_or_404(conn,'SELECT * FROM purchase_orders WHERE id=?',(po_id,),'PO not found')
        if po['status']=='Received':raise HTTPException(409,'PO already received')
        items=rows(conn.execute('SELECT * FROM purchase_order_items WHERE po_id=?',(po_id,)))
        for x in items:
            if x['inventory_item_id']:
                item=conn.execute('SELECT * FROM inventory_items WHERE id=?',(x['inventory_item_id'],)).fetchone();conn.execute('UPDATE inventory_items SET current_stock=current_stock+? WHERE id=?',(x['quantity'],x['inventory_item_id']));conn.execute('INSERT INTO inventory_transactions(item_id,tx_type,quantity,from_warehouse_id,reference,user_id,created_at) VALUES(?,?,?,?,?,?,?)',(x['inventory_item_id'],'RECEIPT',x['quantity'],item['warehouse_id'],po['po_no'],user['id'],now()))
        conn.execute("UPDATE purchase_orders SET status='Received',actual_receipt=? WHERE id=?",(date.today().isoformat(),po_id));
        if po['pr_id']:conn.execute("UPDATE purchase_requisitions SET status='Received' WHERE id=?",(po['pr_id'],))
        audit(conn,user['id'],'RECEIVE','Procurement',po['po_no'],po['status'],'Received');return {'ok':True}

# ---------- outages / operational availability ----------
@app.get('/api/outages')
def list_outages(status:str='',site_id:Optional[int]=None,asset_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT o.*,a.asset_no,a.name asset_name,s.site_code,s.name site_name,w.wo_no,u.full_name reported_by_name
      FROM asset_outages o JOIN assets a ON a.id=o.asset_id LEFT JOIN sites s ON s.id=o.site_id LEFT JOIN work_orders w ON w.id=o.work_order_id JOIN users u ON u.id=o.reported_by WHERE 1=1""";args=[]
    if status:sql+=' AND o.status=?';args.append(status)
    if site_id:sql+=' AND o.site_id=?';args.append(site_id)
    if asset_id:sql+=' AND o.asset_id=?';args.append(asset_id)
    sql+=' ORDER BY o.start_at DESC,o.id DESC'
    with db() as conn:
        out=rows(conn.execute(sql,args));now_dt=datetime.now()
        for x in out:x['duration_hours']=round(_outage_overlap_hours(x['start_at'],x.get('end_at'),datetime(2000,1,1),now_dt),2)
        return out

@app.post('/api/outages')
def create_outage(body:OutageIn,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        a=get_or_404(conn,"SELECT a.*,l.site_id FROM assets a LEFT JOIN locations l ON l.id=a.location_id WHERE a.id=?",(body.asset_id,),'Asset not found')
        if body.work_order_id:get_or_404(conn,'SELECT id FROM work_orders WHERE id=?',(body.work_order_id,),'Work order not found')
        start_at=body.start_at or now();_dt(start_at)
        no=next_no(conn,'asset_outages','outage_no','OUT-',30001)
        cur=conn.execute("INSERT INTO asset_outages(outage_no,asset_id,site_id,work_order_id,outage_type,status,cause_code,impact,lost_capacity,capacity_unit,start_at,reported_by,created_at,updated_at) VALUES(?,?,?,?,?,'Open',?,?,?,?,?,?,?,?)",(no,body.asset_id,a.get('site_id'),body.work_order_id,body.outage_type,body.cause_code,body.impact,body.lost_capacity,body.capacity_unit,start_at,user['id'],now(),now()))
        if a['status'] in ('Operating','Standby'):conn.execute("UPDATE assets SET status='Under Maintenance',updated_at=? WHERE id=?",(now(),body.asset_id))
        audit(conn,user['id'],'OPEN OUTAGE','Operations',no,'',body.model_dump());emit_event(conn,'asset.outage.opened','asset',body.asset_id,{'outage_no':no,'asset_no':a['asset_no'],'type':body.outage_type,'start_at':start_at})
        notify(conn,'Asset outage opened',f'{no} — {a["asset_no"]} is unavailable','High' if body.outage_type=='Forced' else 'Warning',None,'maintenance_manager','operations',no)
        return {'id':cur.lastrowid,'outage_no':no,'status':'Open'}

@app.post('/api/outages/{outage_id}/close')
def close_outage(outage_id:int,body:OutageCloseIn,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        o=get_or_404(conn,'SELECT o.*,a.asset_no FROM asset_outages o JOIN assets a ON a.id=o.asset_id WHERE o.id=?',(outage_id,),'Outage not found')
        if o['status']!='Open':raise HTTPException(409,'Outage is already closed')
        end_at=body.end_at or now()
        if _dt(end_at)<=_dt(o['start_at']):raise HTTPException(400,'Outage end must be after start')
        impact=body.impact if body.impact is not None else o['impact'];conn.execute("UPDATE asset_outages SET status='Closed',end_at=?,impact=?,updated_at=? WHERE id=?",(end_at,impact,now(),outage_id))
        other=conn.execute("SELECT COUNT(*) FROM asset_outages WHERE asset_id=? AND status='Open' AND id<>?",(o['asset_id'],outage_id)).fetchone()[0]
        if not other:conn.execute("UPDATE assets SET status='Operating',updated_at=? WHERE id=?",(now(),o['asset_id']))
        hours=_outage_overlap_hours(o['start_at'],end_at,_dt(o['start_at']),_dt(end_at));audit(conn,user['id'],'CLOSE OUTAGE','Operations',o['outage_no'],'Open',{'status':'Closed','duration_hours':round(hours,2)})
        emit_event(conn,'asset.outage.closed','asset',o['asset_id'],{'outage_no':o['outage_no'],'asset_no':o['asset_no'],'end_at':end_at,'duration_hours':round(hours,2)})
        return {'ok':True,'status':'Closed','duration_hours':round(hours,2)}

# ---------- offline field synchronization ----------
def _field_sync_hash(value:dict):
    payload=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def _field_sync_work_access(conn,user,work_order_id:int):
    w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(work_order_id,),'Work order not found')
    if user['role']=='technician' and int(w['assigned_to'] or 0)!=int(user['id']):
        raise HTTPException(403,'Technicians can only synchronize work assigned to them')
    return dict(w)

def _field_sync_entity_state(conn,user,entity_type:str,entity_id:int):
    kind=entity_type.strip().lower()
    if kind=='work_order':
        w=_field_sync_work_access(conn,user,entity_id)
        state={'id':w['id'],'wo_no':w['wo_no'],'status':w['status'],'assigned_to':w.get('assigned_to'),'actual_start':w.get('actual_start'),'actual_finish':w.get('actual_finish'),'completion_notes':w.get('completion_notes') or '','technician_signature':w.get('technician_signature') or ''}
    elif kind=='work_order_task':
        r=get_or_404(conn,'''SELECT t.*,w.wo_no,w.assigned_to FROM work_order_tasks t JOIN work_orders w ON w.id=t.work_order_id WHERE t.id=?''',(entity_id,),'Work-order task not found')
        if user['role']=='technician' and int(r['assigned_to'] or 0)!=int(user['id']):raise HTTPException(403,'Technicians can only synchronize tasks on their assigned work')
        state={'id':r['id'],'work_order_id':r['work_order_id'],'wo_no':r['wo_no'],'task':r['task'],'status':r['status'],'completed_at':r.get('completed_at')}
    elif kind=='asset':
        a=get_or_404(conn,'SELECT id,asset_no,condition,meter_reading FROM assets WHERE id=?',(entity_id,),'Asset not found')
        if user['role']=='technician':
            allowed=conn.execute("SELECT id FROM work_orders WHERE asset_id=? AND assigned_to=? AND status NOT IN ('Closed','Cancelled') LIMIT 1",(entity_id,user['id'])).fetchone()
            if not allowed:raise HTTPException(403,'Technicians can only synchronize assets linked to their assigned work')
        state={'id':a['id'],'asset_no':a['asset_no'],'condition':a['condition'],'meter_reading':a['meter_reading']}
    elif kind=='dispatch':
        d=get_or_404(conn,'''SELECT d.*,w.wo_no,w.assigned_to FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.id=?''',(entity_id,),'Dispatch not found')
        if user['role']=='technician' and int(d['technician_user_id'])!=int(user['id']):raise HTTPException(403,'Technicians can only synchronize their own dispatch')
        state={'id':d['id'],'dispatch_no':d['dispatch_no'],'work_order_id':d['work_order_id'],'wo_no':d['wo_no'],'status':d['status'],'accepted_at':d.get('accepted_at'),'enroute_at':d.get('enroute_at'),'arrived_at':d.get('arrived_at'),'completed_at':d.get('completed_at'),'cancelled_at':d.get('cancelled_at')}
    else:
        raise HTTPException(400,'Unsupported field-sync entity type')
    return {'state':state,'hash':_field_sync_hash(state)}

def _field_sync_apply(conn,user,entity_type:str,entity_id:int,operation_type:str,payload:dict):
    kind=entity_type.strip().lower(); op=operation_type.strip().lower(); stamp=now()
    if kind=='work_order' and op=='append_note':
        w=_field_sync_work_access(conn,user,entity_id);note=str(payload.get('note') or '').strip()
        if not note:raise HTTPException(400,'Field note is required')
        if len(note)>2000:raise HTTPException(400,'Field note is too long')
        entry=f"[{stamp}] {user['full_name']}: {note}";new=((w.get('comments') or '')+'\n'+entry).strip()
        conn.execute('UPDATE work_orders SET comments=?,updated_at=? WHERE id=?',(new,stamp,entity_id));audit(conn,user['id'],'ADD NOTE','Field Sync',w['wo_no'],w.get('comments') or '',new)
        return {'ok':True,'note_appended':True}
    if kind=='work_order' and op=='transition':
        w=_field_sync_work_access(conn,user,entity_id);action=str(payload.get('action') or '').lower();target=TRANSITIONS.get(w['status'],{}).get(action)
        if not target:raise HTTPException(409,f"Action '{action}' is not valid from {w['status']}")
        if action in ACTION_ROLES and user['role'] not in ACTION_ROLES[action]:raise HTTPException(403,f"Role {user['role']} cannot perform {action}")
        if user['role']=='technician' and action not in ('start','pause','complete'):raise HTTPException(403,'Technicians can only start, pause or complete assigned work offline')
        if action=='assign' and not w.get('assigned_to'):raise HTTPException(409,'Assign a technician before moving to Assigned')
        fields={'status':target,'updated_at':stamp};notes=str(payload.get('notes') or '');signature=str(payload.get('signature') or '')
        if action=='start':fields['actual_start']=stamp
        if action=='complete':fields['actual_finish']=stamp;fields['completion_notes']=notes or w.get('completion_notes','');fields['technician_signature']=signature or w.get('technician_signature','')
        conn.execute('UPDATE work_orders SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),entity_id))
        if action=='start':_mark_sla_response(conn,entity_id,stamp)
        if action=='complete':_mark_sla_resolution(conn,entity_id,stamp)
        if action in ('submit','resubmit'):create_approval(conn,'Work Management','work_order',entity_id,w['wo_no'],f"Approve {w['wo_no']} — {w['title']}",user['id'],assigned_user_id=w.get('supervisor_id'),assigned_role=None if w.get('supervisor_id') else 'maintenance_manager')
        if action=='approve':resolve_approval(conn,'Work Management','work_order',entity_id,'approve',user['id'],notes)
        if target=='Closed' and w.get('asset_id'):conn.execute('UPDATE assets SET last_maintenance=?,updated_at=? WHERE id=?',(date.today().isoformat(),stamp,w['asset_id']))
        workflow_event(conn,'Work Management','work_order',entity_id,w['wo_no'],action.upper(),w['status'],target,user['id'],notes);audit(conn,user['id'],action.upper(),'Field Sync',w['wo_no'],w['status'],target)
        notify(conn,'Work order status changed',f"{w['wo_no']} is now {target}",'Info',w.get('requested_by'),None,'work',w['wo_no'])
        return {'ok':True,'status':target}
    if kind=='work_order_task' and op=='set_status':
        r=get_or_404(conn,'''SELECT t.*,w.wo_no,w.assigned_to FROM work_order_tasks t JOIN work_orders w ON w.id=t.work_order_id WHERE t.id=?''',(entity_id,),'Work-order task not found')
        if user['role']=='technician' and int(r['assigned_to'] or 0)!=int(user['id']):raise HTTPException(403,'Technicians can only synchronize tasks on their assigned work')
        target=str(payload.get('status') or '')
        if target not in ('Pending','Completed'):raise HTTPException(400,'Task status must be Pending or Completed')
        if r['status']!=target:
            conn.execute('UPDATE work_order_tasks SET status=?,completed_at=? WHERE id=?',(target,stamp if target=='Completed' else None,entity_id));audit(conn,user['id'],'TASK '+target.upper(),'Field Sync',r['wo_no'],r['status'],target)
        return {'ok':True,'status':target}
    if kind=='asset' and op=='update':
        current=_field_sync_entity_state(conn,user,'asset',entity_id)['state'];changes={}
        if 'condition' in payload and payload.get('condition') is not None:
            condition=str(payload.get('condition'));allowed=('Good','Fair','Warning','Poor','Critical')
            if condition not in allowed:raise HTTPException(400,'Invalid asset condition')
            changes['condition']=condition
        if 'meter_reading' in payload and payload.get('meter_reading') is not None:
            try:changes['meter_reading']=float(payload.get('meter_reading'))
            except (TypeError,ValueError):raise HTTPException(400,'Meter reading must be numeric')
        if not changes:raise HTTPException(400,'No asset changes supplied')
        conn.execute('UPDATE assets SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),stamp,entity_id));audit(conn,user['id'],'FIELD UPDATE','Field Sync',current['asset_no'],{k:current.get(k) for k in changes},changes)
        return {'ok':True,'changes':changes}
    if kind=='dispatch' and op=='transition':
        d=get_or_404(conn,'SELECT d.*,w.wo_no,w.status work_status FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.id=?',(entity_id,),'Dispatch not found');action=str(payload.get('action') or '').lower().replace(' ','');mapping={'accept':('Dispatched','Accepted','accepted_at'),'enroute':('Accepted','En Route','enroute_at'),'arrive':('En Route','On Site','arrived_at'),'complete':('On Site','Completed','completed_at')}
        elevated=user['role'] in ('admin','maintenance_manager','planner','supervisor')
        if user['role']=='technician' and int(d['technician_user_id'])!=int(user['id']):raise HTTPException(403,'Technicians can only update their own dispatch')
        if action=='cancel':
            if not elevated:raise HTTPException(403,'Only planners/supervisors can cancel dispatch')
            if d['status'] in ('Completed','Cancelled'):raise HTTPException(409,f"Dispatch is {d['status']}")
            target='Cancelled';field='cancelled_at'
        else:
            if action not in mapping:raise HTTPException(400,'Action must be accept, enroute, arrive, complete or cancel')
            expected,target,field=mapping[action]
            if d['status']!=expected:raise HTTPException(409,f"Action {action} requires {expected}, current status is {d['status']}")
        notes=str(payload.get('notes') or '');conn.execute(f"UPDATE dispatch_assignments SET status=?,{field}=?,notes=CASE WHEN ?<>'' THEN ? ELSE notes END WHERE id=?",(target,stamp,notes,notes,entity_id))
        if action=='arrive' and d['work_status']=='Assigned':
            conn.execute("UPDATE work_orders SET status='In Progress',actual_start=COALESCE(actual_start,?),updated_at=? WHERE id=?",(stamp,stamp,d['work_order_id']));_mark_sla_response(conn,d['work_order_id'],stamp);workflow_event(conn,'Work Management','work_order',d['work_order_id'],d['wo_no'],'ARRIVE','Assigned','In Progress',user['id'],notes)
        audit(conn,user['id'],'DISPATCH '+action.upper(),'Field Sync',d['dispatch_no'],d['status'],target);return {'ok':True,'status':target}
    raise HTTPException(400,'Unsupported field-sync operation for this entity')

def _field_sync_register_client(conn,user,client_id:str,device_name:str='',pulled:bool=False):
    stamp=now();r=conn.execute('SELECT * FROM field_sync_clients WHERE client_id=?',(client_id,)).fetchone()
    if r and int(r['user_id'])!=int(user['id']):raise HTTPException(409,'Field sync client ID is already registered to another user')
    if r:
        conn.execute("UPDATE field_sync_clients SET device_name=CASE WHEN ?<>'' THEN ? ELSE device_name END,last_seen_at=?,last_pull_at=CASE WHEN ?=1 THEN ? ELSE last_pull_at END WHERE id=?",(device_name,device_name,stamp,1 if pulled else 0,stamp,r['id']))
        return r['id']
    cur=conn.execute('INSERT INTO field_sync_clients(client_id,user_id,device_name,created_at,last_seen_at,last_pull_at) VALUES(?,?,?,?,?,?)',(client_id,user['id'],device_name,stamp,stamp,stamp if pulled else None));return cur.lastrowid

def _field_sync_log_row(r):
    d=dict(r)
    for key in ('payload_json','result_json','conflict_json'):
        raw=d.pop(key,'') or ''
        d[key.replace('_json','')]=json.loads(raw) if raw else {}
    return d

@app.get('/api/field/sync/bootstrap')
def field_sync_bootstrap(client_id:str=Query(min_length=8,max_length=128),device_name:str=Query(default='',max_length=120),user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        _field_sync_register_client(conn,user,client_id,device_name,True)
        items=rows(conn.execute(WO_SELECT+' WHERE w.assigned_to=? ORDER BY w.target_finish,w.id DESC',(user['id'],)))
        today=date.today().isoformat();rich=[]
        for w in items:
            w['sync_hash']=_field_sync_entity_state(conn,user,'work_order',w['id'])['hash']
            w['tasks']=rows(conn.execute('SELECT * FROM work_order_tasks WHERE work_order_id=? ORDER BY sequence_no',(w['id'],)))
            for t in w['tasks']:t['sync_hash']=_field_sync_entity_state(conn,user,'work_order_task',t['id'])['hash']
            if w.get('asset_id'):
                asset=_field_sync_entity_state(conn,user,'asset',w['asset_id']);w['asset_sync_hash']=asset['hash'];w['asset_state']=asset['state']
            else:w['asset_sync_hash']='';w['asset_state']=None
            dispatches=rows(conn.execute('SELECT * FROM dispatch_assignments WHERE work_order_id=? AND technician_user_id=? ORDER BY id DESC',(w['id'],user['id'])))
            for d in dispatches:d['sync_hash']=_field_sync_entity_state(conn,user,'dispatch',d['id'])['hash']
            w['dispatches']=dispatches;rich.append(w)
        my_work={'assigned':[x for x in rich if x['status'] in ('Assigned','Approved')],'today':[x for x in rich if x.get('target_start')==today or x.get('target_finish')==today],'in_progress':[x for x in rich if x['status']=='In Progress'],'overdue':[x for x in rich if x.get('target_finish') and x['target_finish']<today and x['status'] not in ('Completed','Closed','Cancelled')],'completed':[x for x in rich if x['status'] in ('Completed','Closed')][:20]}
        conflicts=rows(conn.execute("SELECT * FROM field_sync_operations WHERE user_id=? AND client_id=? AND status='Conflict' ORDER BY id DESC LIMIT 100",(user['id'],client_id)))
        return {'server_time':now(),'client_id':client_id,'schema_version':SCHEMA_VERSION,'work_orders':rich,'my_work':my_work,'conflicts':[_field_sync_log_row(x) for x in conflicts]}

@app.post('/api/field/sync/push')
def field_sync_push(body:FieldSyncPushIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        _field_sync_register_client(conn,user,body.client_id,body.device_name,False);results=[];batch_initial={};batch_mutated=set()
        for op in body.operations:
            existing=conn.execute('SELECT * FROM field_sync_operations WHERE operation_id=?',(op.operation_id,)).fetchone()
            if existing:
                if int(existing['user_id'])!=int(user['id']) or existing['client_id']!=body.client_id:raise HTTPException(409,'Field sync operation ID collision')
                item=_field_sync_log_row(existing);item['idempotent_replay']=True;results.append(item);continue
            payload=op.payload or {};submitted=now();payload_json=json.dumps(payload,sort_keys=True,ensure_ascii=False,default=str)
            cur=conn.execute('''INSERT INTO field_sync_operations(operation_id,client_id,user_id,entity_type,entity_id,operation_type,base_hash,payload_json,status,client_created_at,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(op.operation_id,body.client_id,user['id'],op.entity_type.lower(),op.entity_id,op.operation_type.lower(),op.base_hash,payload_json,'Pending',op.client_created_at,submitted));row_id=cur.lastrowid
            try:current=_field_sync_entity_state(conn,user,op.entity_type,op.entity_id)
            except HTTPException as exc:
                result={'detail':str(exc.detail),'status_code':exc.status_code};conn.execute("UPDATE field_sync_operations SET status='Rejected',result_json=? WHERE id=?",(json.dumps(result),row_id));results.append({'operation_id':op.operation_id,'status':'Rejected','result':result});continue
            key=(op.entity_type.lower(),int(op.entity_id));batch_initial.setdefault(key,current['hash']);is_append=op.entity_type.lower()=='work_order' and op.operation_type.lower()=='append_note';rebased=False
            if not is_append and not op.base_hash:
                result={'detail':'base_hash is required for conflict-safe mutable field operations'};conn.execute("UPDATE field_sync_operations SET status='Rejected',result_json=? WHERE id=?",(json.dumps(result),row_id));results.append({'operation_id':op.operation_id,'status':'Rejected','result':result});continue
            if not is_append and op.base_hash!=current['hash']:
                if key in batch_mutated and op.base_hash==batch_initial[key]:rebased=True
                else:
                    conflict={'reason':'Server state changed since the field snapshot','server_hash':current['hash'],'server_state':current['state'],'base_hash':op.base_hash};conn.execute("UPDATE field_sync_operations SET status='Conflict',conflict_json=? WHERE id=?",(json.dumps(conflict,sort_keys=True,default=str),row_id));results.append({'operation_id':op.operation_id,'status':'Conflict','conflict':conflict});continue
            conn.execute('SAVEPOINT field_sync_apply')
            try:
                applied=_field_sync_apply(conn,user,op.entity_type,op.entity_id,op.operation_type,payload);fresh=_field_sync_entity_state(conn,user,op.entity_type,op.entity_id);applied.update({'server_hash':fresh['hash'],'server_state':fresh['state'],'rebased_in_batch':rebased});conn.execute('RELEASE SAVEPOINT field_sync_apply')
                conn.execute("UPDATE field_sync_operations SET status='Applied',result_json=?,applied_at=? WHERE id=?",(json.dumps(applied,sort_keys=True,default=str),now(),row_id));batch_mutated.add(key);results.append({'operation_id':op.operation_id,'status':'Applied','result':applied})
            except HTTPException as exc:
                conn.execute('ROLLBACK TO SAVEPOINT field_sync_apply');conn.execute('RELEASE SAVEPOINT field_sync_apply');result={'detail':str(exc.detail),'status_code':exc.status_code};conn.execute("UPDATE field_sync_operations SET status='Rejected',result_json=? WHERE id=?",(json.dumps(result),row_id));results.append({'operation_id':op.operation_id,'status':'Rejected','result':result})
        counts={k:sum(1 for x in results if x.get('status')==k) for k in ('Applied','Conflict','Rejected')};audit(conn,user['id'],'FIELD SYNC PUSH','Field Sync',body.client_id,'',{'operations':len(body.operations),**counts})
        return {'client_id':body.client_id,'server_time':now(),'counts':counts,'results':results}

@app.get('/api/field/sync/operations')
def field_sync_operations(client_id:str=Query(min_length=8,max_length=128),status:str='',limit:int=Query(default=100,ge=1,le=500),user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        _field_sync_register_client(conn,user,client_id,'',False);sql='SELECT * FROM field_sync_operations WHERE user_id=? AND client_id=?';args=[user['id'],client_id]
        if status:sql+=' AND status=?';args.append(status)
        sql+=' ORDER BY id DESC LIMIT ?';args.append(limit);return [_field_sync_log_row(x) for x in rows(conn.execute(sql,args))]

@app.post('/api/field/sync/conflicts/{operation_id}/resolve')
def field_sync_resolve(operation_id:str,body:FieldSyncResolveIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        op=get_or_404(conn,'SELECT * FROM field_sync_operations WHERE operation_id=? AND user_id=?',(operation_id,user['id']),'Field sync operation not found')
        if op['status']!='Conflict':raise HTTPException(409,f"Operation is {op['status']}, not Conflict")
        if body.resolution=='discard':
            stamp=now();result={'resolution':'discard','detail':'Server state retained'};conn.execute("UPDATE field_sync_operations SET status='Discarded',result_json=?,resolved_at=? WHERE id=?",(json.dumps(result),stamp,op['id']));audit(conn,user['id'],'FIELD SYNC DISCARD','Field Sync',operation_id,'Conflict','Discarded');return {'operation_id':operation_id,'status':'Discarded','result':result}
        current=_field_sync_entity_state(conn,user,op['entity_type'],op['entity_id'])
        if not body.expected_server_hash or body.expected_server_hash!=current['hash']:raise HTTPException(409,{'message':'Server state changed again; refresh conflict before retry','server_hash':current['hash'],'server_state':current['state']})
        payload=json.loads(op['payload_json'] or '{}');conn.execute('SAVEPOINT field_sync_retry')
        try:
            applied=_field_sync_apply(conn,user,op['entity_type'],op['entity_id'],op['operation_type'],payload);fresh=_field_sync_entity_state(conn,user,op['entity_type'],op['entity_id']);applied.update({'resolution':'retry','server_hash':fresh['hash'],'server_state':fresh['state']});conn.execute('RELEASE SAVEPOINT field_sync_retry')
        except HTTPException:
            conn.execute('ROLLBACK TO SAVEPOINT field_sync_retry');conn.execute('RELEASE SAVEPOINT field_sync_retry');raise
        stamp=now();conn.execute("UPDATE field_sync_operations SET status='Applied',base_hash=?,result_json=?,applied_at=?,resolved_at=? WHERE id=?",(current['hash'],json.dumps(applied,sort_keys=True,default=str),stamp,stamp,op['id']));audit(conn,user['id'],'FIELD SYNC RETRY','Field Sync',operation_id,'Conflict','Applied');return {'operation_id':operation_id,'status':'Applied','result':applied}

# ---------- technician dispatch ----------
@app.get('/api/dispatch')
def list_dispatch(status:str='',technician_user_id:Optional[int]=None,user=Depends(current_user)):
    sql="""SELECT d.*,w.wo_no,w.title,w.priority,w.status work_status,a.asset_no,l.name location_name,s.name site_name,u.full_name technician_name,u.username technician_username,byu.full_name dispatched_by_name
      FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id LEFT JOIN assets a ON a.id=w.asset_id LEFT JOIN locations l ON l.id=w.location_id LEFT JOIN sites s ON s.id=l.site_id
      JOIN users u ON u.id=d.technician_user_id JOIN users byu ON byu.id=d.dispatched_by WHERE 1=1""";args=[]
    if status:sql+=' AND d.status=?';args.append(status)
    if technician_user_id:sql+=' AND d.technician_user_id=?';args.append(technician_user_id)
    if user['role']=='technician':sql+=' AND d.technician_user_id=?';args.append(user['id'])
    sql+=' ORDER BY CASE d.status WHEN \'On Site\' THEN 0 WHEN \'En Route\' THEN 1 WHEN \'Accepted\' THEN 2 WHEN \'Dispatched\' THEN 3 ELSE 4 END,d.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/dispatch/board')
def dispatch_board(site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        techsql="""SELECT tp.user_id,u.username,u.full_name,c.craft_code,c.name craft_name,s.id site_id,s.site_code,s.name site_name,tp.efficiency_pct
          FROM technician_profiles tp JOIN users u ON u.id=tp.user_id LEFT JOIN crafts c ON c.id=tp.craft_id LEFT JOIN sites s ON s.id=tp.home_site_id WHERE tp.active=1 AND u.active=1""";args=[]
        if site_id:techsql+=' AND tp.home_site_id=?';args.append(site_id)
        techs=rows(conn.execute(techsql,args));active_states=('Dispatched','Accepted','En Route','On Site')
        for t in techs:
            d=conn.execute("""SELECT d.*,w.wo_no,w.title,w.priority FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.technician_user_id=? AND d.status IN ('Dispatched','Accepted','En Route','On Site') ORDER BY d.id DESC LIMIT 1""",(t['user_id'],)).fetchone()
            t['dispatch']=dict(d) if d else None;t['availability']='Busy' if d else 'Available'
        open_work=rows(conn.execute(WO_SELECT+" WHERE w.status IN ('Approved','Assigned')"+(" AND s.id=?" if site_id else '')+" ORDER BY CASE w.priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 ELSE 1 END DESC",([site_id] if site_id else [])))
        return {'technicians':techs,'work_queue':open_work}

@app.post('/api/work-orders/{wo_id}/dispatch')
def dispatch_work(wo_id:int,body:DispatchIn,user=Depends(require_permission('work.write','admin','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        if w['status'] not in ('Approved','Assigned'):raise HTTPException(409,'Work order must be Approved or Assigned before dispatch')
        tech=get_or_404(conn,"SELECT u.id,u.full_name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.active=1 AND r.code='technician'",(body.technician_user_id,),'Active technician not found')
        busy=conn.execute("SELECT d.dispatch_no,w.wo_no FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.technician_user_id=? AND d.work_order_id<>? AND d.status IN ('Dispatched','Accepted','En Route','On Site') ORDER BY d.id DESC LIMIT 1",(body.technician_user_id,wo_id)).fetchone()
        if busy:raise HTTPException(409,f"Technician already has active dispatch {busy['dispatch_no']} for {busy['wo_no']}")
        conn.execute("UPDATE dispatch_assignments SET status='Cancelled',cancelled_at=? WHERE work_order_id=? AND status IN ('Dispatched','Accepted','En Route','On Site')",(now(),wo_id))
        no=next_no(conn,'dispatch_assignments','dispatch_no','DSP-',40001);cur=conn.execute("INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,dispatched_by,status,eta_minutes,notes,dispatched_at) VALUES(?,?,?,?, 'Dispatched',?,?,?)",(no,wo_id,body.technician_user_id,user['id'],body.eta_minutes,body.notes,now()))
        old=w['status'];conn.execute("UPDATE work_orders SET assigned_to=?,status='Assigned',updated_at=? WHERE id=?",(body.technician_user_id,now(),wo_id))
        workflow_event(conn,'Work Management','work_order',wo_id,w['wo_no'],'DISPATCH',old,'Assigned',user['id'],f'{no} → {tech["full_name"]}');audit(conn,user['id'],'DISPATCH','Field Service',no,'',{'work_order':w['wo_no'],'technician':tech['full_name'],'eta_minutes':body.eta_minutes})
        notify(conn,'Dispatch assigned',f'{no} — {w["wo_no"]}: {w["title"]}','High' if w['priority'] in ('Emergency','Critical','High') else 'Info',body.technician_user_id,None,'dispatch',no)
        return {'id':cur.lastrowid,'dispatch_no':no,'status':'Dispatched'}

@app.post('/api/dispatch/{dispatch_id}/transition')
def transition_dispatch(dispatch_id:int,body:DispatchTransitionIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    action=body.action.lower().replace(' ','');mapping={'accept':('Dispatched','Accepted','accepted_at'),'enroute':('Accepted','En Route','enroute_at'),'arrive':('En Route','On Site','arrived_at'),'complete':('On Site','Completed','completed_at')}
    with db() as conn:
        d=get_or_404(conn,'SELECT d.*,w.wo_no,w.status work_status FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.id=?',(dispatch_id,),'Dispatch not found')
        elevated=user['role'] in ('admin','maintenance_manager','planner','supervisor')
        if user['role']=='technician' and d['technician_user_id']!=user['id']:raise HTTPException(403,'Technicians can only update their own dispatch')
        if action=='cancel':
            if not elevated:raise HTTPException(403,'Only planners/supervisors can cancel dispatch')
            if d['status'] in ('Completed','Cancelled'):raise HTTPException(409,f"Dispatch is {d['status']}")
            target='Cancelled';field='cancelled_at'
        else:
            if action not in mapping:raise HTTPException(400,'Action must be accept, enroute, arrive, complete or cancel')
            expected,target,field=mapping[action]
            if d['status']!=expected:raise HTTPException(409,f"Action {action} requires {expected}, current status is {d['status']}")
        stamp=now();conn.execute(f'UPDATE dispatch_assignments SET status=?,{field}=?,notes=CASE WHEN ?<>\'\' THEN ? ELSE notes END WHERE id=?',(target,stamp,body.notes,body.notes,dispatch_id))
        if action=='arrive' and d['work_status']=='Assigned':
            conn.execute("UPDATE work_orders SET status='In Progress',actual_start=COALESCE(actual_start,?),updated_at=? WHERE id=?",(stamp,stamp,d['work_order_id']));_mark_sla_response(conn,d['work_order_id'],stamp)
            workflow_event(conn,'Work Management','work_order',d['work_order_id'],d['wo_no'],'ARRIVE','Assigned','In Progress',user['id'],body.notes)
        audit(conn,user['id'],'DISPATCH '+action.upper(),'Field Service',d['dispatch_no'],d['status'],target);return {'ok':True,'status':target}

# ---------- field service ----------
@app.get('/api/field/my-work')
def my_work(user=Depends(current_user)):
    with db() as conn:
        sql=WO_SELECT+' WHERE w.assigned_to=? ORDER BY w.target_finish,w.id DESC';items=rows(conn.execute(sql,(user['id'],)))
        today=date.today().isoformat()
        return {'assigned':[x for x in items if x['status'] in ('Assigned','Approved')],'today':[x for x in items if x.get('target_start')==today or x.get('target_finish')==today],'in_progress':[x for x in items if x['status']=='In Progress'],'overdue':[x for x in items if x.get('target_finish') and x['target_finish']<today and x['status'] not in ('Completed','Closed','Cancelled')],'completed':[x for x in items if x['status'] in ('Completed','Closed')][:20]}

# ---------- inspections ----------
@app.get('/api/inspections')
def list_inspections(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT i.*,a.asset_no,a.name asset_name,u.full_name inspector_name,w.wo_no corrective_wo_no FROM inspections i LEFT JOIN assets a ON a.id=i.asset_id LEFT JOIN users u ON u.id=i.inspector_id LEFT JOIN work_orders w ON w.id=i.corrective_wo_id ORDER BY i.id DESC'''))
@app.get('/api/inspections/{inspection_id}')
def get_inspection(inspection_id:int,user=Depends(current_user)):
    with db() as conn:
        i=get_or_404(conn,'SELECT * FROM inspections WHERE id=?',(inspection_id,),'Inspection not found');i['items']=rows(conn.execute('SELECT * FROM inspection_items WHERE inspection_id=? ORDER BY id',(inspection_id,)));return i
@app.post('/api/inspections')
def create_inspection(body:InspectionIn,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    items=body.items or ['Visual Condition','Leaks','Temperature','Noise','Grounding','Physical Damage']
    with db() as conn:
        no=next_no(conn,'inspections','inspection_no','INS-',5001);cur=conn.execute('INSERT INTO inspections(inspection_no,template_name,asset_id,work_order_id,inspector_id,status,created_at) VALUES(?,?,?,?,?,\'Draft\',?)',(no,body.template_name,body.asset_id,body.work_order_id,user['id'],now()))
        for item in items:
            conn.execute('INSERT INTO inspection_items(inspection_id,item_name) VALUES(?,?)',(cur.lastrowid,item))
        audit(conn,user['id'],'CREATE','Inspections',no,'',body.model_dump())
        return {'id':cur.lastrowid,'inspection_no':no}
@app.post('/api/inspections/{inspection_id}/submit')
def submit_inspection(inspection_id:int,body:InspectionSubmit,user=Depends(require_permission('work.transition',*WORK_ROLES))):
    with db() as conn:
        ins=get_or_404(conn,'SELECT * FROM inspections WHERE id=?',(inspection_id,),'Inspection not found');failed=False
        for r in body.responses:
            resp=r.get('response','N/A'); failed=failed or resp=='Fail';conn.execute('UPDATE inspection_items SET response=?,reading=?,remarks=? WHERE id=? AND inspection_id=?',(resp,r.get('reading',''),r.get('remarks',''),r.get('id'),inspection_id))
        corrective=None;result='Fail' if failed else 'Pass'
        if failed and body.create_corrective_on_fail:
            asset=conn.execute('SELECT * FROM assets WHERE id=?',(ins['asset_id'],)).fetchone() if ins['asset_id'] else None;no=next_no(conn,'work_orders','wo_no','WO-',10026);cur=conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,requested_by,target_start,target_finish,instructions,created_at,updated_at) VALUES(?,?,?,?,?,'High','Submitted','Corrective Maintenance',?,?,?,?,?,?)''',(no,f"Corrective action from {ins['inspection_no']}",f"Inspection {ins['inspection_no']} failed. Review failed items and correct defects.",ins['asset_id'],asset['location_id'] if asset else None,user['id'],date.today().isoformat(),(date.today()+timedelta(days=2)).isoformat(),'Review failed inspection items and implement corrective actions.',now(),now()));corrective=cur.lastrowid;conn.execute('UPDATE inspections SET corrective_wo_id=? WHERE id=?',(corrective,inspection_id));notify(conn,'Inspection failed',f'{ins["inspection_no"]} failed and generated {no}','High',None,'planner','inspections',ins['inspection_no'])
        conn.execute("UPDATE inspections SET status='Completed',result=?,inspected_at=?,remarks=? WHERE id=?",(result,now(),body.remarks,inspection_id));audit(conn,user['id'],'SUBMIT','Inspections',ins['inspection_no'],'Draft',result);return {'ok':True,'result':result,'corrective_work_order_id':corrective}

# ---------- HSE ----------
@app.get('/api/hse')
def list_hse(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT h.*,s.name site_name,l.name location_name,a.asset_no,u.full_name reported_by_name FROM safety_incidents h LEFT JOIN sites s ON s.id=h.site_id LEFT JOIN locations l ON l.id=h.location_id LEFT JOIN assets a ON a.id=h.asset_id LEFT JOIN users u ON u.id=h.reported_by ORDER BY h.id DESC'''))
@app.post('/api/hse')
def create_hse(body:HSEIn,user=Depends(require_permission('hse.write',*HSE_ROLES))):
    with db() as conn:
        no=next_no(conn,'safety_incidents','incident_no','HSE-',7001);risk=body.severity*body.probability;cur=conn.execute('''INSERT INTO safety_incidents(incident_no,incident_type,title,site_id,location_id,asset_id,reported_by,severity,probability,risk_score,status,description,corrective_action,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'Open',?,?,?,?)''',(no,body.incident_type,body.title,body.site_id,body.location_id,body.asset_id,user['id'],body.severity,body.probability,risk,body.description,body.corrective_action,body.occurred_at or now(),now()));audit(conn,user['id'],'CREATE','HSE',no,'',body.model_dump());
        if risk>=12:notify(conn,'High HSE risk',f'{no} has risk score {risk}','Critical',None,'maintenance_manager','hse',no)
        return {'id':cur.lastrowid,'incident_no':no,'risk_score':risk}
@app.patch('/api/hse/{incident_id}')
def update_hse(incident_id:int,body:HSEPatch,user=Depends(require_permission('hse.write',*HSE_ROLES))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM safety_incidents WHERE id=?',(incident_id,),'HSE record not found')
        if not changes:return old
        if 'status' in changes and changes['status'] not in ('Open','Investigating','Action Required','Closed','Cancelled'):
            raise HTTPException(400,'Invalid HSE status')
        severity=int(changes.get('severity',old['severity'])); probability=int(changes.get('probability',old['probability']))
        if 'severity' in changes or 'probability' in changes: changes['risk_score']=severity*probability
        sets=','.join(f'{k}=?' for k in changes)
        conn.execute(f'UPDATE safety_incidents SET {sets} WHERE id=?',(*changes.values(),incident_id))
        audit(conn,user['id'],'UPDATE','HSE',old['incident_no'],old,changes)
        if changes.get('risk_score',old['risk_score'])>=12 and old['risk_score']<12:
            notify(conn,'High HSE risk',f"{old['incident_no']} escalated to risk score {changes['risk_score']}",'Critical',None,'maintenance_manager','hse',old['incident_no'])
        return one(conn.execute('SELECT * FROM safety_incidents WHERE id=?',(incident_id,)))

def _recalculate_project_progress(conn, project_id:int):
    r=conn.execute("SELECT AVG(CASE WHEN status='Completed' THEN 100 ELSE progress END) avg_progress FROM project_tasks WHERE project_id=? AND status<>'Cancelled'",(project_id,)).fetchone()
    progress=round(float(r['avg_progress'] or 0),1)
    conn.execute('UPDATE projects SET progress=? WHERE id=?',(progress,project_id))
    return progress

# ---------- projects ----------
@app.get('/api/projects')
def list_projects(user=Depends(current_user)):
    with db() as conn:
        ps=rows(conn.execute('''SELECT p.*,u.full_name manager_name,s.name site_name FROM projects p LEFT JOIN users u ON u.id=p.manager_id LEFT JOIN sites s ON s.id=p.site_id ORDER BY p.id DESC'''))
        for p in ps:p['tasks']=rows(conn.execute('SELECT t.*,u.full_name owner_name FROM project_tasks t LEFT JOIN users u ON u.id=t.owner_id WHERE project_id=? ORDER BY id',(p['id'],)))
        return ps
@app.post('/api/projects')
def create_project(body:ProjectIn,user=Depends(require_permission('projects.write',*PROJECT_ROLES))):
    with db() as conn:
        no=next_no(conn,'projects','project_no','PRJ-',3001);cur=conn.execute('INSERT INTO projects(project_no,name,manager_id,site_id,start_date,finish_date,budget,actual_cost,progress,status) VALUES(?,?,?,?,?,?,?,0,0,?)',(no,body.name,body.manager_id,body.site_id,body.start_date,body.finish_date,body.budget,body.status));audit(conn,user['id'],'CREATE','Projects',no,'',body.model_dump());return {'id':cur.lastrowid,'project_no':no}
@app.post('/api/projects/{project_id}/tasks')
def create_project_task(project_id:int,body:ProjectTaskIn,user=Depends(require_permission('projects.write',*PROJECT_ROLES))):
    with db() as conn:
        project=get_or_404(conn,'SELECT * FROM projects WHERE id=?',(project_id,),'Project not found')
        cur=conn.execute('INSERT INTO project_tasks(project_id,task_name,owner_id,due_date,status,progress) VALUES(?,?,?,?,?,?)',(project_id,body.task_name,body.owner_id,body.due_date,body.status,body.progress))
        _recalculate_project_progress(conn,project_id)
        audit(conn,user['id'],'ADD TASK','Projects',project['project_no'],'',body.model_dump())
        return {'id':cur.lastrowid}
@app.patch('/api/projects/{project_id}/tasks/{task_id}')
def update_project_task(project_id:int,task_id:int,body:ProjectTaskPatch,user=Depends(require_permission('projects.write',*PROJECT_ROLES))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    if 'status' in changes and changes['status'] not in ('Open','In Progress','Blocked','Completed','Cancelled'):
        raise HTTPException(400,'Invalid project task status')
    with db() as conn:
        project=get_or_404(conn,'SELECT * FROM projects WHERE id=?',(project_id,),'Project not found')
        old=get_or_404(conn,'SELECT * FROM project_tasks WHERE id=? AND project_id=?',(task_id,project_id),'Project task not found')
        if changes:
            if changes.get('status')=='Completed' and 'progress' not in changes: changes['progress']=100
            sets=','.join(f'{k}=?' for k in changes);conn.execute(f'UPDATE project_tasks SET {sets} WHERE id=?',(*changes.values(),task_id))
            _recalculate_project_progress(conn,project_id)
            audit(conn,user['id'],'UPDATE TASK','Projects',project['project_no'],old,changes)
        return one(conn.execute('SELECT t.*,u.full_name owner_name FROM project_tasks t LEFT JOIN users u ON u.id=t.owner_id WHERE t.id=?',(task_id,)))

# ---------- vendors / contracts ----------
@app.get('/api/vendors')
def vendors(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT * FROM vendors ORDER BY name'))
@app.post('/api/vendors')
def create_vendor(body:VendorIn,user=Depends(require_permission('procurement.write','admin','procurement','maintenance_manager'))):
    with db() as conn:
        code=body.vendor_code or next_no(conn,'vendors','vendor_code','VND-',100);cur=conn.execute('INSERT INTO vendors(vendor_code,name,category,contact_person,email,phone,status) VALUES(?,?,?,?,?,?,?)',(code,body.name,body.category,body.contact_person,body.email,body.phone,body.status));audit(conn,user['id'],'CREATE','Vendors',code,'',body.model_dump());return {'id':cur.lastrowid,'vendor_code':code}
@app.get('/api/contracts')
def contracts(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT c.*,v.name vendor_name FROM contracts c LEFT JOIN vendors v ON v.id=c.vendor_id ORDER BY c.id DESC'))
@app.post('/api/contracts')
def create_contract(body:ContractIn,user=Depends(require_permission('procurement.write','admin','procurement','maintenance_manager'))):
    with db() as conn:
        no=body.contract_no or next_no(conn,'contracts','contract_no','CTR-',4001);cur=conn.execute('INSERT INTO contracts(contract_no,title,vendor_id,start_date,end_date,value,status) VALUES(?,?,?,?,?,?,?)',(no,body.title,body.vendor_id,body.start_date,body.end_date,body.value,body.status));audit(conn,user['id'],'CREATE','Contracts',no,'',body.model_dump());return {'id':cur.lastrowid,'contract_no':no}

# ---------- documents ----------
@app.get('/api/documents')
def documents(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT d.*,a.asset_no,w.wo_no,l.location_code,p.project_no,v.vendor_code,u.full_name uploaded_by_name FROM documents d LEFT JOIN assets a ON a.id=d.asset_id LEFT JOIN work_orders w ON w.id=d.work_order_id LEFT JOIN locations l ON l.id=d.location_id LEFT JOIN projects p ON p.id=d.project_id LEFT JOIN vendors v ON v.id=d.vendor_id JOIN users u ON u.id=d.uploaded_by ORDER BY d.id DESC'''))
@app.post('/api/documents/upload')
def upload_document(title:str=Form(...),category:str=Form(...),asset_id:Optional[int]=Form(None),work_order_id:Optional[int]=Form(None),location_id:Optional[int]=Form(None),project_id:Optional[int]=Form(None),vendor_id:Optional[int]=Form(None),file:UploadFile=File(...),user=Depends(require_permission('documents.upload',*DOC_WRITE_ROLES))):
    original=Path(file.filename or 'document').name
    suffix=Path(original).suffix.lower()
    if suffix not in ALLOWED_DOC_SUFFIXES: raise HTTPException(400,'Unsupported document type')
    stored=f'{secrets.token_hex(12)}{suffix}';dest=UPLOAD_DIR/stored;size=0
    try:
        with dest.open('wb') as out:
            while chunk:=file.file.read(1024*1024):
                size+=len(chunk)
                if size>MAX_UPLOAD_BYTES: raise HTTPException(413,f'Document exceeds {MAX_UPLOAD_MB} MB limit')
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    with db() as conn:
        no=next_no(conn,'documents','document_no','DOC-',6001);cur=conn.execute('''INSERT INTO documents(document_no,title,category,file_name,stored_name,mime_type,asset_id,work_order_id,location_id,project_id,vendor_id,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,title,category,original,stored,file.content_type or '',asset_id,work_order_id,location_id,project_id,vendor_id,user['id'],now()));audit(conn,user['id'],'UPLOAD','Documents',no,'',original);return {'id':cur.lastrowid,'document_no':no,'size_bytes':size}
@app.get('/api/documents/{doc_id}/download')
def download_document(doc_id:int,user=Depends(current_user)):
    with db() as conn:d=get_or_404(conn,'SELECT * FROM documents WHERE id=?',(doc_id,),'Document not found')
    p=UPLOAD_DIR/d['stored_name']
    if not p.exists():raise HTTPException(404,'Stored file missing')
    return FileResponse(p,filename=d['file_name'],media_type=d['mime_type'] or 'application/octet-stream')

# ---------- service levels / integration events ----------
@app.get('/api/sla/summary')
def sla_summary(user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:
        _backfill_work_order_slas(conn)
        total=conn.execute('SELECT COUNT(*) FROM work_order_sla').fetchone()[0]
        response_met=conn.execute("SELECT COUNT(*) FROM work_order_sla WHERE response_status='Met'").fetchone()[0]
        response_breached=conn.execute("SELECT COUNT(*) FROM work_order_sla WHERE response_status='Breached'").fetchone()[0]
        resolution_met=conn.execute("SELECT COUNT(*) FROM work_order_sla WHERE resolution_status='Met'").fetchone()[0]
        resolution_breached=conn.execute("SELECT COUNT(*) FROM work_order_sla WHERE resolution_status='Breached'").fetchone()[0]
        active_breaches=conn.execute('''SELECT COUNT(*) FROM work_order_sla s JOIN work_orders w ON w.id=s.work_order_id
          WHERE w.status NOT IN ('Completed','Closed','Cancelled') AND (s.response_status='Breached' OR s.resolution_status='Breached')''').fetchone()[0]
        measured=response_met+response_breached+resolution_met+resolution_breached
        compliance=round((response_met+resolution_met)*100/max(measured,1),1) if measured else 100.0
        return {'total':total,'active_breaches':active_breaches,'response_met':response_met,'response_breached':response_breached,'resolution_met':resolution_met,'resolution_breached':resolution_breached,'compliance_percent':compliance}

@app.get('/api/sla/policies')
def sla_policies(user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:return rows(conn.execute("SELECT * FROM sla_policies ORDER BY CASE priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC"))

@app.patch('/api/sla/policies/{policy_id}')
def update_sla_policy(policy_id:int,body:SLAPolicyPatch,user=Depends(require_permission('automation.run','admin','maintenance_manager'))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM sla_policies WHERE id=?',(policy_id,),'SLA policy not found')
        response=int(changes.get('response_minutes',old['response_minutes']));resolution=int(changes.get('resolution_minutes',old['resolution_minutes']))
        if resolution<response:raise HTTPException(400,'Resolution target must be greater than or equal to response target')
        if 'active' in changes:changes['active']=1 if changes['active'] else 0
        if changes:
            conn.execute('UPDATE sla_policies SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),policy_id))
            for w in rows(conn.execute('SELECT id FROM work_orders WHERE priority=?',(old['priority'],))):_ensure_work_sla(conn,w['id'],force=True)
            audit(conn,user['id'],'UPDATE','Service Levels',old['policy_code'],old,changes);emit_event(conn,'sla.policy_updated','sla_policy',old['policy_code'],changes)
        return dict(conn.execute('SELECT * FROM sla_policies WHERE id=?',(policy_id,)).fetchone())

@app.get('/api/sla/work-orders')
def sla_work_orders(status:str='',user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:
        _backfill_work_order_slas(conn)
        sql='''SELECT w.id,w.wo_no,w.title,w.priority,w.status,w.assigned_to,s.response_due,s.resolution_due,s.response_status,s.resolution_status,s.escalated_level,p.policy_code,
          u.full_name assigned_to_name FROM work_orders w JOIN work_order_sla s ON s.work_order_id=w.id JOIN sla_policies p ON p.id=s.policy_id LEFT JOIN users u ON u.id=w.assigned_to WHERE 1=1''';args=[]
        if status:sql+=' AND (s.response_status=? OR s.resolution_status=?)';args += [status,status]
        sql+=" ORDER BY CASE WHEN s.response_status='Breached' OR s.resolution_status='Breached' THEN 0 ELSE 1 END,w.id DESC"
        return rows(conn.execute(sql,args))

@app.get('/api/sla/events')
def sla_event_list(limit:int=Query(100,ge=1,le=500),user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:return rows(conn.execute('SELECT e.*,w.wo_no,w.title FROM sla_events e JOIN work_orders w ON w.id=e.work_order_id ORDER BY e.id DESC LIMIT ?',(limit,)))

@app.get('/api/events/outbox')
def outbox_list(status:str='',limit:int=Query(100,ge=1,le=500),user=Depends(require_roles('admin','maintenance_manager','executive'))):
    sql='SELECT * FROM event_outbox WHERE 1=1';args=[]
    if status:sql+=' AND status=?';args.append(status)
    sql+=' ORDER BY id DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/events/outbox/{event_id}/retry')
def retry_outbox_event(event_id:int,user=Depends(require_permission('automation.run','admin','maintenance_manager'))):
    with db() as conn:
        event=rearm_outbox_event(conn,event_id)
        if not event:raise HTTPException(404,'Outbox event not found')
        audit(conn,user['id'],'RETRY','Integration Events',event['event_no'],event['status'],'Pending')
        return {'ok':True,'event_no':event['event_no']}

# ---------- automation / observability / reporting ----------
@app.get('/api/automation/status')
def automation_status(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:
        last=one(conn.execute('SELECT jr.*,u.full_name actor_name FROM job_runs jr LEFT JOIN users u ON u.id=jr.actor_id ORDER BY jr.id DESC LIMIT 1'))
        pending=conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='Pending'").fetchone()[0]
        due_pm=conn.execute("SELECT COUNT(*) FROM maintenance_plans WHERE active=1 AND trigger_type='Calendar' AND next_due IS NOT NULL AND next_due<=?",(date.today().isoformat(),)).fetchone()[0]
        low=conn.execute('SELECT COUNT(*) FROM inventory_items WHERE current_stock-reserved_stock<=reorder_point').fetchone()[0]
        overdue=conn.execute("SELECT COUNT(*) FROM work_orders WHERE target_finish IS NOT NULL AND target_finish<? AND status NOT IN ('Completed','Closed','Cancelled')",(date.today().isoformat(),)).fetchone()[0]
        sla_breaches=conn.execute("SELECT COUNT(*) FROM work_order_sla s JOIN work_orders w ON w.id=s.work_order_id WHERE w.status NOT IN ('Completed','Closed','Cancelled') AND (s.response_status='Breached' OR s.resolution_status='Breached')").fetchone()[0]
        outbox_pending=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed')").fetchone()[0]
        outbox_dead_lettered=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status='DeadLetter'").fetchone()[0]
        return {'version':APP_VERSION,'scheduler_enabled':AUTOMATION_INTERVAL_MINUTES>0,'interval_minutes':AUTOMATION_INTERVAL_MINUTES,'webhook_configured':bool(EVENT_WEBHOOK_URL),'last_run':last,'queue':{'due_pm':due_pm,'low_stock':low,'overdue_work':overdue,'pending_approvals':pending,'sla_breaches':sla_breaches,'outbox_pending':outbox_pending,'outbox_dead_lettered':outbox_dead_lettered}}

@app.get('/api/automation/runs')
def automation_runs(limit:int=Query(50,ge=1,le=200),user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('''SELECT jr.*,u.full_name actor_name FROM job_runs jr LEFT JOIN users u ON u.id=jr.actor_id ORDER BY jr.id DESC LIMIT ?''',(limit,)))

@app.post('/api/automation/run')
def automation_run(as_of:Optional[str]=None,user=Depends(require_permission('automation.run','admin','maintenance_manager'))):
    with db() as conn:
        result=_execute_automation(conn,user['id'],'manual',as_of)
        if result['status']=='Failed':raise HTTPException(500,result['error'])
        return result

@app.get('/api/metrics',response_class=PlainTextResponse)
def metrics(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    uptime=max(time.time()-_REQUEST_METRICS['started_at'],0.001);total=_REQUEST_METRICS['requests_total'];avg=_REQUEST_METRICS['latency_ms_total']/max(total,1)
    with db() as conn:
        active_sessions=conn.execute('SELECT COUNT(*) FROM sessions WHERE expires_at>?',(now(),)).fetchone()[0]
        jobs=conn.execute("SELECT COUNT(*) FROM job_runs WHERE status='Succeeded'").fetchone()[0]
        sla_breaches=conn.execute("SELECT COUNT(*) FROM work_order_sla WHERE response_status='Breached' OR resolution_status='Breached'").fetchone()[0]
        outbox_pending=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed')").fetchone()[0]
        outbox_dead_lettered=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status='DeadLetter'").fetchone()[0]
        hs=[_asset_health(conn,x['id'])['score'] for x in rows(conn.execute('SELECT id FROM assets'))];health_avg=sum(hs)/max(len(hs),1)
        plan=_maintenance_forecast(conn,90,None);peak=plan['summary']['peak_utilization_pct'];parts_short=plan['summary']['parts_shortage_jobs'];workforce=plan['technicians']
        open_outages=conn.execute("SELECT COUNT(*) FROM asset_outages WHERE status='Open'").fetchone()[0]
        active_dispatches=conn.execute("SELECT COUNT(*) FROM dispatch_assignments WHERE status IN ('Dispatched','Accepted','En Route','On Site')").fetchone()[0]
        reserved_units=conn.execute('SELECT COALESCE(SUM(reserved_stock),0) FROM inventory_items').fetchone()[0] or 0
        active_alarms=conn.execute("SELECT COUNT(*) FROM operational_alarms WHERE status IN ('Open','Acknowledged')").fetchone()[0]
        critical_alarms=conn.execute("SELECT COUNT(*) FROM operational_alarms WHERE status IN ('Open','Acknowledged') AND severity='Critical'").fetchone()[0]
        telemetry_channels=conn.execute("SELECT COUNT(*) FROM telemetry_channels WHERE active=1").fetchone()[0]
        open_incidents=conn.execute("SELECT COUNT(*) FROM alarm_incidents WHERE status IN ('Open','Acknowledged')").fetchone()[0]
        active_suppressions=conn.execute("SELECT COUNT(*) FROM alarm_suppressions WHERE active=1 AND start_at<=? AND end_at>=?",(now(),now())).fetchone()[0]
        active_shelves=conn.execute("SELECT COUNT(*) FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id WHERE sh.status='Approved' AND sh.start_at<=? AND sh.end_at>? AND oa.status IN ('Open','Acknowledged')",(now(),now())).fetchone()[0]
        active_topology_links=conn.execute("SELECT COUNT(*) FROM asset_topology_links WHERE active=1").fetchone()[0]
        topology_incidents=conn.execute("SELECT COUNT(*) FROM alarm_incidents WHERE status IN ('Open','Acknowledged') AND correlation_mode='Topology'").fetchone()[0]
        field_sync_pending=conn.execute("SELECT COUNT(*) FROM field_sync_operations WHERE status='Pending'").fetchone()[0]
        field_sync_conflicts=conn.execute("SELECT COUNT(*) FROM field_sync_operations WHERE status='Conflict'").fetchone()[0]
        field_sync_applied_24h=conn.execute("SELECT COUNT(*) FROM field_sync_operations WHERE status='Applied' AND applied_at>=?",((datetime.now()-timedelta(hours=24)).isoformat(timespec='seconds'),)).fetchone()[0]
        signed_approvals=conn.execute('SELECT COUNT(*) FROM approval_signature_evidence').fetchone()[0]
        signature_integrity=verify_approval_signature_chain(conn)
        retention_runs_total=conn.execute('SELECT COUNT(*) FROM retention_runs').fetchone()[0]
        retention_purged_total=conn.execute('SELECT COALESCE(SUM(purged_count),0) FROM retention_run_items').fetchone()[0] or 0
        active_retention_holds=conn.execute("SELECT COUNT(*) FROM retention_holds WHERE status='Active'").fetchone()[0]
        retention_integrity=verify_retention_run_chain(conn)
        permission_role_grants=conn.execute('SELECT COUNT(*) FROM role_permissions').fetchone()[0]
        active_permission_overrides=conn.execute("SELECT COUNT(*) FROM user_permission_overrides WHERE expires_at IS NULL OR expires_at='' OR expires_at>?",(now(),)).fetchone()[0]
        active_permission_denies=conn.execute("SELECT COUNT(*) FROM user_permission_overrides WHERE effect='Deny' AND (expires_at IS NULL OR expires_at='' OR expires_at>?)",(now(),)).fetchone()[0]
        active_cbm_rules=conn.execute('SELECT COUNT(*) FROM cbm_rules WHERE active=1').fetchone()[0]
        open_cbm_events=conn.execute("SELECT COUNT(*) FROM cbm_events WHERE status IN ('Open','Acknowledged')").fetchone()[0]
        cbm_work_orders=conn.execute('SELECT COUNT(*) FROM cbm_events WHERE work_order_id IS NOT NULL').fetchone()[0]
        active_fmea_records=conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status='Active'").fetchone()[0]
        critical_fmea_records=conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status<>'Retired' AND risk_band='Critical'").fetchone()[0]
        overdue_fmea_reviews=conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status<>'Retired' AND review_due_date IS NOT NULL AND review_due_date<?",(date.today().isoformat(),)).fetchone()[0]
        active_rcm_strategies=conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status='Active'").fetchone()[0]
        overdue_rcm_reviews=conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status='Active' AND review_due_date IS NOT NULL AND review_due_date<?",(date.today().isoformat(),)).fetchone()[0]
        rcm_eligible=conn.execute("SELECT COUNT(*) FROM asset_fmea WHERE status<>'Retired'").fetchone()[0]
        rcm_covered=conn.execute("SELECT COUNT(*) FROM rcm_strategies WHERE status IN ('Approved','Active')").fetchone()[0]
        critical_fmea_without_rcm=conn.execute("""SELECT COUNT(*) FROM asset_fmea f WHERE f.status<>'Retired' AND f.risk_band='Critical' AND NOT EXISTS(SELECT 1 FROM rcm_strategies r WHERE r.asset_fmea_id=f.id AND r.status IN ('Approved','Active'))""").fetchone()[0]
        rcm_coverage_pct=100*float(rcm_covered)/max(int(rcm_eligible),1)
        q24=_telemetry_quality_summary(conn,24,None);bad_quality_24h=q24['bad'];duplicates_24h=conn.execute("SELECT COALESCE(SUM(duplicate_count),0) FROM telemetry_ingest_batches WHERE started_at>=?",((datetime.now()-timedelta(hours=24)).isoformat(timespec='seconds'),)).fetchone()[0] or 0
    lines=['# HELP euas_requests_total Total HTTP requests','# TYPE euas_requests_total counter',f'euas_requests_total {total}',f'euas_request_errors_total {_REQUEST_METRICS["errors_total"]}',f'euas_request_latency_ms_avg {avg:.3f}',f'euas_uptime_seconds {uptime:.0f}',f'euas_active_sessions {active_sessions}',f'euas_automation_runs_succeeded {jobs}',f'euas_sla_breaches_total {sla_breaches}',f'euas_outbox_pending {outbox_pending}',f'euas_outbox_dead_lettered {outbox_dead_lettered}',f'euas_asset_health_score_avg {health_avg:.2f}',f'euas_maintenance_forecast_peak_utilization_pct {peak:.2f}',f'euas_workforce_technicians {workforce}',f'euas_parts_shortage_jobs_90d {parts_short}',f'euas_open_outages {open_outages}',f'euas_active_dispatches {active_dispatches}',f'euas_reserved_inventory_units {reserved_units}',f'euas_active_operational_alarms {active_alarms}',f'euas_critical_operational_alarms {critical_alarms}',f'euas_telemetry_channels {telemetry_channels}',f'euas_open_alarm_incidents {open_incidents}',f'euas_active_alarm_suppressions {active_suppressions}',f'euas_active_alarm_shelves {active_shelves}',f'euas_active_topology_links {active_topology_links}',f'euas_topology_correlated_incidents {topology_incidents}',f'euas_field_sync_pending {field_sync_pending}',f'euas_field_sync_conflicts {field_sync_conflicts}',f'euas_field_sync_applied_24h {field_sync_applied_24h}',f'euas_signed_approvals_total {signed_approvals}',f'euas_approval_signature_chain_valid {1 if signature_integrity['valid'] else 0}',f'euas_retention_runs_total {retention_runs_total}',f'euas_retention_purged_records_total {retention_purged_total}',f'euas_active_retention_holds {active_retention_holds}',f'euas_retention_run_chain_valid {1 if retention_integrity['valid'] else 0}',f'euas_permission_role_grants {permission_role_grants}',f'euas_active_permission_overrides {active_permission_overrides}',f'euas_active_permission_denies {active_permission_denies}',f'euas_active_cbm_rules {active_cbm_rules}',f'euas_open_cbm_events {open_cbm_events}',f'euas_cbm_work_orders_total {cbm_work_orders}',f'euas_active_fmea_records {active_fmea_records}',f'euas_critical_fmea_records {critical_fmea_records}',f'euas_overdue_fmea_reviews {overdue_fmea_reviews}',f'euas_active_rcm_strategies {active_rcm_strategies}',f'euas_overdue_rcm_reviews {overdue_rcm_reviews}',f'euas_rcm_strategy_coverage_pct {rcm_coverage_pct:.2f}',f'euas_critical_fmea_without_rcm {critical_fmea_without_rcm}',f'euas_bad_quality_readings_24h {bad_quality_24h}',f'euas_duplicate_telemetry_readings_24h {duplicates_24h}']
    for code,count in sorted(_REQUEST_METRICS['status'].items()):lines.append(f'euas_http_responses_total{{status="{code}"}} {count}')
    return '\n'.join(lines)+'\n'

@app.get('/api/exports/approval-signatures.csv')
def export_approval_signatures(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:
        data=rows(conn.execute('SELECT * FROM approval_signature_evidence ORDER BY id DESC'))
    return csv_response('EUAS_approval_signature_evidence.csv',['Evidence','Approval','Module','Record Type','Record ID','Record','Decision','Signer','Username','Role','Delegated','Credential Verified','Intent','Comments','Signed At','Previous Hash','Evidence Hash'],[[x['evidence_no'],x['approval_no'],x['module'],x['record_type'],x['record_id'],x['record_code'],x['decision'],x['signer_name'],x['signer_username'],x['signer_role'],x['delegated_authority'],x['credential_verified'],x['intent_statement'],x.get('comments') or '',x['signed_at'],x.get('prev_hash') or '',x['evidence_hash']] for x in data])

@app.get('/api/exports/failure-modes.csv')
def export_failure_modes(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT fm.*,p.mode_no parent_mode_no,p.name parent_name FROM failure_modes fm LEFT JOIN failure_modes p ON p.id=fm.parent_id ORDER BY fm.id"""))
    return csv_response('EUAS_failure_modes.csv',['Mode','Name','Category','Parent','Parent Name','Active','Description'],[[x['mode_no'],x['name'],x['category'],x.get('parent_mode_no') or '',x.get('parent_name') or '',x['active'],x.get('description') or ''] for x in data])

@app.get('/api/exports/fmea.csv')
def export_fmea(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT f.*,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,u.full_name owner_name FROM asset_fmea f JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id LEFT JOIN users u ON u.id=f.owner_id ORDER BY f.rpn DESC,f.id DESC"""))
    return csv_response('EUAS_asset_fmea.csv',['FMEA','Asset','Asset Name','Failure Mode','Failure Mode Name','Effect','Cause','Severity','Occurrence','Detectability','RPN','Risk Band','Status','Owner','Review Due','Recommended Action'],[[x['fmea_no'],x['asset_no'],x['asset_name'],x['mode_no'],x['failure_mode_name'],x['failure_effect'],x['failure_cause'],x['severity'],x['occurrence'],x['detectability'],x['rpn'],x['risk_band'],x['status'],x.get('owner_name') or '',x.get('review_due_date') or '',x.get('recommended_action') or ''] for x in data])

@app.get('/api/exports/rcm-strategies.csv')
def export_rcm_strategies(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT r.*,f.fmea_no,f.rpn,f.risk_band,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,u.full_name owner_name,cb.rule_no linked_cbm_rule_no,pm.pm_no linked_pm_no
      FROM rcm_strategies r JOIN asset_fmea f ON f.id=r.asset_fmea_id JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id LEFT JOIN users u ON u.id=r.owner_id LEFT JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id LEFT JOIN maintenance_plans pm ON pm.id=r.linked_pm_plan_id ORDER BY r.id DESC"""))
    return csv_response('EUAS_rcm_strategies.csv',['Strategy','Asset','Asset Name','FMEA','Failure Mode','Failure Mode Name','RPN','Risk','Consequence','Strategy Type','Task','Interval Days','CBM Rule','PM Plan','Status','Owner','Review Due','Justification'],[[x['strategy_no'],x['asset_no'],x['asset_name'],x['fmea_no'],x['mode_no'],x['failure_mode_name'],x['rpn'],x['risk_band'],x['consequence_classification'],x['strategy_type'],x['task_description'],x.get('interval_days') or '',x.get('linked_cbm_rule_no') or '',x.get('linked_pm_no') or '',x['status'],x.get('owner_name') or '',x.get('review_due_date') or '',x['justification']] for x in data])

@app.get('/api/exports/work-orders.csv')
def export_work_orders(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute(WO_SELECT+' ORDER BY w.id DESC'))
    return csv_response('EUAS_work_orders.csv',['Work Order','Title','Asset','Priority','Status','Type','Assigned To','Target Finish','Actual Hours','Actual Cost'],[[x['wo_no'],x['title'],x.get('asset_no') or '',x['priority'],x['status'],x['work_type'],x.get('assigned_to_name') or '',x.get('target_finish') or '',x['actual_hours'],x['actual_cost']] for x in data])

@app.get('/api/exports/inventory.csv')
def export_inventory(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute('''SELECT i.*,w.name warehouse_name,v.name vendor_name FROM inventory_items i JOIN warehouses w ON w.id=i.warehouse_id LEFT JOIN vendors v ON v.id=i.vendor_id ORDER BY i.item_no'''))
    return csv_response('EUAS_inventory.csv',['Item','Name','Category','Warehouse','Current','Reserved','Available','Reorder Point','Unit Price','Vendor'],[[x['item_no'],x['name'],x['category'],x['warehouse_name'],x['current_stock'],x['reserved_stock'],x['current_stock']-x['reserved_stock'],x['reorder_point'],x['unit_price'],x.get('vendor_name') or ''] for x in data])

@app.get('/api/exports/procurement.csv')
def export_procurement(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute('''SELECT pr.pr_no,pr.title,pr.status,pr.total_estimate,pr.created_at,u.full_name requester,s.name site_name FROM purchase_requisitions pr LEFT JOIN users u ON u.id=pr.requester_id LEFT JOIN sites s ON s.id=pr.site_id ORDER BY pr.id DESC'''))
    return csv_response('EUAS_procurement.csv',['PR','Title','Requester','Site','Status','Estimate','Created'],[[x['pr_no'],x['title'],x.get('requester') or '',x.get('site_name') or '',x['status'],x['total_estimate'],x['created_at']] for x in data])

@app.get('/api/exports/audit.csv')
def export_audit(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:data=rows(conn.execute('''SELECT a.*,u.full_name FROM audit_logs a JOIN users u ON u.id=a.user_id ORDER BY a.id DESC'''))
    return csv_response('EUAS_audit.csv',['Time','User','Action','Module','Record','Old Value','New Value','Previous Hash','Audit Hash'],[[x['created_at'],x['full_name'],x['action'],x['module'],x['record_id'],x['old_value'],x['new_value'],x.get('prev_hash') or '',x.get('audit_hash') or ''] for x in data])

@app.get('/api/exports/sla.csv')
def export_sla(user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:
        _backfill_work_order_slas(conn)
        data=rows(conn.execute('SELECT w.wo_no,w.title,w.priority,w.status,s.response_due,s.response_status,s.resolution_due,s.resolution_status,s.escalated_level,p.policy_code FROM work_orders w JOIN work_order_sla s ON s.work_order_id=w.id JOIN sla_policies p ON p.id=s.policy_id ORDER BY w.id DESC'))
    return csv_response('EUAS_sla.csv',['Work Order','Title','Priority','Status','Response Due','Response Status','Resolution Due','Resolution Status','Escalation Level','Policy'],[[x['wo_no'],x['title'],x['priority'],x['status'],x['response_due'],x['response_status'],x['resolution_due'],x['resolution_status'],x['escalated_level'],x['policy_code']] for x in data])

@app.get('/api/exports/cost-ledger.csv')
def export_cost_ledger(user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:data=rows(conn.execute('''SELECT c.*,w.wo_no,a.asset_no,u.full_name posted_by_name FROM maintenance_cost_ledger c LEFT JOIN work_orders w ON w.id=c.work_order_id LEFT JOIN assets a ON a.id=c.asset_id LEFT JOIN users u ON u.id=c.posted_by ORDER BY c.id DESC'''))
    return csv_response('EUAS_maintenance_cost_ledger.csv',['Entry','Posted','Asset','Work Order','Cost Type','Amount','Quantity','Reference','Posted By'],[[x['entry_no'],x['posted_at'],x.get('asset_no') or '',x.get('wo_no') or '',x['cost_type'],x['amount'],x['quantity'],x['reference'],x.get('posted_by_name') or ''] for x in data])

@app.get('/api/exports/asset-health.csv')
def export_asset_health(user=Depends(current_user)):
    with db() as conn:data=[_asset_health(conn,x['id']) for x in rows(conn.execute('SELECT id FROM assets ORDER BY asset_no'))]
    return csv_response('EUAS_asset_health.csv',['Asset','Name','Site','Location','Score','Risk Band','Condition','Criticality','Priority Work','Overdue Work','Failed Inspections','SLA Breaches'],[[x['asset_no'],x['name'],x.get('site_name') or '',x.get('location_name') or '',x['score'],x['risk_band'],x['condition'],x['criticality'],x['open_priority_work'],x['overdue_work'],x['failed_inspections'],x['sla_breaches']] for x in data])

@app.get('/api/exports/maintenance-forecast.csv')
def export_maintenance_forecast(horizon_days:int=Query(90,ge=7,le=365),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:data=_maintenance_forecast(conn,horizon_days,site_id)
    return csv_response('EUAS_maintenance_forecast.csv',['Week Start','PM Jobs','Backlog Jobs','Demand Hours','Capacity Hours','Utilization %','State'],[[x['week_start'],x['pm_jobs'],x['backlog_jobs'],x['demand_hours'],x['capacity_hours'],x['utilization_pct'],x['capacity_state']] for x in data['weeks']])

@app.get('/api/exports/workforce-capacity.csv')
def export_workforce_capacity(weeks:int=Query(12,ge=1,le=52),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:
        start=_forecast_bucket_start(date.today());data=[]
        for i in range(weeks):
            ws=start+timedelta(days=7*i);cap=_workforce_week_capacity(conn,ws,site_id)
            data.append([ws.isoformat(),cap['technicians'],cap['capacity_hours'],cap['source'],json.dumps(cap['craft_capacity'],sort_keys=True)])
    return csv_response('EUAS_workforce_capacity.csv',['Week Start','Technicians','Capacity Hours','Source','Craft Capacity'],data)

@app.get('/api/exports/reliability.csv')
def export_reliability(period_days:int=Query(365,ge=30,le=3650),site_id:Optional[int]=None,user=Depends(current_user)):
    with db() as conn:data=_asset_reliability_rows(conn,period_days,site_id)
    return csv_response('EUAS_asset_reliability.csv',['Asset','Name','Site','Period Days','Failures','Downtime Hours','MTBF Hours','MTTR Hours','Availability %','Maintenance Cost'],[[x['asset_no'],x['name'],x.get('site_name') or '',x['period_days'],x['failures'],x['downtime_hours'],x['mtbf_hours'] if x['mtbf_hours'] is not None else '',x['mttr_hours'],x['availability_pct'],x['maintenance_cost']] for x in data])

@app.get('/api/exports/outages.csv')
def export_outages(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute('''SELECT o.*,a.asset_no,a.name asset_name,s.name site_name,w.wo_no,u.full_name reported_by_name FROM asset_outages o JOIN assets a ON a.id=o.asset_id LEFT JOIN sites s ON s.id=o.site_id LEFT JOIN work_orders w ON w.id=o.work_order_id JOIN users u ON u.id=o.reported_by ORDER BY o.id DESC'''))
    return csv_response('EUAS_asset_outages.csv',['Outage','Asset','Asset Name','Site','Work Order','Type','Status','Cause','Impact','Lost Capacity','Unit','Start','End','Reported By'],[[x['outage_no'],x['asset_no'],x['asset_name'],x.get('site_name') or '',x.get('wo_no') or '',x['outage_type'],x['status'],x.get('cause_code') or '',x.get('impact') or '',x['lost_capacity'],x.get('capacity_unit') or '',x['start_at'],x.get('end_at') or '',x['reported_by_name']] for x in data])

@app.get('/api/exports/field-sync.csv')
def export_field_sync(user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','executive'))):
    with db() as conn:
        data=rows(conn.execute('''SELECT o.*,u.full_name user_name FROM field_sync_operations o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC'''))
    return csv_response('EUAS_field_sync_operations.csv',['Operation','Client','User','Entity Type','Entity ID','Operation Type','Status','Base Hash','Client Created','Submitted','Applied','Resolved','Payload','Result','Conflict'],[[x['operation_id'],x['client_id'],x['user_name'],x['entity_type'],x['entity_id'],x['operation_type'],x['status'],x.get('base_hash') or '',x.get('client_created_at') or '',x['submitted_at'],x.get('applied_at') or '',x.get('resolved_at') or '',x['payload_json'],x.get('result_json') or '',x.get('conflict_json') or ''] for x in data])

@app.get('/api/exports/dispatch.csv')
def export_dispatch(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute('''SELECT d.*,w.wo_no,w.title,u.full_name technician_name,du.full_name dispatcher_name FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id JOIN users u ON u.id=d.technician_user_id JOIN users du ON du.id=d.dispatched_by ORDER BY d.id DESC'''))
    return csv_response('EUAS_dispatch.csv',['Dispatch','Work Order','Title','Technician','Status','ETA Minutes','Dispatched','Accepted','En Route','Arrived','Completed','Dispatcher'],[[x['dispatch_no'],x['wo_no'],x['title'],x['technician_name'],x['status'],x.get('eta_minutes') or '',x['dispatched_at'],x.get('accepted_at') or '',x.get('enroute_at') or '',x.get('arrived_at') or '',x.get('completed_at') or '',x['dispatcher_name']] for x in data])

@app.get('/api/exports/reservations.csv')
def export_reservations(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute('''SELECT r.*,w.wo_no,i.item_no,i.name item_name,u.full_name reserved_by_name FROM inventory_reservations r JOIN work_orders w ON w.id=r.work_order_id JOIN inventory_items i ON i.id=r.inventory_item_id JOIN users u ON u.id=r.reserved_by ORDER BY r.id DESC'''))
    return csv_response('EUAS_material_reservations.csv',['Reservation','Work Order','Item','Item Name','Quantity','Issued','Remaining','Status','Reserved At','Released At','Reserved By'],[[x['reservation_no'],x['wo_no'],x['item_no'],x['item_name'],x['quantity'],x['issued_quantity'],round(float(x['quantity'])-float(x['issued_quantity']),3),x['status'],x['reserved_at'],x.get('released_at') or '',x['reserved_by_name']] for x in data])

@app.get('/api/exports/alarms.csv')
def export_alarms(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute("""SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,s.name site_name,w.wo_no FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id LEFT JOIN sites s ON s.id=oa.site_id LEFT JOIN work_orders w ON w.id=oa.work_order_id ORDER BY oa.id DESC"""))
    return csv_response('EUAS_operational_alarms.csv',['Alarm','Asset','Channel','Metric','Site','Severity','Status','Value','Unit','Threshold','Opened','Last Seen','Work Order'],[[x['alarm_no'],x['asset_no'],x['channel_code'],x['channel_name'],x.get('site_name') or '',x['severity'],x['status'],x['trigger_value'],x['unit'],x.get('threshold_value') if x.get('threshold_value') is not None else '',x['opened_at'],x['last_seen_at'],x.get('wo_no') or ''] for x in data])

@app.get('/api/exports/telemetry.csv')
def export_telemetry(hours:int=Query(168,ge=1,le=8760),user=Depends(current_user)):
    cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
    with db() as conn:data=rows(conn.execute("""SELECT tr.*,tc.channel_code,tc.name channel_name,tc.metric_type,tc.unit,a.asset_no FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id JOIN assets a ON a.id=tc.asset_id WHERE tr.captured_at>=? ORDER BY tr.captured_at DESC""",(cutoff,)))
    return csv_response('EUAS_telemetry.csv',['Captured','Asset','Channel','Metric','Value','Unit','Quality','Source'],[[x['captured_at'],x['asset_no'],x['channel_code'],x['metric_type'],x['value'],x['unit'],x['quality'],x['source']] for x in data])

@app.get('/api/exports/alarm-shelves.csv')
def export_alarm_shelves(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT sh.*,oa.alarm_no,a.asset_no,tc.channel_code,req.full_name requester,apr.full_name approver FROM alarm_shelves sh
      JOIN operational_alarms oa ON oa.id=sh.alarm_id JOIN assets a ON a.id=oa.asset_id JOIN telemetry_channels tc ON tc.id=oa.channel_id
      JOIN users req ON req.id=sh.requested_by LEFT JOIN users apr ON apr.id=sh.approved_by ORDER BY sh.id DESC"""))
    return csv_response('EUAS_alarm_shelves.csv',['Shelf','Alarm','Asset','Channel','Status','Reason','Duration Minutes','Requester','Approver','Start','End'],[[x['shelf_no'],x['alarm_no'],x['asset_no'],x['channel_code'],x['status'],x['reason'],x['duration_minutes'],x['requester'],x.get('approver') or '',x.get('start_at') or '',x.get('end_at') or ''] for x in data])

@app.get('/api/exports/alarm-incidents.csv')
def export_alarm_incidents(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute("""SELECT i.*,a.asset_no,a.name asset_name,rc.asset_no root_cause_asset_no,rc.name root_cause_asset_name,s.name site_name,w.wo_no FROM alarm_incidents i
          LEFT JOIN assets a ON a.id=i.asset_id LEFT JOIN assets rc ON rc.id=i.root_cause_asset_id LEFT JOIN sites s ON s.id=i.site_id LEFT JOIN work_orders w ON w.id=i.work_order_id ORDER BY i.id DESC"""))
    return csv_response('EUAS_alarm_incidents.csv',['Incident','Anchor Asset','Site','Severity','Status','Alarm Count','Correlation','Root Cause Candidate','Root Score','Topology Hops','Root Cause Reason','Opened','Last Seen','Resolved','Work Order'],[[x['incident_no'],x.get('asset_no') or '',x.get('site_name') or '',x['severity'],x['status'],x['alarm_count'],x.get('correlation_mode') or 'Asset',x.get('root_cause_asset_no') or '',x.get('root_cause_score') or 0,x.get('topology_hops') or 0,x.get('root_cause_reason') or '',x['opened_at'],x['last_seen_at'],x.get('resolved_at') or '',x.get('wo_no') or ''] for x in data])

@app.get('/api/exports/asset-topology.csv')
def export_asset_topology(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute("""SELECT t.*,ua.asset_no upstream_asset_no,ua.name upstream_asset_name,da.asset_no downstream_asset_no,da.name downstream_asset_name,u.full_name created_by_name FROM asset_topology_links t
          JOIN assets ua ON ua.id=t.upstream_asset_id JOIN assets da ON da.id=t.downstream_asset_id LEFT JOIN users u ON u.id=t.created_by ORDER BY t.link_no"""))
    return csv_response('EUAS_asset_topology.csv',['Link','Upstream Asset','Upstream Name','Relation','Downstream Asset','Downstream Name','Active','Notes','Created','Created By'],[[x['link_no'],x['upstream_asset_no'],x['upstream_asset_name'],x['relation_type'],x['downstream_asset_no'],x['downstream_asset_name'],x['active'],x.get('notes') or '',x['created_at'],x.get('created_by_name') or ''] for x in data])

@app.get('/api/exports/cbm-rules.csv')
def export_cbm_rules(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT r.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,st.consecutive_hits,st.last_triggered_at
      FROM cbm_rules r JOIN telemetry_channels tc ON tc.id=r.channel_id JOIN assets a ON a.id=tc.asset_id LEFT JOIN cbm_rule_state st ON st.rule_id=r.id ORDER BY r.id DESC"""))
    return csv_response('EUAS_cbm_rules.csv',['Rule','Name','Asset','Channel','Condition','Consecutive','Cooldown Minutes','Severity','Action','Work Priority','Active','Current Hits','Last Triggered'],[[x['rule_no'],x['name'],x['asset_no'],x['channel_code'],_cbm_rule_threshold_text(x),x['consecutive_readings'],x['cooldown_minutes'],x['severity'],x['action_type'],x['work_priority'],x['active'],x.get('consecutive_hits') or 0,x.get('last_triggered_at') or ''] for x in data])

@app.get('/api/exports/cbm-events.csv')
def export_cbm_events(user=Depends(current_user)):
    with db() as conn:data=rows(conn.execute("""SELECT e.*,r.rule_no,r.name rule_name,tc.channel_code,tc.unit,a.asset_no,w.wo_no FROM cbm_events e JOIN cbm_rules r ON r.id=e.rule_id JOIN telemetry_channels tc ON tc.id=e.channel_id JOIN assets a ON a.id=e.asset_id LEFT JOIN work_orders w ON w.id=e.work_order_id ORDER BY e.id DESC"""))
    return csv_response('EUAS_cbm_events.csv',['Event','Rule','Rule Name','Asset','Channel','Severity','Status','Value','Unit','Message','Occurrences','Opened','Last Seen','Resolved','Work Order','Resolution'],[[x['event_no'],x['rule_no'],x['rule_name'],x['asset_no'],x['channel_code'],x['severity'],x['status'],x['trigger_value'],x['unit'],x['message'],x['occurrence_count'],x['opened_at'],x['last_seen_at'],x.get('resolved_at') or '',x.get('wo_no') or '',x.get('resolution_reason') or ''] for x in data])

@app.get('/api/exports/alarm-suppressions.csv')
def export_alarm_suppressions(user=Depends(current_user)):
    with db() as conn:
        data=rows(conn.execute("""SELECT sp.*,s.name site_name,a.asset_no,tc.channel_code,u.full_name created_by_name FROM alarm_suppressions sp
          LEFT JOIN sites s ON s.id=sp.site_id LEFT JOIN assets a ON a.id=sp.asset_id LEFT JOIN telemetry_channels tc ON tc.id=sp.channel_id LEFT JOIN users u ON u.id=sp.created_by ORDER BY sp.id DESC"""))
    return csv_response('EUAS_alarm_suppressions.csv',['Suppression','Site','Asset','Channel','Reason','Start','End','Active','Created By'],[[x['suppression_no'],x.get('site_name') or '',x.get('asset_no') or '',x.get('channel_code') or '',x['reason'],x['start_at'],x['end_at'],x['active'],x.get('created_by_name') or ''] for x in data])

@app.get('/api/exports/telemetry-batches.csv')
def export_telemetry_batches(user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','executive'))):
    with db() as conn:data=rows(conn.execute('SELECT * FROM telemetry_ingest_batches ORDER BY id DESC'))
    return csv_response('EUAS_telemetry_ingest_batches.csv',['Batch','Source','Received','Accepted','Duplicates','Bad Quality','Suppressed','Alarms Opened','Alarms Updated','Alarms Cleared','CBM Opened','CBM Resolved','CBM Work Orders','Started','Completed'],[[x['batch_no'],x['source_system'],x['received_count'],x['accepted_count'],x['duplicate_count'],x['bad_quality_count'],x['suppressed_count'],x['alarms_opened'],x['alarms_updated'],x['alarms_cleared'],x.get('cbm_events_opened') or 0,x.get('cbm_events_resolved') or 0,x.get('cbm_work_orders_created') or 0,x['started_at'],x.get('completed_at') or ''] for x in data])

@app.get('/api/admin/backup')
def admin_backup(user=Depends(require_roles('admin'))):
    if DB_BACKEND!='sqlite':raise HTTPException(409,'Built-in backup is available for SQLite deployments only; use pg_dump for PostgreSQL')
    snapshot=io.BytesIO()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        snap=Path(td)/'euas.db'
        src=sqlite3.connect(DB_PATH);dst=sqlite3.connect(snap)
        try:src.backup(dst)
        finally:dst.close();src.close()
        with zipfile.ZipFile(snapshot,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(snap,'database/euas.db')
            for f in UPLOAD_DIR.rglob('*'):
                if f.is_file() and f.name!='.gitkeep':z.write(f,'uploads/'+str(f.relative_to(UPLOAD_DIR)))
            z.writestr('backup_manifest.json',json.dumps({'application':APP_NAME,'version':APP_VERSION,'created_at':now(),'database_backend':'sqlite','schema_version':SCHEMA_VERSION},indent=2))
    data=snapshot.getvalue();digest=hashlib.sha256(data).hexdigest();file_name=f'EUAS_backup_{date.today().isoformat()}.zip'
    with db() as conn:
        no=next_no(conn,'backup_records','backup_no','BKP-',10001)
        conn.execute('INSERT INTO backup_records(backup_no,database_backend,application_version,file_name,size_bytes,sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',(no,DB_BACKEND,APP_VERSION,file_name,len(data),digest,user['id'],now()))
        audit(conn,user['id'],'BACKUP','Administration',no,'',{'file':file_name,'size_bytes':len(data),'sha256':digest})
    snapshot.seek(0)
    return StreamingResponse(snapshot,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{file_name}"','X-EUAS-Backup-SHA256':digest})

@app.get('/api/admin/backups')
def backup_history(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('''SELECT b.*,u.full_name created_by_name FROM backup_records b JOIN users u ON u.id=b.created_by ORDER BY b.id DESC LIMIT 100'''))

# ---------- notifications / search / analytics / admin ----------
@app.get('/api/notifications')
def notifications(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT * FROM notifications WHERE (user_id=? OR role_code=? OR (user_id IS NULL AND role_code IS NULL)) ORDER BY is_read,id DESC LIMIT 100''',(user['id'],user['role'])))
@app.post('/api/notifications/{nid}/read')
def notification_read(nid:int,user=Depends(current_user)):
    with db() as conn:conn.execute('UPDATE notifications SET is_read=1 WHERE id=? AND (user_id=? OR role_code=? OR (user_id IS NULL AND role_code IS NULL))',(nid,user['id'],user['role']));return {'ok':True}
@app.post('/api/notifications/read-all')
def notifications_read_all(user=Depends(current_user)):
    with db() as conn:
        cur=conn.execute('UPDATE notifications SET is_read=1 WHERE is_read=0 AND (user_id=? OR role_code=? OR (user_id IS NULL AND role_code IS NULL))',(user['id'],user['role']))
        return {'ok':True,'updated':cur.rowcount}
@app.get('/api/search')
def search(q:str=Query(min_length=2),user=Depends(current_user)):
    like=f'%{q}%'
    with db() as conn:
        out=[]
        for r in rows(conn.execute(ASSET_SELECT+' WHERE a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10',(like,like))):out.append({'module':'assets','id':r['id'],'code':r['asset_no'],'title':r['name'],'subtitle':f"{r.get('site_name') or ''} · {r['condition']}"})
        for r in rows(conn.execute(WO_SELECT+' WHERE w.wo_no LIKE ? OR w.title LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10',(like,like,like,like))):out.append({'module':'work','id':r['id'],'code':r['wo_no'],'title':r['title'],'subtitle':f"{r.get('asset_no') or ''} · {r['status']}"})
        for r in rows(conn.execute('SELECT d.*,a.asset_no FROM documents d LEFT JOIN assets a ON a.id=d.asset_id WHERE d.document_no LIKE ? OR d.title LIKE ? OR a.asset_no LIKE ? LIMIT 10',(like,like,like))):out.append({'module':'documents','id':r['id'],'code':r['document_no'],'title':r['title'],'subtitle':r['category']})
        for r in rows(conn.execute('SELECT i.*,a.asset_no FROM inspections i LEFT JOIN assets a ON a.id=i.asset_id WHERE i.inspection_no LIKE ? OR i.template_name LIKE ? OR a.asset_no LIKE ? LIMIT 10',(like,like,like))):out.append({'module':'inspections','id':r['id'],'code':r['inspection_no'],'title':r['template_name'],'subtitle':r.get('asset_no') or ''})
        for r in rows(conn.execute('''SELECT o.*,a.asset_no,a.name asset_name FROM asset_outages o JOIN assets a ON a.id=o.asset_id WHERE o.outage_no LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? OR o.cause_code LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'operations','id':r['id'],'code':r['outage_no'],'title':f"Outage — {r['asset_no']}",'subtitle':f"{r['status']} · {r['outage_type']}"})
        for r in rows(conn.execute('''SELECT d.*,w.wo_no,w.title,u.full_name technician_name FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id JOIN users u ON u.id=d.technician_user_id WHERE d.dispatch_no LIKE ? OR w.wo_no LIKE ? OR w.title LIKE ? OR u.full_name LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'dispatch','id':r['id'],'code':r['dispatch_no'],'title':f"{r['wo_no']} — {r['technician_name']}",'subtitle':r['status']})
        for r in rows(conn.execute('''SELECT oa.*,tc.channel_code,tc.name channel_name,a.asset_no,a.name asset_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id WHERE oa.alarm_no LIKE ? OR tc.channel_code LIKE ? OR tc.name LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10''',(like,like,like,like,like))):out.append({'module':'telemetry','id':r['id'],'code':r['alarm_no'],'title':f"{r['asset_no']} — {r['channel_name']} alarm",'subtitle':f"{r['severity']} · {r['status']}"})
        for r in rows(conn.execute('''SELECT tc.*,a.asset_no,a.name asset_name FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id WHERE tc.channel_code LIKE ? OR tc.name LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'telemetry','id':r['id'],'code':r['channel_code'],'title':r['name'],'subtitle':f"{r['asset_no']} · {r['metric_type']}"})
        for r in rows(conn.execute('''SELECT i.*,a.asset_no,a.name asset_name FROM alarm_incidents i LEFT JOIN assets a ON a.id=i.asset_id WHERE i.incident_no LIKE ? OR i.title LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'commandcenter','id':r['id'],'code':r['incident_no'],'title':r['title'],'subtitle':f"{r['severity']} · {r['status']} · {r['alarm_count']} alarm(s)"})
        for r in rows(conn.execute('''SELECT sp.*,a.asset_no,tc.channel_code FROM alarm_suppressions sp LEFT JOIN assets a ON a.id=sp.asset_id LEFT JOIN telemetry_channels tc ON tc.id=sp.channel_id WHERE sp.suppression_no LIKE ? OR sp.reason LIKE ? OR a.asset_no LIKE ? OR tc.channel_code LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'commandcenter','id':r['id'],'code':r['suppression_no'],'title':'Alarm suppression','subtitle':f"{r.get('asset_no') or r.get('channel_code') or 'Site'} · {r['reason']}"})
        return out
@app.get('/api/analytics')
def analytics(user=Depends(current_user)):
    with db() as conn:
        total=conn.execute('SELECT COUNT(*) FROM work_orders').fetchone()[0];done=conn.execute("SELECT COUNT(*) FROM work_orders WHERE status IN ('Completed','Closed')").fetchone()[0]
        reliability_all=_asset_reliability_rows(conn,365,None);site_reliability=_site_reliability_rows(conn,365)
        rel_failures=sum(x['failures'] for x in reliability_all);rel_downtime=sum(x['downtime_hours'] for x in reliability_all);rel_period=sum(x['period_hours'] for x in reliability_all);rel_uptime=max(0.0,rel_period-rel_downtime)
        repair=(rel_downtime/rel_failures) if rel_failures else 0;mtbf=(rel_uptime/rel_failures) if rel_failures else None;availability=100*rel_uptime/rel_period if rel_period else 100
        pm_total=conn.execute('SELECT COUNT(*) FROM maintenance_plans WHERE active=1').fetchone()[0];pm_over=conn.execute("SELECT COUNT(*) FROM maintenance_plans WHERE active=1 AND trigger_type='Calendar' AND next_due IS NOT NULL AND next_due<?",(date.today().isoformat(),)).fetchone()[0]
        monthly=rows(conn.execute("""SELECT substr(COALESCE(actual_finish,created_at),1,7) period,COUNT(*) count,COALESCE(SUM(actual_cost),0) cost FROM work_orders GROUP BY substr(COALESCE(actual_finish,created_at),1,7) ORDER BY period"""))
        backlog=rows(conn.execute("""SELECT priority,COUNT(*) count FROM work_orders WHERE status NOT IN ('Completed','Closed','Cancelled') GROUP BY priority ORDER BY CASE priority WHEN 'Emergency' THEN 5 WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 ELSE 1 END DESC"""))
        procurement_vendor=rows(conn.execute("""SELECT v.name vendor,COUNT(po.id) orders,COALESCE(SUM(po.total_cost),0) spend FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id GROUP BY v.id,v.name ORDER BY spend DESC"""))
        inventory_health=rows(conn.execute("""SELECT CASE WHEN current_stock-reserved_stock<=0 THEN 'Out of Stock' WHEN current_stock-reserved_stock<=reorder_point THEN 'Low Stock' WHEN max_level>0 AND current_stock>max_level THEN 'Overstock' ELSE 'Healthy' END stock_state,COUNT(*) count FROM inventory_items GROUP BY stock_state"""))
        reliability=sorted(reliability_all,key=lambda x:(-x['failures'],x['availability_pct'],x['asset_no']))[:10]
        approval_summary=rows(conn.execute("SELECT status,COUNT(*) count FROM approval_requests GROUP BY status ORDER BY status"))
        cost_ledger=rows(conn.execute("SELECT cost_type,COUNT(*) entries,COALESCE(SUM(amount),0) amount FROM maintenance_cost_ledger GROUP BY cost_type ORDER BY amount DESC"))
        health_portfolio=[_asset_health(conn,x['id']) for x in rows(conn.execute('SELECT id FROM assets ORDER BY asset_no'))]
        forecast=_maintenance_forecast(conn,90,None)
        alarm_summary=rows(conn.execute("SELECT severity,status,COUNT(*) count FROM operational_alarms GROUP BY severity,status ORDER BY severity,status"))
        incident_summary=rows(conn.execute("SELECT severity,status,COUNT(*) count FROM alarm_incidents GROUP BY severity,status ORDER BY severity,status"))
        telemetry_quality=_telemetry_quality_summary(conn,24,None)
        ingest_summary=rows(conn.execute("SELECT source_system,COUNT(*) batches,COALESCE(SUM(accepted_count),0) accepted,COALESCE(SUM(duplicate_count),0) duplicates,COALESCE(SUM(bad_quality_count),0) bad_quality FROM telemetry_ingest_batches GROUP BY source_system ORDER BY accepted DESC"))
        return {
          'summary':{'mtbf':round(mtbf,1) if mtbf is not None else None,'mttr':round(repair,1),'availability':round(availability,2),'pm_compliance':round(100*(pm_total-pm_over)/pm_total,1) if pm_total else 100,'work_order_completion_rate':round(100*done/max(total,1),1),'reliability_period_days':365},
          'maintenance_by_type':rows(conn.execute('SELECT work_type,COUNT(*) count,COALESCE(SUM(actual_cost),0) cost FROM work_orders GROUP BY work_type ORDER BY count DESC')),
          'cost_by_asset':rows(conn.execute('SELECT a.asset_no,a.name,COALESCE(SUM(w.actual_cost),0) maintenance_cost FROM assets a LEFT JOIN work_orders w ON w.asset_id=a.id GROUP BY a.id,a.asset_no,a.name ORDER BY maintenance_cost DESC')),
          'inventory_by_category':rows(conn.execute('SELECT category,COALESCE(SUM(current_stock*unit_price),0) value,COUNT(*) items FROM inventory_items GROUP BY category')),
          'hse_by_risk':rows(conn.execute("SELECT CASE WHEN risk_score>=15 THEN 'Extreme' WHEN risk_score>=10 THEN 'High' WHEN risk_score>=5 THEN 'Medium' ELSE 'Low' END risk_band,COUNT(*) count FROM safety_incidents GROUP BY risk_band")),
          'monthly_work':monthly,'backlog_by_priority':backlog,'procurement_by_vendor':procurement_vendor,'inventory_health':inventory_health,'asset_reliability':reliability,'site_reliability':site_reliability,'approval_summary':approval_summary,'maintenance_cost_ledger':cost_ledger,'asset_health_scores':health_portfolio,'maintenance_forecast':forecast,'operational_alarm_summary':alarm_summary,'operational_incident_summary':incident_summary,'telemetry_quality_24h':telemetry_quality,'telemetry_ingestion_sources':ingest_summary
        }
@app.get('/api/audit/integrity')
def audit_integrity(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return verify_audit_chain(conn)

RETENTION_CLASS_MAP = {
    'Audit Trail': {'table':'audit_logs','field':'created_at','key':'id','execution_supported':False,'block_reason':'Protected tamper-evident audit evidence'},
    'Approval Signatures': {'table':'approval_signature_evidence','field':'signed_at','key':'evidence_no','execution_supported':False,'block_reason':'Protected approval-signature evidence'},
    'Work Management': {'table':'work_orders','field':'created_at','key':'wo_no','execution_supported':False,'block_reason':'Protected maintenance history'},
    'Documents': {'table':'documents','field':'uploaded_at','key':'document_no','execution_supported':False,'block_reason':'Binary-object deletion requires coordinated external object-storage lifecycle'},
    'Notifications': {'table':'notifications','field':'created_at','key':'id','execution_supported':True,'block_reason':''},
    'Integration Events': {'table':'event_outbox','field':'created_at','key':'event_no','execution_supported':True,'block_reason':''},
}

def _retention_digest(prev_hash:str, manifest_json:str) -> str:
    return hashlib.sha256(((prev_hash or '')+'\n'+manifest_json).encode('utf-8')).hexdigest()

def _retention_holds_for(conn,data_class:str):
    return rows(conn.execute("SELECT * FROM retention_holds WHERE data_class=? AND status='Active' ORDER BY id",(data_class,)))

def _retention_policy_plan(conn,p,reference:datetime):
    cfg=RETENTION_CLASS_MAP.get(p['data_class']); cutoff=(reference-timedelta(days=int(p['retention_days']))).isoformat(timespec='seconds')
    result={**p,'cutoff':cutoff,'eligible_records':0,'held_records':0,'protected_records':0,'blocked_records':0,'executable_records':0,'execution_supported':False,'block_reason':'No execution mapping'}
    if not cfg or not p['active']: return result
    table,field,key=cfg['table'],cfg['field'],cfg['key']
    eligible=int(conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {field}<?',(cutoff,)).fetchone()[0] or 0)
    result['eligible_records']=eligible; result['execution_supported']=bool(cfg['execution_supported']); result['block_reason']=cfg['block_reason']
    if not eligible:return result
    holds=_retention_holds_for(conn,p['data_class']); hold_keys={str(x['record_key']) for x in holds}
    if '*' in hold_keys: held=eligible
    elif hold_keys:
        keys=[str(r[key]) for r in conn.execute(f'SELECT {key} FROM {table} WHERE {field}<?',(cutoff,)).fetchall()]
        held=sum(1 for x in keys if x in hold_keys)
    else: held=0
    result['held_records']=held; remaining=max(eligible-held,0)
    if p['protected']: result['protected_records']=remaining
    elif not cfg['execution_supported']: result['blocked_records']=remaining
    else: result['executable_records']=remaining
    return result

def _retention_plans(conn,data_class:Optional[str]=None,reference:Optional[datetime]=None):
    reference=reference or datetime.now(); args=[]; sql='SELECT * FROM retention_policies WHERE 1=1'
    if data_class: sql+=' AND data_class=?';args.append(data_class)
    sql+=' ORDER BY data_class'
    policies=rows(conn.execute(sql,args))
    if data_class and not policies: raise HTTPException(404,'Retention data class not found')
    return [_retention_policy_plan(conn,p,reference) for p in policies]

def verify_retention_run_chain(conn):
    prev='';checked=0
    for r in conn.execute('SELECT * FROM retention_runs ORDER BY id').fetchall():
        checked+=1; manifest_json=r['manifest_json'] or '{}'
        try: manifest=json.loads(manifest_json); summary=json.loads(r['summary_json'] or '{}')
        except Exception:return {'valid':False,'checked':checked,'first_invalid_id':r['id'],'head_hash':prev,'reason':'invalid_json'}
        if manifest.get('run_no')!=r['run_no'] or manifest.get('mode')!=r['mode'] or manifest.get('status')!=r['status'] or manifest.get('summary')!=summary:
            return {'valid':False,'checked':checked,'first_invalid_id':r['id'],'head_hash':prev,'reason':'column_payload_mismatch'}
        expected=_retention_digest(prev,manifest_json)
        if (r['prev_hash'] or '')!=prev or (r['manifest_hash'] or '')!=expected:
            return {'valid':False,'checked':checked,'first_invalid_id':r['id'],'head_hash':prev,'reason':'hash_mismatch'}
        prev=r['manifest_hash']
    return {'valid':True,'checked':checked,'first_invalid_id':None,'head_hash':prev,'reason':'ok'}

def _execute_retention_run(conn,actor,mode:str,data_class:Optional[str]=None):
    reference=datetime.now(); started=now(); run_no=next_no(conn,'retention_runs','run_no','RET-RUN-',1)
    previous=conn.execute("SELECT manifest_hash FROM retention_runs WHERE manifest_hash<>'' ORDER BY id DESC LIMIT 1").fetchone(); prev_hash=previous['manifest_hash'] if previous else ''
    cur=conn.execute("INSERT INTO retention_runs(run_no,mode,data_class,status,requested_by,started_at,summary_json,manifest_json,prev_hash,manifest_hash) VALUES(?,?,?,'Running',?,?,'{}','{}',?,'')",(run_no,mode,data_class,actor['id'],started,prev_hash));run_id=cur.lastrowid
    try:
        plans=_retention_plans(conn,data_class,reference); items=[]; total_eligible=total_held=total_protected=total_blocked=total_purged=total_executable=0
        for plan in plans:
            purged=0; cfg=RETENTION_CLASS_MAP.get(plan['data_class'])
            if mode=='Execute' and plan['active'] and cfg and cfg['execution_supported'] and not plan['protected'] and plan['executable_records']:
                hold_keys={str(x['record_key']) for x in _retention_holds_for(conn,plan['data_class'])}
                table,field,key=cfg['table'],cfg['field'],cfg['key']
                candidates=rows(conn.execute(f'SELECT id,{key} FROM {table} WHERE {field}<? ORDER BY id',(plan['cutoff'],)))
                for row in candidates:
                    if '*' in hold_keys or str(row[key]) in hold_keys: continue
                    purged += int(conn.execute(f'DELETE FROM {table} WHERE id=?',(row['id'],)).rowcount or 0)
            details={'execution_supported':plan['execution_supported'],'block_reason':plan['block_reason'],'executable_before_run':plan['executable_records']}
            conn.execute('''INSERT INTO retention_run_items(run_id,policy_id,data_class,cutoff,eligible_count,held_count,protected_count,blocked_count,purged_count,details_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',(run_id,plan['id'],plan['data_class'],plan['cutoff'],plan['eligible_records'],plan['held_records'],plan['protected_records'],plan['blocked_records'],purged,json.dumps(details,sort_keys=True)))
            item={'data_class':plan['data_class'],'cutoff':plan['cutoff'],'eligible_count':plan['eligible_records'],'held_count':plan['held_records'],'protected_count':plan['protected_records'],'blocked_count':plan['blocked_records'],'purged_count':purged,**details};items.append(item)
            total_eligible+=plan['eligible_records'];total_held+=plan['held_records'];total_protected+=plan['protected_records'];total_blocked+=plan['blocked_records'];total_purged+=purged;total_executable+=plan['executable_records']
        summary={'eligible_records':total_eligible,'held_records':total_held,'protected_records':total_protected,'blocked_records':total_blocked,'executable_records':total_executable,'purged_records':total_purged,'policies_evaluated':len(plans)}
        finished=now();status='Succeeded'
        manifest={'format':'EUAS-Retention-Evidence-v1','run_no':run_no,'mode':mode,'data_class':data_class or '*','status':status,'requested_by':{'id':actor['id'],'username':actor['username'],'role':actor['role']},'started_at':started,'finished_at':finished,'summary':summary,'items':items}
        manifest_json=json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False);digest=_retention_digest(prev_hash,manifest_json)
        conn.execute('UPDATE retention_runs SET status=?,finished_at=?,summary_json=?,manifest_json=?,prev_hash=?,manifest_hash=? WHERE id=?',(status,finished,json.dumps(summary,sort_keys=True),manifest_json,prev_hash,digest,run_id))
        audit(conn,actor['id'],'EXECUTE RETENTION' if mode=='Execute' else 'PREVIEW RETENTION','Governance',run_no,'',summary)
        return {'id':run_id,'run_no':run_no,'mode':mode,'status':status,'summary':summary,'manifest_hash':digest,'items':items}
    except Exception as exc:
        finished=now();summary={'error':str(exc)[:500]};manifest={'format':'EUAS-Retention-Evidence-v1','run_no':run_no,'mode':mode,'data_class':data_class or '*','status':'Failed','requested_by':{'id':actor['id'],'username':actor['username'],'role':actor['role']},'started_at':started,'finished_at':finished,'summary':summary,'items':[]}
        manifest_json=json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False);digest=_retention_digest(prev_hash,manifest_json)
        conn.execute("UPDATE retention_runs SET status='Failed',finished_at=?,summary_json=?,manifest_json=?,prev_hash=?,manifest_hash=?,error_message=? WHERE id=?",(finished,json.dumps(summary,sort_keys=True),manifest_json,prev_hash,digest,str(exc)[:1000],run_id));return {'id':run_id,'run_no':run_no,'mode':mode,'status':'Failed','summary':summary,'manifest_hash':digest,'items':[],'error':str(exc)}

class RetentionPatch(BaseModel): retention_days:int=Field(gt=0,le=36500); active:Optional[bool]=None
class RetentionHoldIn(BaseModel): data_class:str; record_key:str='*'; reason:str=Field(min_length=10,max_length=500)
class RetentionHoldRelease(BaseModel): current_password:str; reason:str=Field(min_length=10,max_length=500)
class RetentionRunIn(BaseModel): mode:str='Preview'; data_class:Optional[str]=None; current_password:str=''; confirmation:str=''

@app.get('/api/governance/retention')
def retention_policies(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('SELECT * FROM retention_policies ORDER BY data_class'))

@app.get('/api/governance/retention/preview')
def retention_preview(data_class:str='',user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return _retention_plans(conn,data_class or None)

@app.patch('/api/governance/retention/{policy_id}')
def update_retention(policy_id:int,body:RetentionPatch,user=Depends(require_permission('governance.retention.execute','admin'))):
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM retention_policies WHERE id=?',(policy_id,),'Retention policy not found');changes={'retention_days':body.retention_days,'updated_at':now()}
        if body.active is not None:changes['active']=1 if body.active else 0
        conn.execute('UPDATE retention_policies SET '+','.join(f'{k}=?' for k in changes)+' WHERE id=?',(*changes.values(),policy_id));audit(conn,user['id'],'UPDATE RETENTION','Governance',old['policy_code'],old,changes);return {'ok':True}

@app.get('/api/governance/retention/holds')
def retention_holds(status:str='',user=Depends(require_roles('admin','maintenance_manager','executive'))):
    sql='''SELECT h.*,u.full_name placed_by_name,ru.full_name released_by_name FROM retention_holds h JOIN users u ON u.id=h.placed_by LEFT JOIN users ru ON ru.id=h.released_by WHERE 1=1''';args=[]
    if status:sql+=' AND h.status=?';args.append(status)
    sql+=' ORDER BY h.id DESC'
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/governance/retention/holds')
def create_retention_hold(body:RetentionHoldIn,user=Depends(require_permission('governance.legal_hold.manage','admin'))):
    if body.data_class not in RETENTION_CLASS_MAP:raise HTTPException(400,'Unknown retention data class')
    record_key=(body.record_key or '*').strip()
    with db() as conn:
        existing=conn.execute("SELECT id FROM retention_holds WHERE data_class=? AND record_key=? AND status='Active'",(body.data_class,record_key)).fetchone()
        if existing:raise HTTPException(409,'An active legal hold already covers this scope')
        no=next_no(conn,'retention_holds','hold_no','HOLD-',1);cur=conn.execute("INSERT INTO retention_holds(hold_no,data_class,record_key,reason,status,placed_by,placed_at) VALUES(?,?,?,?, 'Active',?,?)",(no,body.data_class,record_key,body.reason.strip(),user['id'],now()));audit(conn,user['id'],'PLACE HOLD','Governance',no,'',body.model_dump());return {'id':cur.lastrowid,'hold_no':no,'status':'Active'}

@app.post('/api/governance/retention/holds/{hold_id}/release')
def release_retention_hold(hold_id:int,body:RetentionHoldRelease,user=Depends(require_permission('governance.legal_hold.manage','admin'))):
    with db() as conn:
        acct=get_or_404(conn,'SELECT password_hash FROM users WHERE id=?',(user['id'],),'User not found')
        if not verify_password(body.current_password,acct['password_hash']):raise HTTPException(401,'Current password is incorrect')
        hold=get_or_404(conn,'SELECT * FROM retention_holds WHERE id=?',(hold_id,),'Retention hold not found')
        if hold['status']!='Active':raise HTTPException(409,'Retention hold is not active')
        conn.execute("UPDATE retention_holds SET status='Released',released_by=?,released_at=?,release_reason=? WHERE id=?",(user['id'],now(),body.reason.strip(),hold_id));audit(conn,user['id'],'RELEASE HOLD','Governance',hold['hold_no'],hold,{'reason':body.reason});return {'ok':True,'hold_no':hold['hold_no'],'status':'Released'}

@app.get('/api/governance/retention/runs')
def retention_runs(limit:int=Query(100,ge=1,le=500),user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('''SELECT r.*,u.full_name requested_by_name FROM retention_runs r JOIN users u ON u.id=r.requested_by ORDER BY r.id DESC LIMIT ?''',(limit,)))

@app.post('/api/governance/retention/runs')
def create_retention_run(body:RetentionRunIn,user=Depends(require_roles('admin','maintenance_manager'))):
    mode=(body.mode or 'Preview').strip().title()
    if mode not in ('Preview','Execute'):raise HTTPException(400,'Mode must be Preview or Execute')
    if body.data_class and body.data_class not in RETENTION_CLASS_MAP:raise HTTPException(400,'Unknown retention data class')
    if mode=='Execute':
        if not has_permission(user,'governance.retention.execute'):raise HTTPException(403,'Missing permission: governance.retention.execute')
        if body.confirmation!='EXECUTE RETENTION':raise HTTPException(400,'Exact confirmation EXECUTE RETENTION is required')
        with db() as conn:
            acct=get_or_404(conn,'SELECT password_hash FROM users WHERE id=?',(user['id'],),'User not found')
            if not verify_password(body.current_password,acct['password_hash']):raise HTTPException(401,'Current password is incorrect')
            result=_execute_retention_run(conn,user,mode,body.data_class)
        if result['status']=='Failed':raise HTTPException(500,result.get('error','Retention execution failed'))
        return result
    with db() as conn:result=_execute_retention_run(conn,user,mode,body.data_class)
    if result['status']=='Failed':raise HTTPException(500,result.get('error','Retention preview failed'))
    return result

@app.get('/api/governance/retention/runs/{run_id}')
def retention_run_detail(run_id:int,user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:
        run=get_or_404(conn,'SELECT r.*,u.full_name requested_by_name FROM retention_runs r JOIN users u ON u.id=r.requested_by WHERE r.id=?',(run_id,),'Retention run not found');run['summary']=json.loads(run['summary_json'] or '{}');run['manifest']=json.loads(run['manifest_json'] or '{}');run['items']=rows(conn.execute('SELECT * FROM retention_run_items WHERE run_id=? ORDER BY id',(run_id,)));return run

@app.get('/api/governance/retention/verify')
def retention_run_integrity(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return verify_retention_run_chain(conn)

@app.get('/api/governance/retention/runs/{run_id}/evidence')
def retention_run_evidence(run_id:int,user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:
        run=get_or_404(conn,'SELECT * FROM retention_runs WHERE id=?',(run_id,),'Retention run not found');items=rows(conn.execute('SELECT * FROM retention_run_items WHERE run_id=? ORDER BY id',(run_id,)));verification=verify_retention_run_chain(conn)
    manifest=json.loads(run['manifest_json'] or '{}');buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',json.dumps(manifest,indent=2,ensure_ascii=False))
        sio=io.StringIO();w=csv.writer(sio);w.writerow(['Data Class','Cutoff','Eligible','Held','Protected','Blocked','Purged'])
        for x in items:w.writerow([x['data_class'],x['cutoff'],x['eligible_count'],x['held_count'],x['protected_count'],x['blocked_count'],x['purged_count']])
        z.writestr('items.csv',sio.getvalue());z.writestr('verification.json',json.dumps({'run_no':run['run_no'],'manifest_hash':run['manifest_hash'],'chain':verification},indent=2))
    buf.seek(0);return StreamingResponse(iter([buf.getvalue()]),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="EUAS_{run["run_no"]}_retention_evidence.zip"'})

@app.get('/api/exports/retention-runs.csv')
def export_retention_runs(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:data=rows(conn.execute('SELECT r.*,u.full_name requested_by_name FROM retention_runs r JOIN users u ON u.id=r.requested_by ORDER BY r.id DESC'))
    return csv_response('EUAS_retention_runs.csv',['Run','Mode','Data Class','Status','Requested By','Started','Finished','Manifest Hash'],[[x['run_no'],x['mode'],x.get('data_class') or '*',x['status'],x['requested_by_name'],x['started_at'],x.get('finished_at') or '',x.get('manifest_hash') or ''] for x in data])

@app.get('/api/exports/access-control.csv')
def export_access_control(user=Depends(require_permission('admin.permissions.manage','admin'))):
    with db() as conn:
        data=[]
        for r in conn.execute('''SELECT r.name role_name,r.code role_code,p.code permission_code,p.name permission_name FROM role_permissions rp JOIN roles r ON r.id=rp.role_id JOIN permissions p ON p.id=rp.permission_id ORDER BY r.name,p.code''').fetchall():
            data.append(['Role Grant',r['role_name'],r['role_code'],r['permission_code'],'Allow','',''])
        for r in conn.execute('''SELECT u.full_name,u.username,p.code permission_code,o.effect,o.reason,o.expires_at FROM user_permission_overrides o JOIN users u ON u.id=o.user_id JOIN permissions p ON p.id=o.permission_id ORDER BY u.full_name,p.code''').fetchall():
            data.append(['User Override',r['full_name'],r['username'],r['permission_code'],r['effect'],r['expires_at'] or '',r['reason']])
    return csv_response('EUAS_access_control.csv',['Type','Subject','Subject Code','Permission','Effect','Expires','Reason'],data)

@app.get('/api/audit')
def audit_list(limit:int=Query(200,ge=1,le=1000),module:str='',action:str='',q:str='',user=Depends(require_roles('admin','maintenance_manager','executive'))):
    sql='''SELECT a.*,u.full_name,u.username FROM audit_logs a JOIN users u ON u.id=a.user_id WHERE 1=1''';args=[]
    if module:sql+=' AND a.module=?';args.append(module)
    if action:sql+=' AND a.action=?';args.append(action)
    if q:
        like=f'%{q}%';sql+=' AND (a.record_id LIKE ? OR a.old_value LIKE ? OR a.new_value LIKE ? OR u.full_name LIKE ?)';args += [like,like,like,like]
    sql+=' ORDER BY a.id DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))
@app.get('/api/admin/users')
def list_users(user=Depends(require_permission('admin.users.manage','admin'))):
    with db() as conn:return rows(conn.execute('''SELECT u.id,u.username,u.full_name,u.email,u.department,u.phone,u.active,r.code role,r.name role_name FROM users u JOIN roles r ON r.id=u.role_id ORDER BY u.full_name'''))
@app.post('/api/admin/users')
def create_user(body:UserIn,user=Depends(require_permission('admin.users.manage','admin'))):
    with db() as conn:
        role=get_or_404(conn,'SELECT id FROM roles WHERE code=?',(body.role_code,),'Role not found');cur=conn.execute('INSERT INTO users(username,password_hash,full_name,email,role_id,department,phone,created_at) VALUES(?,?,?,?,?,?,?,?)',(body.username,hash_password(body.password),body.full_name,body.email,role['id'],body.department,body.phone,now()));audit(conn,user['id'],'CREATE','Administration',body.username,'',{'role':body.role_code,'full_name':body.full_name});return {'id':cur.lastrowid}
@app.patch('/api/admin/users/{user_id}/status')
def set_user_status(user_id:int,body:UserStatusIn,user=Depends(require_permission('admin.users.manage','admin'))):
    if user_id==user['id'] and not body.active: raise HTTPException(400,'You cannot deactivate your own account')
    with db() as conn:
        target=get_or_404(conn,'SELECT u.*,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(user_id,),'User not found')
        if target['role']=='admin' and not body.active:
            admins=conn.execute("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='admin' AND u.active=1").fetchone()[0]
            if admins<=1: raise HTTPException(409,'At least one active administrator is required')
        conn.execute('UPDATE users SET active=? WHERE id=?',(1 if body.active else 0,user_id))
        if not body.active: conn.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        audit(conn,user['id'],'ACTIVATE' if body.active else 'DEACTIVATE','Administration',target['username'],target['active'],1 if body.active else 0)
        return {'ok':True,'active':body.active}
ACCESS_CONFIRMATION='UPDATE ACCESS'
ADMIN_CORE_PERMISSIONS={'admin.users.manage','admin.permissions.manage'}

def _verify_access_change(conn,user,password:str,confirmation:str):
    if confirmation!=ACCESS_CONFIRMATION:raise HTTPException(400,f'Exact confirmation {ACCESS_CONFIRMATION} is required')
    acct=get_or_404(conn,'SELECT password_hash FROM users WHERE id=? AND active=1',(user['id'],),'User not found')
    if not password or not verify_password(password,acct['password_hash']):raise HTTPException(401,'Access-control re-authentication failed')

def _permission_codes(conn):
    return {r['code']:dict(r) for r in conn.execute('SELECT * FROM permissions ORDER BY category,name')}

@app.get('/api/admin/access-control')
def access_control_snapshot(user=Depends(require_permission('admin.permissions.manage','admin'))):
    with db() as conn:
        permissions=rows(conn.execute('SELECT id,code,name,category,risk_level,description FROM permissions ORDER BY category,name'))
        roles=rows(conn.execute('SELECT id,code,name FROM roles ORDER BY name'))
        for role in roles:
            role['permissions']=[r['code'] for r in conn.execute("""SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=? ORDER BY p.code""",(role['id'],)).fetchall()]
        users=rows(conn.execute('''SELECT u.id,u.username,u.full_name,u.active,r.code role,r.name role_name FROM users u JOIN roles r ON r.id=u.role_id ORDER BY u.full_name'''))
        stamp=now()
        overrides=rows(conn.execute('''SELECT o.user_id,p.code permission_code,o.effect,o.reason,o.expires_at,o.updated_at,up.full_name updated_by_name
          FROM user_permission_overrides o JOIN permissions p ON p.id=o.permission_id LEFT JOIN users up ON up.id=o.updated_by
          WHERE o.expires_at IS NULL OR o.expires_at='' OR o.expires_at>? ORDER BY o.user_id,p.code''',(stamp,)))
        return {'confirmation_phrase':ACCESS_CONFIRMATION,'permissions':permissions,'roles':roles,'users':users,'overrides':overrides}

@app.get('/api/admin/roles/{role_code}/permissions')
def role_permissions(role_code:str,user=Depends(require_permission('admin.permissions.manage','admin'))):
    with db() as conn:
        role=get_or_404(conn,'SELECT id,code,name FROM roles WHERE code=?',(role_code,),'Role not found')
        role['permissions']=[r['code'] for r in conn.execute("""SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=? ORDER BY p.code""",(role['id'],)).fetchall()]
        return role

@app.put('/api/admin/roles/{role_code}/permissions')
def update_role_permissions(role_code:str,body:RolePermissionUpdateIn,user=Depends(require_permission('admin.permissions.manage','admin'))):
    requested=sorted(set(x.strip() for x in body.permissions if x.strip()))
    with db() as conn:
        _verify_access_change(conn,user,body.current_password,body.confirmation)
        role=get_or_404(conn,'SELECT id,code,name FROM roles WHERE code=?',(role_code,),'Role not found')
        catalog=_permission_codes(conn);unknown=[x for x in requested if x not in catalog]
        if unknown:raise HTTPException(400,{'message':'Unknown permissions','permissions':unknown})
        if role_code=='admin' and not ADMIN_CORE_PERMISSIONS.issubset(requested):
            raise HTTPException(409,'Administrator role must retain core access-management permissions')
        old=sorted(r['code'] for r in conn.execute("""SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=?""",(role['id'],)).fetchall())
        conn.execute('DELETE FROM role_permissions WHERE role_id=?',(role['id'],))
        for code in requested:conn.execute('INSERT INTO role_permissions(role_id,permission_id) VALUES(?,?)',(role['id'],catalog[code]['id']))
        audit(conn,user['id'],'UPDATE ROLE PERMISSIONS','Administration',role_code,old,requested)
        emit_event(conn,'access.role_permissions.updated','role',role_code,{'role':role_code,'permissions':requested,'reason':body.reason,'updated_by':user['username']})
        return {'ok':True,'role':role_code,'permissions':requested}

@app.get('/api/admin/users/{user_id}/permission-overrides')
def user_permission_overrides(user_id:int,user=Depends(require_permission('admin.permissions.manage','admin'))):
    with db() as conn:
        get_or_404(conn,'SELECT id FROM users WHERE id=?',(user_id,),'User not found')
        return rows(conn.execute('''SELECT o.user_id,p.id permission_id,p.code permission_code,p.name,o.effect,o.reason,o.expires_at,o.updated_at,u.full_name updated_by_name
          FROM user_permission_overrides o JOIN permissions p ON p.id=o.permission_id LEFT JOIN users u ON u.id=o.updated_by
          WHERE o.user_id=? ORDER BY p.code''',(user_id,)))

@app.post('/api/admin/users/{user_id}/permission-overrides')
def set_user_permission_override(user_id:int,body:UserPermissionOverrideIn,user=Depends(require_permission('admin.permissions.manage','admin'))):
    effect=body.effect.strip().title()
    if effect not in ('Allow','Deny','Inherit'):raise HTTPException(400,'Effect must be Allow, Deny or Inherit')
    with db() as conn:
        _verify_access_change(conn,user,body.current_password,body.confirmation)
        target=get_or_404(conn,'SELECT u.id,u.username,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(user_id,),'User not found')
        perm=get_or_404(conn,'SELECT * FROM permissions WHERE code=?',(body.permission_code,),'Permission not found')
        if target['role']=='admin' and perm['code'] in ADMIN_CORE_PERMISSIONS and effect=='Deny':
            raise HTTPException(409,'Core administrator permissions cannot be explicitly denied')
        expires=body.expires_at
        if expires:
            try:
                if _dt(expires)<=datetime.now():raise HTTPException(422,'Override expiry must be in the future')
            except HTTPException:raise
            except Exception:raise HTTPException(422,'Invalid override expiry')
        old=conn.execute('SELECT effect,reason,expires_at FROM user_permission_overrides WHERE user_id=? AND permission_id=?',(user_id,perm['id'])).fetchone()
        old=dict(old) if old else None
        if effect=='Inherit':
            conn.execute('DELETE FROM user_permission_overrides WHERE user_id=? AND permission_id=?',(user_id,perm['id']))
            new={'effect':'Inherit'}
        else:
            conn.execute('''INSERT INTO user_permission_overrides(user_id,permission_id,effect,reason,expires_at,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(user_id,permission_id) DO UPDATE SET effect=excluded.effect,reason=excluded.reason,expires_at=excluded.expires_at,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
              (user_id,perm['id'],effect,body.reason.strip(),expires,user['id'],now()))
            new={'effect':effect,'reason':body.reason.strip(),'expires_at':expires}
        audit(conn,user['id'],'UPDATE USER PERMISSION','Administration',f"{target['username']}:{perm['code']}",old or '',new)
        emit_event(conn,'access.user_permission.updated','user',target['username'],{'permission':perm['code'],'effect':effect,'reason':body.reason,'expires_at':expires,'updated_by':user['username']})
        return {'ok':True,'user_id':user_id,'permission_code':perm['code'],'effect':effect,'expires_at':expires}

@app.patch('/api/admin/users/{user_id}/role')
def update_user_role(user_id:int,body:UserRoleUpdateIn,user=Depends(require_permission('admin.users.manage','admin'))):
    with db() as conn:
        _verify_access_change(conn,user,body.current_password,body.confirmation)
        target=get_or_404(conn,'SELECT u.id,u.username,u.active,r.code role FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(user_id,),'User not found')
        role=get_or_404(conn,'SELECT id,code,name FROM roles WHERE code=?',(body.role_code,),'Role not found')
        if target['role']=='admin' and role['code']!='admin' and target['active']:
            active_admins=conn.execute("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='admin' AND u.active=1").fetchone()[0]
            if active_admins<=1:raise HTTPException(409,'At least one active administrator is required')
        conn.execute('UPDATE users SET role_id=? WHERE id=?',(role['id'],user_id))
        if user_id!=user['id']:conn.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        audit(conn,user['id'],'UPDATE USER ROLE','Administration',target['username'],target['role'],role['code'])
        emit_event(conn,'access.user_role.updated','user',target['username'],{'old_role':target['role'],'new_role':role['code'],'reason':body.reason,'updated_by':user['username']})
        return {'ok':True,'user_id':user_id,'role':role['code']}

@app.get('/api/admin/roles')
def list_roles(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT * FROM roles ORDER BY name'))

app.mount('/uploads',StaticFiles(directory=UPLOAD_DIR),name='uploads')
app.mount('/static',StaticFiles(directory=STATIC_DIR),name='static')
@app.get('/')
def root():return FileResponse(STATIC_DIR/'index.html')
