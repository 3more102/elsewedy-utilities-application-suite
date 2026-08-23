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
from .database import db, init_db, now, audit_digest
from .audit_verification import AuditIntegrityError, replay_audit_history, verify_audit_chain_report
from .auth import hash_password, verify_password, current_user, require_roles
from .report_html import render_snapshot_report_html, render_work_order_report_html

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(hash_password)
    with db() as conn:
        _backfill_work_order_slas(conn)
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
_LOGIN_FAILURES: dict[str, list[float]] = {}
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5

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

def _login_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else 'unknown'
    return f'{host}:{username.lower()}'

def _login_is_blocked(key: str) -> bool:
    now_ts = time.time()
    recent = [t for t in _LOGIN_FAILURES.get(key, []) if now_ts - t < LOGIN_WINDOW_SECONDS]
    _LOGIN_FAILURES[key] = recent
    return len(recent) >= LOGIN_MAX_FAILURES

def _login_failure(key: str):
    _LOGIN_FAILURES.setdefault(key, []).append(time.time())

def _login_success(key: str):
    _LOGIN_FAILURES.pop(key, None)

WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor')
WORK_ROLES = ('admin','maintenance_manager','planner','supervisor','technician')
INV_ROLES = ('admin','maintenance_manager','planner','storekeeper','technician')
DOC_WRITE_ROLES = ('admin','asset_manager','maintenance_manager','planner','supervisor','technician','storekeeper','procurement','hse','project_manager')
PROC_ROLES = ('admin','maintenance_manager','procurement')
HSE_ROLES = ('admin','hse','maintenance_manager')
PROJECT_ROLES = ('admin','project_manager','maintenance_manager')


def rows(cur): return [dict(r) for r in cur.fetchall()]
def one(cur):
    r=cur.fetchone(); return dict(r) if r else None

def audit(conn, user_id:int, action:str, module:str, record_id:str, old='', new=''):
    if not isinstance(old,str): old=json.dumps(old,ensure_ascii=False,default=str,sort_keys=True)
    if not isinstance(new,str): new=json.dumps(new,ensure_ascii=False,default=str,sort_keys=True)
    created=now()
    prev=conn.execute("SELECT audit_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash=(prev['audit_hash'] if prev and prev['audit_hash'] else '')
    digest=audit_digest(prev_hash,user_id,action,module,record_id,old,new,created)
    conn.execute('INSERT INTO audit_logs(user_id,action,module,record_id,old_value,new_value,created_at,prev_hash,audit_hash) VALUES(?,?,?,?,?,?,?,?,?)',(user_id,action,module,record_id,old,new,created,prev_hash,digest))
    return digest

def verify_audit_chain(conn):
    # Delegates to the shared validator so the API, the replay endpoint and the
    # operational CLI can never drift apart on chain rules.
    return verify_audit_chain_report(conn)

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

def workflow_event(conn,module,record_type,record_id,record_code,event,from_status,to_status,actor_id,notes=''):
    conn.execute('INSERT INTO workflow_events(module,record_type,record_id,record_code,event,from_status,to_status,actor_id,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(module,record_type,record_id,record_code,event,from_status or '',to_status or '',actor_id,notes or '',now()))
    event_name='workflow.'+module.lower().replace(' ','_')+'.'+event.lower().replace(' ','_')
    emit_event(conn,event_name,record_type,record_code,{'record_id':record_id,'record_code':record_code,'from_status':from_status or '', 'to_status':to_status or '', 'actor_id':actor_id,'notes':notes or ''})

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

def next_no(conn, table, field, prefix, start=1):
    vals=[r[0] for r in conn.execute(f"SELECT {field} FROM {table} WHERE {field} LIKE ?",(prefix+'%',)).fetchall()]
    nums=[]
    for v in vals:
        try: nums.append(int(str(v).replace(prefix,'')))
        except: pass
    n=max(nums,default=start-1)+1
    return f'{prefix}{n}'

def get_or_404(conn, sql, args, message='Record not found'):
    r=conn.execute(sql,args).fetchone()
    if not r: raise HTTPException(404,message)
    return dict(r)

def user_id_by_username(conn, username):
    r=conn.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone(); return r['id'] if r else None

def _dt(value):
    if isinstance(value,datetime): return value
    return datetime.fromisoformat(str(value))

def emit_event(conn,event_type:str,aggregate_type:str,aggregate_id,payload:dict):
    event_no='EVT-'+uuid.uuid4().hex[:16].upper()
    cur=conn.execute("INSERT INTO event_outbox(event_no,event_type,aggregate_type,aggregate_id,payload_json,status,created_at) VALUES(?,?,?,?,?,'Pending',?)",(event_no,event_type,aggregate_type,str(aggregate_id),json.dumps(payload,ensure_ascii=False,default=str),now()))
    return {'id':cur.lastrowid,'event_no':event_no}

def _channel_site(conn, asset_id:int):
    r=conn.execute('SELECT s.id site_id,s.site_code,s.name site_name FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?',(asset_id,)).fetchone()
    return dict(r) if r else {'site_id':None,'site_code':None,'site_name':None}

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
        message=f"{channel['name']} {severity.lower()}: {value:g} {unit}".strip()
        if active:
            conn.execute('UPDATE operational_alarms SET severity=?,message=?,trigger_value=?,threshold_value=?,last_seen_at=?,occurrence_count=occurrence_count+1 WHERE id=?',(severity,message,value,threshold,captured_at,active['id']))
            return {'action':'updated','alarm_id':active['id'],'alarm_no':active['alarm_no'],'severity':severity}
        no=next_no(conn,'operational_alarms','alarm_no','ALM-',50001)
        cur=conn.execute("INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)",(no,channel['id'],channel['asset_id'],site.get('site_id'),severity,message,value,threshold,captured_at,captured_at))
        notify_once(conn,'Operational alarm',f"{no} â€” {message}",severity,None,'maintenance_manager','operations',no)
        notify_once(conn,'Operational alarm',f"{no} â€” {message}",severity,None,'asset_manager','operations',no)
        emit_event(conn,'operations.alarm.opened','alarm',no,{'alarm_no':no,'channel_code':channel['channel_code'],'asset_id':channel['asset_id'],'severity':severity,'value':value,'threshold':threshold,'captured_at':captured_at})
        if actor_id:audit(conn,actor_id,'ALARM OPEN','Utilities Operations',no,'',{'channel':channel['channel_code'],'severity':severity,'value':value,'threshold':threshold})
        return {'action':'opened','alarm_id':cur.lastrowid,'alarm_no':no,'severity':severity}
    if active:
        conn.execute("UPDATE operational_alarms SET status='Cleared',cleared_at=?,last_seen_at=?,trigger_value=? WHERE id=?",(captured_at,captured_at,value,active['id']))
        emit_event(conn,'operations.alarm.cleared','alarm',active['alarm_no'],{'alarm_no':active['alarm_no'],'channel_code':channel['channel_code'],'asset_id':channel['asset_id'],'value':value,'captured_at':captured_at})
        if actor_id:audit(conn,actor_id,'ALARM CLEAR','Utilities Operations',active['alarm_no'],active['status'],'Cleared')
        return {'action':'cleared','alarm_id':active['id'],'alarm_no':active['alarm_no'],'severity':active['severity']}
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
    return {'active_alarms':active_alarms,'critical_alarms':critical,'telemetry_channels':channels,'stale_channels_24h':stale}

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
                for uid in recipients: notify_once(conn,'SLA response breach',f"{w['wo_no']} â€” {w['title']} exceeded response target",'Critical',uid,None,'work',w['wo_no']+':sla-response')
                notify_once(conn,'SLA response breach',f"{w['wo_no']} exceeded response target",'Critical',None,'maintenance_manager','work',w['wo_no']+':sla-response')
                emit_event(conn,'sla.response_breached','work_order',w['wo_no'],{'work_order_id':w['id'],'due_at':w['response_due']})
            conn.execute("UPDATE work_order_sla SET response_status='Breached',escalated_level=CASE WHEN escalated_level<1 THEN 1 ELSE escalated_level END,updated_at=? WHERE work_order_id=?",(now(),w['id']))
        if w['resolution_status']=='Pending' and _dt(w['resolution_due'])<cutoff:
            cur=conn.execute("INSERT OR IGNORE INTO sla_events(work_order_id,event_type,level,message,created_at) VALUES(?,'Resolution Breach',1,?,?)",(w['id'],f"{w['wo_no']} exceeded resolution SLA",now()))
            if cur.rowcount:
                resolution_breaches+=1
                recipients={x for x in (w['assigned_to'],w['supervisor_id']) if x}
                for uid in recipients: notify_once(conn,'SLA resolution breach',f"{w['wo_no']} â€” {w['title']} exceeded resolution target",'Critical',uid,None,'work',w['wo_no']+':sla-resolution')
                notify_once(conn,'SLA resolution breach',f"{w['wo_no']} exceeded resolution target",'Critical',None,'maintenance_manager','work',w['wo_no']+':sla-resolution')
                emit_event(conn,'sla.resolution_breached','work_order',w['wo_no'],{'work_order_id':w['id'],'due_at':w['resolution_due']})
            conn.execute("UPDATE work_order_sla SET resolution_status='Breached',escalated_level=CASE WHEN escalated_level<1 THEN 1 ELSE escalated_level END,updated_at=? WHERE work_order_id=?",(now(),w['id']))
    return {'response_breaches':response_breaches,'resolution_breaches':resolution_breaches}

def _process_outbox(conn):
    items=rows(conn.execute("SELECT * FROM event_outbox WHERE status IN ('Pending','Failed') AND attempts<? ORDER BY id LIMIT 100",(OUTBOX_MAX_ATTEMPTS,)))
    delivered=failed=skipped=0
    for item in items:
        attempts=item['attempts']+1
        if not EVENT_WEBHOOK_URL:
            conn.execute("UPDATE event_outbox SET status='Skipped',attempts=?,processed_at=?,last_error='Webhook not configured' WHERE id=?",(attempts,now(),item['id']));skipped+=1;continue
        body=json.dumps({'event_no':item['event_no'],'event_type':item['event_type'],'aggregate_type':item['aggregate_type'],'aggregate_id':item['aggregate_id'],'payload':json.loads(item['payload_json']),'created_at':item['created_at']},ensure_ascii=False).encode()
        headers={'Content-Type':'application/json','User-Agent':'EUAS/'+APP_VERSION,'X-EUAS-Event':item['event_type'],'X-EUAS-Event-ID':item['event_no']}
        if EVENT_WEBHOOK_SECRET: headers['X-EUAS-Signature']='sha256='+hmac.new(EVENT_WEBHOOK_SECRET.encode(),body,hashlib.sha256).hexdigest()
        try:
            req=urllib_request.Request(EVENT_WEBHOOK_URL,data=body,headers=headers,method='POST')
            with urllib_request.urlopen(req,timeout=5) as resp:
                if not 200<=resp.status<300: raise RuntimeError(f'Webhook HTTP {resp.status}')
            conn.execute("UPDATE event_outbox SET status='Delivered',attempts=?,processed_at=?,last_error='' WHERE id=?",(attempts,now(),item['id']));delivered+=1
        except Exception as exc:
            conn.execute("UPDATE event_outbox SET status='Failed',attempts=?,last_error=? WHERE id=?",(attempts,str(exc)[:500],item['id']));failed+=1
    return {'delivered':delivered,'failed':failed,'skipped':skipped,'processed':delivered+failed+skipped}

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
        create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f'Approve {no} â€” {p["name"]}',actor_id,assigned_role='supervisor')
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
        cur=conn.execute('INSERT INTO purchase_requisitions(pr_no,title,requester_id,site_id,status,justification,total_estimate,created_at) VALUES(?,?,?,?,?,?,?,?)',(no,f"Auto-replenishment â€” {i['item_no']}",actor_id,i['site_id'],'Submitted','Automatically generated because available stock reached reorder point.',qty*i['unit_price'],now()))
        conn.execute('INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,i['id'],i['name'],qty,i['unit_price']))
        create_approval(conn,'Procurement','purchase_requisition',cur.lastrowid,no,f'Approve {no} â€” Auto-replenishment',actor_id,assigned_role='procurement')
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
                overdue_alerts += int(notify_once(conn,'Overdue work order',f"{w['wo_no']} â€” {w['title']} is overdue",'Warning',uid,None,'work',w['wo_no']))
        horizon=(target+timedelta(days=30)).isoformat()
        for a in rows(conn.execute("SELECT asset_no,name,warranty_expiry FROM assets WHERE warranty_expiry IS NOT NULL AND warranty_expiry>=? AND warranty_expiry<=?",(target.isoformat(),horizon))):
            warranty_alerts += int(notify_once(conn,'Asset warranty expiring',f"{a['asset_no']} â€” {a['name']} warranty expires {a['warranty_expiry']}",'Warning',None,'asset_manager','assets',a['asset_no']))
        for c in rows(conn.execute("SELECT contract_no,title,end_date FROM contracts WHERE status='Active' AND end_date IS NOT NULL AND end_date>=? AND end_date<=?",(target.isoformat(),horizon))):
            contract_alerts += int(notify_once(conn,'Contract expiring',f"{c['contract_no']} â€” {c['title']} expires {c['end_date']}",'Warning',None,'procurement','contracts',c['contract_no']))
        stale_cutoff=(datetime.combine(target,datetime.min.time())-timedelta(days=2)).isoformat(timespec='seconds')
        for ap in rows(conn.execute("SELECT * FROM approval_requests WHERE status='Pending' AND requested_at<?",(stale_cutoff,))):
            approval_alerts += int(notify_once(conn,'Approval overdue',f"{ap['record_code']} has been waiting for approval",'Warning',ap['assigned_user_id'],ap['assigned_role'],'approvals',ap['approval_no']))
        health_results=[_save_asset_health(conn,x['id'],actor_id) for x in rows(conn.execute('SELECT id FROM assets ORDER BY id'))]
        critical_health=0
        for h in health_results:
            if h['risk_band']=='Critical':
                critical_health+=1
                notify_once(conn,'Critical asset health',f"{h['asset_no']} â€” {h['name']} health score {h['score']}",'Critical',None,'asset_manager','assets',h['asset_no']+':health')
        expired=conn.execute('UPDATE approval_delegations SET active=0 WHERE active=1 AND end_at<?',(now(),)).rowcount
        outbox=_process_outbox(conn)
        health_avg=round(sum(x['score'] for x in health_results)/max(len(health_results),1),1)
        summary={'pm_work_orders':len(pm),'reorder_requisitions':len(reorders),'overdue_alerts':overdue_alerts,'warranty_alerts':warranty_alerts,'contract_alerts':contract_alerts,'approval_alerts':approval_alerts,'sla_response_breaches':sla['response_breaches'],'sla_resolution_breaches':sla['resolution_breaches'],'asset_health_average':health_avg,'critical_health_assets':critical_health,'delegations_expired':expired,'outbox_delivered':outbox['delivered'],'outbox_failed':outbox['failed'],'outbox_skipped':outbox['skipped']}
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
    title:str; description:str=''; asset_id:Optional[int]=None; location_id:Optional[int]=None; priority:str='Medium'; work_type:str='Corrective Maintenance'; failure_code:str=''
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
class ApprovalDecisionIn(BaseModel): decision:str; comments:str=''
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
    channel_code:str; value:float; captured_at:Optional[str]=None; quality:str='Good'; source:Optional[str]=None
class TelemetryIngestIn(BaseModel):
    readings:list[TelemetryReadingItem]=Field(min_length=1,max_length=500)
class AlarmWorkOrderIn(BaseModel):
    assigned_to:Optional[int]=None; supervisor_id:Optional[int]=None; target_finish:Optional[str]=None; notes:str=''
class DispatchIn(BaseModel):
    technician_user_id:int; eta_minutes:Optional[int]=Field(default=None,ge=0,le=1440); notes:str=''
class DispatchTransitionIn(BaseModel):
    action:str; notes:str=''

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
        token=secrets.token_urlsafe(36)
        conn.execute('INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',(token,r['id'],now(),(datetime.now()+timedelta(hours=SESSION_HOURS)).isoformat(timespec='seconds')))
        audit(conn,r['id'],'LOGIN','Authentication',r['username'],'','Successful login')
        return {'token':token,'user':{'id':r['id'],'username':r['username'],'full_name':r['full_name'],'email':r['email'],'department':r['department'],'phone':r['phone'],'role':r['role'],'role_name':r['role_name']}}

@app.post('/api/auth/logout')
def logout(authorization:Optional[str]=Header(None), user=Depends(current_user)):
    token=authorization.split(' ',1)[1]
    with db() as conn: conn.execute('DELETE FROM sessions WHERE token=?',(token,)); audit(conn,user['id'],'LOGOUT','Authentication',user['username'])
    return {'ok':True}
@app.get('/api/auth/me')
def me(user=Depends(current_user)): return user
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
# Approval queue/delegation API ownership lives in approval_store.py; the
# shared approval service helpers below (create_approval, resolve_approval,
# _delegation_active) remain the cross-domain write-side contract.

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
      ('assets','Asset Management','Enterprise asset registry & hierarchy','AS'),('work','Work Management','Plan, assign and execute work','WO'),('maintenance','Preventive Maintenance','Calendar, meter and condition plans','PM'),('workforce','Workforce Planning','Crafts, shifts, absences and capacity','WF'),('inventory','Inventory','Spares, warehouses and transactions','IN'),('procurement','Procurement','PR, approval, PO and receipt','PO'),('approvals','Approval Center','Unified operational approval queue','AP'),('operations','Utilities Operations','Electrical, water and infrastructure','OP'),('telemetry','Telemetry & Alarms','SCADA-style readings, thresholds and alarm response','TM'),('field','Field Service','Technician mobile workspace','FS'),('dispatch','Technician Dispatch','Dispatch board, ETA and field arrival','DP'),('map','GIS / Locations','Sites, assets, work and alerts','GI'),('inspections','Inspection Management','Digital inspection forms','IP'),('hse','Safety & HSE','Incidents, hazards and actions','HS'),('contracts','Contracts','Utility service and supply agreements','CT'),('vendors','Vendors','Supplier and OEM management','VN'),('projects','Projects','Budgets, progress and milestones','PJ'),('documents','Documents','Technical records and attachments','DC'),('analytics','Analytics','Reliability, cost and performance','AN'),('automation','Automation & Reports','Scheduled controls, exports, backups and observability','AU'),('administration','Administration','Users, RBAC and audit','AD')]
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
        return {'kpis':{'total_assets':asset_total,'operating_assets':operating,'assets_under_maintenance':maintenance,'critical_assets':critical,'open_work_orders':openwo,'emergency_work_orders':emergency,'overdue_work_orders':overdue,'completed_work_orders':completed,'pm_compliance':pm_compliance,'mttr':round(avg_repair,1),'mtbf':round(mtbf,1) if mtbf is not None else None,'inventory_value':round(inv_value,2),'low_stock_items':low,'pending_purchase_orders':po_pending,'active_technicians':forecast['technicians'],'safety_incidents':incidents,'open_outages':open_outages,'active_dispatches':active_dispatches,'active_alarms':active_alarms,'critical_alarms':critical_alarms,'maintenance_cost':round(sum(x['cost'] for x in costs),2),'utility_performance':round(100*operating/max(asset_total,1),1),'asset_health_score':portfolio_health,'forecast_demand_hours_90d':forecast['summary']['demand_hours'],'forecast_peak_utilization':forecast['summary']['peak_utilization_pct'],'parts_shortage_jobs_90d':forecast['summary']['parts_shortage_jobs']},'wo_by_status':statuses,'asset_health':health,'cost_by_asset':costs,'recent_activity':recent}

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
def create_telemetry_channel(body:TelemetryChannelIn,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner'))):
    with db() as conn:
        get_or_404(conn,'SELECT id,asset_no FROM assets WHERE id=?',(body.asset_id,),'Asset not found')
        code=(body.channel_code or next_no(conn,'telemetry_channels','channel_code','TEL-',1001)).strip().upper()
        if conn.execute('SELECT id FROM telemetry_channels WHERE channel_code=?',(code,)).fetchone():raise HTTPException(409,'Telemetry channel code already exists')
        cur=conn.execute('INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,source_system,warning_low,critical_low,warning_high,critical_high,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,body.asset_id,body.name,body.metric_type,body.unit,body.source_system,body.warning_low,body.critical_low,body.warning_high,body.critical_high,1 if body.active else 0,now(),now()))
        audit(conn,user['id'],'CREATE','Utilities Operations',code,'',body.model_dump());return {'id':cur.lastrowid,'channel_code':code}

@app.patch('/api/telemetry/channels/{channel_id}')
def update_telemetry_channel(channel_id:int,body:TelemetryChannelPatch,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner'))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    if 'active' in changes:changes['active']=1 if changes['active'] else 0
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM telemetry_channels WHERE id=?',(channel_id,),'Telemetry channel not found')
        if changes:
            conn.execute('UPDATE telemetry_channels SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),channel_id));audit(conn,user['id'],'UPDATE','Utilities Operations',old['channel_code'],old,changes)
        return {'ok':True}

@app.post('/api/telemetry/ingest')
def ingest_telemetry(body:TelemetryIngestIn,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    summary={'accepted':0,'alarms_opened':0,'alarms_updated':0,'alarms_cleared':0,'normal':0,'results':[]}
    with db() as conn:
        for reading in body.readings:
            c=get_or_404(conn,'SELECT * FROM telemetry_channels WHERE channel_code=? AND active=1',(reading.channel_code.strip().upper(),),f'Telemetry channel {reading.channel_code} not found or inactive')
            captured=reading.captured_at or now();source=reading.source or c['source_system'] or 'Manual'
            conn.execute('INSERT INTO telemetry_readings(channel_id,value,quality,source,captured_at,ingested_at,ingested_by) VALUES(?,?,?,?,?,?,?)',(c['id'],reading.value,reading.quality,source,captured,now(),user['id']))
            conn.execute('UPDATE telemetry_channels SET last_value=?,last_quality=?,last_reading_at=?,updated_at=? WHERE id=?',(reading.value,reading.quality,captured,now(),c['id']))
            c=dict(c);result=_evaluate_telemetry_alarm(conn,c,float(reading.value),captured,user['id']);summary['accepted']+=1;summary['results'].append({'channel_code':c['channel_code'],'value':reading.value,**result})
            key='alarms_'+result['action'] if result['action'] in ('opened','updated','cleared') else 'normal';summary[key]+=1
        emit_event(conn,'operations.telemetry.ingested','telemetry','batch',{'accepted':summary['accepted'],'alarms_opened':summary['alarms_opened'],'alarms_updated':summary['alarms_updated'],'alarms_cleared':summary['alarms_cleared']})
        audit(conn,user['id'],'INGEST TELEMETRY','Utilities Operations','batch','',{'accepted':summary['accepted'],'alarms_opened':summary['alarms_opened'],'alarms_cleared':summary['alarms_cleared']})
        return summary

@app.get('/api/telemetry/readings')
def telemetry_readings(channel_id:Optional[int]=None,asset_id:Optional[int]=None,hours:int=Query(24,ge=1,le=8760),limit:int=Query(500,ge=1,le=5000),user=Depends(current_user)):
    cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
    sql="SELECT tr.*,tc.channel_code,tc.name channel_name,tc.metric_type,tc.unit,a.asset_no,a.name asset_name FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id JOIN assets a ON a.id=tc.asset_id WHERE tr.captured_at>=?";args=[cutoff]
    if channel_id is not None:sql+=' AND tc.id=?';args.append(channel_id)
    if asset_id is not None:sql+=' AND a.id=?';args.append(asset_id)
    sql+=' ORDER BY tr.captured_at DESC LIMIT ?';args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.get('/api/alarms')
def alarms(status:str='',severity:str='',asset_id:Optional[int]=None,site_id:Optional[int]=None,limit:int=Query(200,ge=1,le=1000),user=Depends(current_user)):
    sql="SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.name asset_name,s.site_code,s.name site_name,w.wo_no,ack.full_name acknowledged_by_name,cl.full_name closed_by_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id LEFT JOIN sites s ON s.id=oa.site_id LEFT JOIN work_orders w ON w.id=oa.work_order_id LEFT JOIN users ack ON ack.id=oa.acknowledged_by LEFT JOIN users cl ON cl.id=oa.closed_by WHERE 1=1";args=[]
    if status:sql+=' AND oa.status=?';args.append(status)
    if severity:sql+=' AND oa.severity=?';args.append(severity)
    if asset_id is not None:sql+=' AND oa.asset_id=?';args.append(asset_id)
    if site_id is not None:sql+=' AND oa.site_id=?';args.append(site_id)
    sql+=" ORDER BY CASE oa.severity WHEN 'Critical' THEN 2 ELSE 1 END DESC,oa.id DESC LIMIT ?";args.append(limit)
    with db() as conn:return rows(conn.execute(sql,args))

@app.post('/api/alarms/{alarm_id}/acknowledge')
def acknowledge_alarm(alarm_id:int,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor','technician'))):
    with db() as conn:
        a=get_or_404(conn,'SELECT * FROM operational_alarms WHERE id=?',(alarm_id,),'Alarm not found')
        if a['status'] not in ('Open','Acknowledged'):raise HTTPException(409,f"Alarm is {a['status']}")
        conn.execute("UPDATE operational_alarms SET status='Acknowledged',acknowledged_at=?,acknowledged_by=? WHERE id=?",(now(),user['id'],alarm_id));audit(conn,user['id'],'ACKNOWLEDGE ALARM','Utilities Operations',a['alarm_no'],a['status'],'Acknowledged');return {'ok':True,'status':'Acknowledged'}

@app.post('/api/alarms/{alarm_id}/close')
def close_alarm(alarm_id:int,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        a=get_or_404(conn,'SELECT * FROM operational_alarms WHERE id=?',(alarm_id,),'Alarm not found')
        if a['status']=='Closed':return {'ok':True,'status':'Closed'}
        conn.execute("UPDATE operational_alarms SET status='Closed',closed_at=?,closed_by=? WHERE id=?",(now(),user['id'],alarm_id));emit_event(conn,'operations.alarm.closed','alarm',a['alarm_no'],{'alarm_no':a['alarm_no'],'asset_id':a['asset_id']});audit(conn,user['id'],'CLOSE ALARM','Utilities Operations',a['alarm_no'],a['status'],'Closed');return {'ok':True,'status':'Closed'}

@app.post('/api/alarms/{alarm_id}/work-order')
def alarm_create_work_order(alarm_id:int,body:AlarmWorkOrderIn,user=Depends(require_roles('admin','asset_manager','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        alarm=get_or_404(conn,'SELECT oa.*,tc.channel_code,tc.name channel_name,tc.unit,a.asset_no,a.location_id FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id WHERE oa.id=?',(alarm_id,),'Alarm not found')
        if alarm.get('work_order_id'):
            w=conn.execute('SELECT id,wo_no FROM work_orders WHERE id=?',(alarm['work_order_id'],)).fetchone();return {'id':w['id'],'wo_no':w['wo_no'],'existing':True}
        no=next_no(conn,'work_orders','wo_no','WO-',10026);priority='Critical' if alarm['severity']=='Critical' else 'High';finish=body.target_finish or (date.today()+timedelta(days=1 if priority=='Critical' else 2)).isoformat();title=f"Investigate {alarm['channel_name']} alarm";desc=f"Generated from {alarm['alarm_no']} on {alarm['asset_no']}. {alarm['message']}"
        cur=conn.execute("INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,instructions,created_at,updated_at) VALUES(?,?,?,?,?,?,'Submitted','Corrective Maintenance',?,?,?,?,?,?,?,?,?,?)",(no,title,desc,alarm['asset_id'],alarm['location_id'],priority,f"ALARM-{alarm['channel_code']}",user['id'],body.assigned_to,body.supervisor_id,date.today().isoformat(),finish,2,body.notes or f"Validate {alarm['channel_name']} reading and investigate root cause.",now(),now()))
        conn.execute('UPDATE operational_alarms SET work_order_id=? WHERE id=?',(cur.lastrowid,alarm_id));_ensure_work_sla(conn,cur.lastrowid);create_approval(conn,'Work Management','work_order',cur.lastrowid,no,f"Approve {no} â€” {title}",user['id'],assigned_user_id=body.supervisor_id,assigned_role=None if body.supervisor_id else 'maintenance_manager');workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'ALARM GENERATED','', 'Submitted',user['id'],alarm['alarm_no']);emit_event(conn,'operations.alarm.work_order_created','alarm',alarm['alarm_no'],{'alarm_no':alarm['alarm_no'],'work_order':no});audit(conn,user['id'],'CREATE WORK FROM ALARM','Utilities Operations',alarm['alarm_no'],'',no);return {'id':cur.lastrowid,'wo_no':no,'existing':False}

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

# ---------- assets ----------
ASSET_SELECT='''SELECT a.*,at.name asset_type,at.utility_domain,l.name location_name,l.location_code,s.id site_id,s.name site_name,p.asset_no parent_asset_no,p.name parent_asset_name,u.full_name responsible_person,v.name vendor_name FROM assets a LEFT JOIN asset_types at ON at.id=a.asset_type_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id LEFT JOIN assets p ON p.id=a.parent_asset_id LEFT JOIN users u ON u.id=a.responsible_user_id LEFT JOIN vendors v ON v.id=a.vendor_id'''
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
    html=render_snapshot_report_html(r,d)
    return HTMLResponse(html)

# Asset registry API ownership lives in asset_store.py; ASSET_SELECT and the
# shared health engine remain here for cross-domain consumers.
@app.post('/api/meters/{meter_id}/readings')
def add_meter_reading(meter_id:int,body:MeterReadingIn,user=Depends(require_roles(*WORK_ROLES))):
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
def create_work(body:WorkOrderIn,user=Depends(require_roles(*WRITE_ROLES))):
    with db() as conn:
        no=next_no(conn,'work_orders','wo_no','WO-',10026);loc=body.location_id
        if body.asset_id and not loc:
            r=conn.execute('SELECT location_id FROM assets WHERE id=?',(body.asset_id,)).fetchone();loc=r['location_id'] if r else None
        cur=conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,requested_by,assigned_to,supervisor_id,target_start,target_finish,estimated_hours,safety_requirements,instructions,checklist,estimated_cost,created_at,updated_at) VALUES(?,?,?,?,?,?, 'Draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,body.title,body.description,body.asset_id,loc,body.priority,body.work_type,body.failure_code,user['id'],body.assigned_to,body.supervisor_id,body.target_start,body.target_finish,body.estimated_hours,body.safety_requirements,body.instructions,body.checklist,body.estimated_cost,now(),now()))
        checklist=[x.strip() for x in body.checklist.replace('\n',';').replace(',',';').split(';') if x.strip()]
        for seq,task in enumerate(checklist,1): conn.execute("INSERT INTO work_order_tasks(work_order_id,sequence_no,task,status) VALUES(?,?,?,'Pending')",(cur.lastrowid,seq,task))
        _ensure_work_sla(conn,cur.lastrowid)
        workflow_event(conn,'Work Management','work_order',cur.lastrowid,no,'CREATE','', 'Draft',user['id'])
        if body.assigned_to: notify(conn,'Work order assigned',f'{no} â€” {body.title}','High' if body.priority in ('High','Critical','Emergency') else 'Info',body.assigned_to,None,'work',no)
        audit(conn,user['id'],'CREATE','Work Management',no,'',body.model_dump());return {'id':cur.lastrowid,'wo_no':no}
@app.patch('/api/work-orders/{wo_id}')
def update_work(wo_id:int,body:WorkOrderPatch,user=Depends(require_roles(*WRITE_ROLES))):
    changes={k:v for k,v in body.model_dump().items() if v is not None}
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        if changes:
            conn.execute('UPDATE work_orders SET '+','.join(f'{k}=?' for k in changes)+',updated_at=? WHERE id=?',(*changes.values(),now(),wo_id));audit(conn,user['id'],'UPDATE','Work Management',old['wo_no'],old,changes)
            if 'priority' in changes:_ensure_work_sla(conn,wo_id,force=True)
            if 'assigned_to' in changes and changes['assigned_to']:notify(conn,'Work order assigned',f"{old['wo_no']} â€” {changes.get('title',old['title'])}",'Info',changes['assigned_to'],None,'work',old['wo_no'])
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
def transition_work(wo_id:int,body:TransitionIn,user=Depends(require_roles(*WORK_ROLES))):
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
            create_approval(conn,'Work Management','work_order',wo_id,w['wo_no'],f"Approve {w['wo_no']} â€” {w['title']}",user['id'],assigned_user_id=w['supervisor_id'],assigned_role=None if w['supervisor_id'] else 'maintenance_manager')
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
def add_work_requirement(wo_id:int,body:WorkRequirementIn,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','storekeeper'))):
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
def delete_work_requirement(wo_id:int,requirement_id:int,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');r=get_or_404(conn,'SELECT * FROM work_order_requirements WHERE id=? AND work_order_id=?',(requirement_id,wo_id),'Requirement not found')
        conn.execute('DELETE FROM work_order_requirements WHERE id=?',(requirement_id,));audit(conn,user['id'],'REMOVE MATERIAL PLAN','Work Management',w['wo_no'],r,'');return {'ok':True}

@app.get('/api/work-orders/{wo_id}/reservations')
def list_work_reservations(wo_id:int,user=Depends(current_user)):
    with db() as conn:
        get_or_404(conn,'SELECT id FROM work_orders WHERE id=?',(wo_id,),'Work order not found');return _reservation_rows(conn,wo_id)

@app.post('/api/work-orders/{wo_id}/reservations')
def reserve_work_material(wo_id:int,body:ReservationIn,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','storekeeper'))):
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
def reserve_all_work_materials(wo_id:int,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','storekeeper'))):
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
def release_reservation(reservation_id:int,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor','storekeeper'))):
    with db() as conn:
        r=get_or_404(conn,'SELECT r.*,w.wo_no,i.item_no FROM inventory_reservations r JOIN work_orders w ON w.id=r.work_order_id JOIN inventory_items i ON i.id=r.inventory_item_id WHERE r.id=?',(reservation_id,),'Reservation not found')
        if r['status'] not in ('Reserved','Partially Issued'):raise HTTPException(409,f"Reservation is {r['status']}")
        conn.execute("UPDATE inventory_reservations SET status='Released',released_at=? WHERE id=?",(now(),reservation_id));_sync_reserved_stock(conn,r['inventory_item_id'])
        req=conn.execute('SELECT id FROM work_order_requirements WHERE work_order_id=? AND inventory_item_id=?',(r['work_order_id'],r['inventory_item_id'])).fetchone()
        if req:conn.execute("UPDATE work_order_requirements SET status='Required' WHERE id=?",(req['id'],))
        audit(conn,user['id'],'RELEASE RESERVATION','Inventory',r['reservation_no'],r['status'],'Released');return {'ok':True,'readiness':_work_order_parts_readiness(conn,r['work_order_id'])}

@app.post('/api/reservations/{reservation_id}/issue')
def issue_reservation(reservation_id:int,body:ReservationIssueIn,user=Depends(require_roles(*INV_ROLES))):
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
def add_work_craft_requirement(wo_id:int,body:CraftRequirementIn,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');c=get_or_404(conn,'SELECT craft_code FROM crafts WHERE id=? AND active=1',(body.craft_id,),'Craft not found')
        existing=conn.execute('SELECT id FROM work_order_craft_requirements WHERE work_order_id=? AND craft_id=?',(wo_id,body.craft_id)).fetchone()
        if existing:conn.execute('UPDATE work_order_craft_requirements SET planned_hours=? WHERE id=?',(body.planned_hours,existing['id']));rid=existing['id']
        else:
            cur=conn.execute('INSERT INTO work_order_craft_requirements(work_order_id,craft_id,planned_hours) VALUES(?,?,?)',(wo_id,body.craft_id,body.planned_hours));rid=cur.lastrowid
        audit(conn,user['id'],'PLAN CRAFT','Work Management',w['wo_no'],'',{'craft':c['craft_code'],'planned_hours':body.planned_hours});return {'id':rid}

@app.post('/api/work-orders/{wo_id}/labor')
def add_labor(wo_id:int,body:LaborIn,user=Depends(require_roles(*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found');uid=body.user_id or user['id'];work_date=body.work_date or date.today().isoformat();cost=body.hours*body.labor_rate
        conn.execute('INSERT INTO labor_entries(work_order_id,user_id,hours,labor_rate,notes,work_date) VALUES(?,?,?,?,?,?)',(wo_id,uid,body.hours,body.labor_rate,body.notes,work_date));conn.execute('UPDATE work_orders SET actual_hours=actual_hours+?,actual_cost=actual_cost+?,updated_at=? WHERE id=?',(body.hours,cost,now(),wo_id));post_cost(conn,w,'Labor',cost,body.hours,f'{body.hours:g} h Ã— {body.labor_rate:g}',user['id']);audit(conn,user['id'],'ADD LABOR','Work Management',w['wo_no'],'',{'hours':body.hours,'user_id':uid,'cost':cost});return {'ok':True}
@app.post('/api/work-orders/{wo_id}/materials')
def add_material(wo_id:int,body:MaterialIn,user=Depends(require_roles(*INV_ROLES))):
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
        if new_stock-float(fresh['reserved_stock'])<=float(fresh['reorder_point']):notify(conn,'Inventory below reorder point',f"{i['item_no']} â€” {i['name']} has {new_stock:g} {i['unit']} remaining",'Warning',None,'storekeeper','inventory',i['item_no'])
        return {'ok':True,'stock':new_stock,'cost':cost,'readiness':_work_order_parts_readiness(conn,wo_id)}
@app.post('/api/work-orders/{wo_id}/notes')
def add_work_note(wo_id:int,body:NoteIn,user=Depends(require_roles(*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        entry=f"[{now()}] {user['full_name']}: {body.note}"; new=(w['comments']+'\n'+entry).strip()
        conn.execute('UPDATE work_orders SET comments=?,updated_at=? WHERE id=?',(new,now(),wo_id));audit(conn,user['id'],'ADD NOTE','Work Management',w['wo_no'],w['comments'],new);return {'ok':True}
@app.post('/api/work-orders/{wo_id}/tasks/{task_id}/toggle')
def toggle_work_task(wo_id:int,task_id:int,user=Depends(require_roles(*WORK_ROLES))):
    with db() as conn:
        w=get_or_404(conn,'SELECT wo_no FROM work_orders WHERE id=?',(wo_id,),'Work order not found');t=get_or_404(conn,'SELECT * FROM work_order_tasks WHERE id=? AND work_order_id=?',(task_id,wo_id),'Task not found');new='Pending' if t['status']=='Completed' else 'Completed';conn.execute('UPDATE work_order_tasks SET status=?,completed_at=? WHERE id=?',(new,now() if new=='Completed' else None,task_id));audit(conn,user['id'],'TASK '+new.upper(),'Work Management',w['wo_no'],t['status'],new);return {'ok':True,'status':new}
@app.post('/api/field/assets/{asset_id}/condition-meter')
def field_asset_update(asset_id:int,body:FieldAssetUpdate,user=Depends(require_roles(*WORK_ROLES))):
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
    html=render_work_order_report_html(w,labor,mats)
    return HTMLResponse(html)

# Maintenance-plan API ownership lives in pm_store.py (installed with the
# production composition); the shared due-work generator remains the domain
# service used by automation and this route family.

# ---------- inventory ----------
@app.get('/api/inventory')
def list_inventory(q:str='',user=Depends(current_user)):
    sql='''SELECT i.*,w.name warehouse_name,w.warehouse_code,v.name vendor_name,(i.current_stock-i.reserved_stock) available_stock,CASE WHEN i.current_stock<=0 THEN 'Out of Stock' WHEN i.current_stock-i.reserved_stock<=i.reorder_point THEN 'Low Stock' WHEN i.max_level>0 AND i.current_stock>i.max_level THEN 'Overstock' ELSE 'Normal' END stock_status FROM inventory_items i JOIN warehouses w ON w.id=i.warehouse_id LEFT JOIN vendors v ON v.id=i.vendor_id WHERE 1=1''';args=[]
    if q:sql+=' AND (i.item_no LIKE ? OR i.name LIKE ? OR i.category LIKE ?)';like=f'%{q}%';args+=[like]*3
    sql+=' ORDER BY i.item_no'
    with db() as conn:return rows(conn.execute(sql,args))
@app.post('/api/inventory')
def create_inventory(body:InventoryIn,user=Depends(require_roles('admin','storekeeper','maintenance_manager'))):
    with db() as conn:
        no=next_no(conn,'inventory_items','item_no','ITM-',1000);vals=body.model_dump();cols=list(vals);cur=conn.execute(f"INSERT INTO inventory_items(item_no,{','.join(cols)}) VALUES(?,{','.join('?'*len(cols))})",(no,*vals.values()));audit(conn,user['id'],'CREATE','Inventory',no,'',vals);return {'id':cur.lastrowid,'item_no':no}
@app.post('/api/inventory/{item_id}/transaction')
def inventory_tx(item_id:int,body:InventoryTxIn,user=Depends(require_roles(*INV_ROLES))):
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
        if new-i['reserved_stock']<=i['reorder_point']:notify(conn,'Inventory below reorder point',f"{i['item_no']} â€” {i['name']} is below reorder point",'Warning',None,'storekeeper','inventory',i['item_no'])
        return {'ok':True,'current_stock':new}
@app.get('/api/inventory/{item_id}/transactions')
def inventory_history(item_id:int,user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT t.*,u.full_name,w.wo_no FROM inventory_transactions t JOIN users u ON u.id=t.user_id LEFT JOIN work_orders w ON w.id=t.work_order_id WHERE t.item_id=? ORDER BY t.id DESC''',(item_id,)))
@app.post('/api/inventory/reorder-scan')
def reorder_scan(user=Depends(require_roles('admin','storekeeper','maintenance_manager','procurement'))):
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
def create_pr(body:PRIn,user=Depends(require_roles('admin','storekeeper','maintenance_manager','procurement','planner'))):
    with db() as conn:
        no=next_no(conn,'purchase_requisitions','pr_no','PR-',8001);total=sum(float(x.get('quantity',0))*float(x.get('estimated_unit_cost',0)) for x in body.items);cur=conn.execute('INSERT INTO purchase_requisitions(pr_no,title,requester_id,site_id,work_order_id,project_id,status,justification,total_estimate,created_at) VALUES(?,?,?,?,?,?,\'Draft\',?,?,?)',(no,body.title,user['id'],body.site_id,body.work_order_id,body.project_id,body.justification,total,now()))
        for x in body.items:conn.execute('INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,x.get('inventory_item_id'),x.get('description','Item'),x.get('quantity',1),x.get('estimated_unit_cost',0)))
        audit(conn,user['id'],'CREATE','Procurement',no,'',body.model_dump());return {'id':cur.lastrowid,'pr_no':no}
@app.post('/api/procurement/requisitions/{pr_id}/submit')
def submit_pr(pr_id:int,user=Depends(require_roles('admin','storekeeper','maintenance_manager','procurement','planner'))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(pr_id,),'PR not found')
        if pr['status'] not in ('Draft','Rejected'): raise HTTPException(409,'Only Draft or Rejected requisitions can be submitted')
        conn.execute("UPDATE purchase_requisitions SET status='Submitted' WHERE id=?",(pr_id,))
        create_approval(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],f"Approve {pr['pr_no']} â€” {pr['title']}",user['id'],assigned_role='procurement')
        workflow_event(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],'SUBMIT',pr['status'],'Submitted',user['id'])
        audit(conn,user['id'],'SUBMIT','Procurement',pr['pr_no'],pr['status'],'Submitted');return {'ok':True,'status':'Submitted'}

@app.post('/api/procurement/requisitions/{pr_id}/approve')
def approve_pr(pr_id:int,user=Depends(require_roles(*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(pr_id,),'PR not found')
        if pr['status']!='Submitted': raise HTTPException(409,'Purchase requisition must be Submitted before approval')
        conn.execute("UPDATE purchase_requisitions SET status='Approved',approved_at=? WHERE id=?",(now(),pr_id));resolve_approval(conn,'Procurement','purchase_requisition',pr_id,'approve',user['id']);workflow_event(conn,'Procurement','purchase_requisition',pr_id,pr['pr_no'],'APPROVE',pr['status'],'Approved',user['id']);audit(conn,user['id'],'APPROVE','Procurement',pr['pr_no'],pr['status'],'Approved');return {'ok':True}
@app.post('/api/procurement/quotations')
def create_quote(body:QuoteIn,user=Depends(require_roles(*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT pr_no FROM purchase_requisitions WHERE id=?',(body.pr_id,),'PR not found');get_or_404(conn,'SELECT id FROM vendors WHERE id=?',(body.vendor_id,),'Vendor not found');no=next_no(conn,'quotations','quote_no','RFQ-',8101);cur=conn.execute("INSERT INTO quotations(quote_no,pr_id,vendor_id,amount,valid_until,status) VALUES(?,?,?,?,?,'Received')",(no,body.pr_id,body.vendor_id,body.amount,body.valid_until));audit(conn,user['id'],'ADD QUOTE','Procurement',no,'',{'pr':pr['pr_no'],'amount':body.amount});return {'id':cur.lastrowid,'quote_no':no}

@app.post('/api/procurement/purchase-orders')
def create_po(body:POIn,user=Depends(require_roles(*PROC_ROLES))):
    with db() as conn:
        pr=get_or_404(conn,'SELECT * FROM purchase_requisitions WHERE id=?',(body.pr_id,),'PR not found')
        if pr['status']!='Approved':raise HTTPException(409,'Purchase requisition must be approved first')
        no=next_no(conn,'purchase_orders','po_no','PO-',9001);cur=conn.execute('INSERT INTO purchase_orders(po_no,pr_id,vendor_id,status,order_date,expected_delivery,total_cost,work_order_id,project_id) VALUES(?,?,?,\'Ordered\',?,?,?,?,?)',(no,body.pr_id,body.vendor_id,date.today().isoformat(),body.expected_delivery,pr['total_estimate'],pr['work_order_id'],pr['project_id']));conn.execute("UPDATE purchase_requisitions SET status='Ordered' WHERE id=?",(body.pr_id,));items=rows(conn.execute('SELECT * FROM purchase_requisition_items WHERE pr_id=?',(body.pr_id,)))
        for x in items:conn.execute('INSERT INTO purchase_order_items(po_id,inventory_item_id,description,quantity,unit_cost) VALUES(?,?,?,?,?)',(cur.lastrowid,x['inventory_item_id'],x['description'],x['quantity'],x['estimated_unit_cost']))
        audit(conn,user['id'],'CREATE PO','Procurement',no,'',{'pr':pr['pr_no']});return {'id':cur.lastrowid,'po_no':no}
@app.post('/api/procurement/purchase-orders/{po_id}/receive')
def receive_po(po_id:int,user=Depends(require_roles('admin','procurement','storekeeper'))):
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
# Outage close ownership lives in outage_store.py (terminal transition claim);
# the read model and outage creation remain here.
@app.get('/api/outages')
def list_outages(status:str='',site_id:Optional[int]=None,asset_id:Optional[int]=None,limit:int=Query(200,ge=1,le=1000),offset:int=Query(0,ge=0),user=Depends(current_user)):
    sql="""SELECT o.*,a.asset_no,a.name asset_name,s.site_code,s.name site_name,w.wo_no,u.full_name reported_by_name
      FROM asset_outages o JOIN assets a ON a.id=o.asset_id LEFT JOIN sites s ON s.id=o.site_id LEFT JOIN work_orders w ON w.id=o.work_order_id JOIN users u ON u.id=o.reported_by WHERE 1=1""";args=[]
    if status:sql+=' AND o.status=?';args.append(status)
    if site_id:sql+=' AND o.site_id=?';args.append(site_id)
    if asset_id:sql+=' AND o.asset_id=?';args.append(asset_id)
    sql+=' ORDER BY o.start_at DESC,o.id DESC LIMIT ? OFFSET ?';args+=[limit,offset]
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
        notify(conn,'Asset outage opened',f'{no} â€” {a["asset_no"]} is unavailable','High' if body.outage_type=='Forced' else 'Warning',None,'maintenance_manager','operations',no)
        return {'id':cur.lastrowid,'outage_no':no,'status':'Open'}

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
def dispatch_work(wo_id:int,body:DispatchIn,user=Depends(require_roles('admin','maintenance_manager','planner','supervisor'))):
    with db() as conn:
        w=get_or_404(conn,'SELECT * FROM work_orders WHERE id=?',(wo_id,),'Work order not found')
        if w['status'] not in ('Approved','Assigned'):raise HTTPException(409,'Work order must be Approved or Assigned before dispatch')
        tech=get_or_404(conn,"SELECT u.id,u.full_name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.active=1 AND r.code='technician'",(body.technician_user_id,),'Active technician not found')
        busy=conn.execute("SELECT d.dispatch_no,w.wo_no FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id WHERE d.technician_user_id=? AND d.work_order_id<>? AND d.status IN ('Dispatched','Accepted','En Route','On Site') ORDER BY d.id DESC LIMIT 1",(body.technician_user_id,wo_id)).fetchone()
        if busy:raise HTTPException(409,f"Technician already has active dispatch {busy['dispatch_no']} for {busy['wo_no']}")
        conn.execute("UPDATE dispatch_assignments SET status='Cancelled',cancelled_at=? WHERE work_order_id=? AND status IN ('Dispatched','Accepted','En Route','On Site')",(now(),wo_id))
        no=next_no(conn,'dispatch_assignments','dispatch_no','DSP-',40001);cur=conn.execute("INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,dispatched_by,status,eta_minutes,notes,dispatched_at) VALUES(?,?,?,?, 'Dispatched',?,?,?)",(no,wo_id,body.technician_user_id,user['id'],body.eta_minutes,body.notes,now()))
        old=w['status'];conn.execute("UPDATE work_orders SET assigned_to=?,status='Assigned',updated_at=? WHERE id=?",(body.technician_user_id,now(),wo_id))
        workflow_event(conn,'Work Management','work_order',wo_id,w['wo_no'],'DISPATCH',old,'Assigned',user['id'],f'{no} â†’ {tech["full_name"]}');audit(conn,user['id'],'DISPATCH','Field Service',no,'',{'work_order':w['wo_no'],'technician':tech['full_name'],'eta_minutes':body.eta_minutes})
        notify(conn,'Dispatch assigned',f'{no} â€” {w["wo_no"]}: {w["title"]}','High' if w['priority'] in ('Emergency','Critical','High') else 'Info',body.technician_user_id,None,'dispatch',no)
        return {'id':cur.lastrowid,'dispatch_no':no,'status':'Dispatched'}

@app.post('/api/dispatch/{dispatch_id}/transition')
def transition_dispatch(dispatch_id:int,body:DispatchTransitionIn,user=Depends(require_roles(*WORK_ROLES))):
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
def create_inspection(body:InspectionIn,user=Depends(require_roles(*WORK_ROLES))):
    items=body.items or ['Visual Condition','Leaks','Temperature','Noise','Grounding','Physical Damage']
    with db() as conn:
        no=next_no(conn,'inspections','inspection_no','INS-',5001);cur=conn.execute('INSERT INTO inspections(inspection_no,template_name,asset_id,work_order_id,inspector_id,status,created_at) VALUES(?,?,?,?,?,\'Draft\',?)',(no,body.template_name,body.asset_id,body.work_order_id,user['id'],now()))
        for item in items:
            conn.execute('INSERT INTO inspection_items(inspection_id,item_name) VALUES(?,?)',(cur.lastrowid,item))
        audit(conn,user['id'],'CREATE','Inspections',no,'',body.model_dump())
        return {'id':cur.lastrowid,'inspection_no':no}
@app.post('/api/inspections/{inspection_id}/submit')
def submit_inspection(inspection_id:int,body:InspectionSubmit,user=Depends(require_roles(*WORK_ROLES))):
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
def create_hse(body:HSEIn,user=Depends(require_roles(*HSE_ROLES))):
    with db() as conn:
        no=next_no(conn,'safety_incidents','incident_no','HSE-',7001);risk=body.severity*body.probability;cur=conn.execute('''INSERT INTO safety_incidents(incident_no,incident_type,title,site_id,location_id,asset_id,reported_by,severity,probability,risk_score,status,description,corrective_action,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'Open',?,?,?,?)''',(no,body.incident_type,body.title,body.site_id,body.location_id,body.asset_id,user['id'],body.severity,body.probability,risk,body.description,body.corrective_action,body.occurred_at or now(),now()));audit(conn,user['id'],'CREATE','HSE',no,'',body.model_dump());
        if risk>=12:notify(conn,'High HSE risk',f'{no} has risk score {risk}','Critical',None,'maintenance_manager','hse',no)
        return {'id':cur.lastrowid,'incident_no':no,'risk_score':risk}
@app.patch('/api/hse/{incident_id}')
def update_hse(incident_id:int,body:HSEPatch,user=Depends(require_roles(*HSE_ROLES))):
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
def create_project(body:ProjectIn,user=Depends(require_roles(*PROJECT_ROLES))):
    with db() as conn:
        no=next_no(conn,'projects','project_no','PRJ-',3001);cur=conn.execute('INSERT INTO projects(project_no,name,manager_id,site_id,start_date,finish_date,budget,actual_cost,progress,status) VALUES(?,?,?,?,?,?,?,0,0,?)',(no,body.name,body.manager_id,body.site_id,body.start_date,body.finish_date,body.budget,body.status));audit(conn,user['id'],'CREATE','Projects',no,'',body.model_dump());return {'id':cur.lastrowid,'project_no':no}
@app.post('/api/projects/{project_id}/tasks')
def create_project_task(project_id:int,body:ProjectTaskIn,user=Depends(require_roles(*PROJECT_ROLES))):
    with db() as conn:
        project=get_or_404(conn,'SELECT * FROM projects WHERE id=?',(project_id,),'Project not found')
        cur=conn.execute('INSERT INTO project_tasks(project_id,task_name,owner_id,due_date,status,progress) VALUES(?,?,?,?,?,?)',(project_id,body.task_name,body.owner_id,body.due_date,body.status,body.progress))
        _recalculate_project_progress(conn,project_id)
        audit(conn,user['id'],'ADD TASK','Projects',project['project_no'],'',body.model_dump())
        return {'id':cur.lastrowid}
@app.patch('/api/projects/{project_id}/tasks/{task_id}')
def update_project_task(project_id:int,task_id:int,body:ProjectTaskPatch,user=Depends(require_roles(*PROJECT_ROLES))):
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
def create_vendor(body:VendorIn,user=Depends(require_roles('admin','procurement','maintenance_manager'))):
    with db() as conn:
        code=body.vendor_code or next_no(conn,'vendors','vendor_code','VND-',100);cur=conn.execute('INSERT INTO vendors(vendor_code,name,category,contact_person,email,phone,status) VALUES(?,?,?,?,?,?,?)',(code,body.name,body.category,body.contact_person,body.email,body.phone,body.status));audit(conn,user['id'],'CREATE','Vendors',code,'',body.model_dump());return {'id':cur.lastrowid,'vendor_code':code}
@app.get('/api/contracts')
def contracts(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT c.*,v.name vendor_name FROM contracts c LEFT JOIN vendors v ON v.id=c.vendor_id ORDER BY c.id DESC'))
@app.post('/api/contracts')
def create_contract(body:ContractIn,user=Depends(require_roles('admin','procurement','maintenance_manager'))):
    with db() as conn:
        no=body.contract_no or next_no(conn,'contracts','contract_no','CTR-',4001);cur=conn.execute('INSERT INTO contracts(contract_no,title,vendor_id,start_date,end_date,value,status) VALUES(?,?,?,?,?,?,?)',(no,body.title,body.vendor_id,body.start_date,body.end_date,body.value,body.status));audit(conn,user['id'],'CREATE','Contracts',no,'',body.model_dump());return {'id':cur.lastrowid,'contract_no':no}

# ---------- documents ----------
@app.get('/api/documents')
def documents(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('''SELECT d.*,a.asset_no,w.wo_no,l.location_code,p.project_no,v.vendor_code,u.full_name uploaded_by_name FROM documents d LEFT JOIN assets a ON a.id=d.asset_id LEFT JOIN work_orders w ON w.id=d.work_order_id LEFT JOIN locations l ON l.id=d.location_id LEFT JOIN projects p ON p.id=d.project_id LEFT JOIN vendors v ON v.id=d.vendor_id JOIN users u ON u.id=d.uploaded_by ORDER BY d.id DESC'''))
@app.post('/api/documents/upload')
def upload_document(title:str=Form(...),category:str=Form(...),asset_id:Optional[int]=Form(None),work_order_id:Optional[int]=Form(None),location_id:Optional[int]=Form(None),project_id:Optional[int]=Form(None),vendor_id:Optional[int]=Form(None),file:UploadFile=File(...),user=Depends(require_roles(*DOC_WRITE_ROLES))):
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
def update_sla_policy(policy_id:int,body:SLAPolicyPatch,user=Depends(require_roles('admin','maintenance_manager'))):
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
    with db() as conn:
        result=rows(conn.execute(sql,args))
        for r in result:r['max_attempts']=OUTBOX_MAX_ATTEMPTS
        return result

@app.post('/api/events/outbox/{event_id}/retry')
def retry_outbox_event(event_id:int,user=Depends(require_roles('admin','maintenance_manager'))):
    with db() as conn:
        event=get_or_404(conn,'SELECT * FROM event_outbox WHERE id=?',(event_id,),'Outbox event not found')
        conn.execute("UPDATE event_outbox SET status='Pending',processed_at=NULL,last_error='' WHERE id=?",(event_id,))
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
        outbox_exhausted=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed') AND attempts>=?",(OUTBOX_MAX_ATTEMPTS,)).fetchone()[0]
        return {'version':APP_VERSION,'scheduler_enabled':AUTOMATION_INTERVAL_MINUTES>0,'interval_minutes':AUTOMATION_INTERVAL_MINUTES,'webhook_configured':bool(EVENT_WEBHOOK_URL),'last_run':last,'queue':{'due_pm':due_pm,'low_stock':low,'overdue_work':overdue,'pending_approvals':pending,'sla_breaches':sla_breaches,'outbox_pending':outbox_pending,'outbox_exhausted':outbox_exhausted}}

@app.get('/api/automation/runs')
def automation_runs(limit:int=Query(50,ge=1,le=200),user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('''SELECT jr.*,u.full_name actor_name FROM job_runs jr LEFT JOIN users u ON u.id=jr.actor_id ORDER BY jr.id DESC LIMIT ?''',(limit,)))

@app.post('/api/automation/run')
def automation_run(as_of:Optional[str]=None,user=Depends(require_roles('admin','maintenance_manager'))):
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
        outbox_exhausted=conn.execute("SELECT COUNT(*) FROM event_outbox WHERE status IN ('Pending','Failed') AND attempts>=?",(OUTBOX_MAX_ATTEMPTS,)).fetchone()[0]
        hs=[_asset_health(conn,x['id'])['score'] for x in rows(conn.execute('SELECT id FROM assets'))];health_avg=sum(hs)/max(len(hs),1)
        plan=_maintenance_forecast(conn,90,None);peak=plan['summary']['peak_utilization_pct'];parts_short=plan['summary']['parts_shortage_jobs'];workforce=plan['technicians']
        open_outages=conn.execute("SELECT COUNT(*) FROM asset_outages WHERE status='Open'").fetchone()[0]
        active_dispatches=conn.execute("SELECT COUNT(*) FROM dispatch_assignments WHERE status IN ('Dispatched','Accepted','En Route','On Site')").fetchone()[0]
        reserved_units=conn.execute('SELECT COALESCE(SUM(reserved_stock),0) FROM inventory_items').fetchone()[0] or 0
        active_alarms=conn.execute("SELECT COUNT(*) FROM operational_alarms WHERE status IN ('Open','Acknowledged')").fetchone()[0]
        critical_alarms=conn.execute("SELECT COUNT(*) FROM operational_alarms WHERE status IN ('Open','Acknowledged') AND severity='Critical'").fetchone()[0]
        telemetry_channels=conn.execute("SELECT COUNT(*) FROM telemetry_channels WHERE active=1").fetchone()[0]
    lines=['# HELP euas_requests_total Total HTTP requests','# TYPE euas_requests_total counter',f'euas_requests_total {total}',f'euas_request_errors_total {_REQUEST_METRICS["errors_total"]}',f'euas_request_latency_ms_avg {avg:.3f}',f'euas_uptime_seconds {uptime:.0f}',f'euas_active_sessions {active_sessions}',f'euas_automation_runs_succeeded {jobs}',f'euas_sla_breaches_total {sla_breaches}',f'euas_outbox_pending {outbox_pending}',f'euas_outbox_attempt_exhausted {outbox_exhausted}',f'euas_asset_health_score_avg {health_avg:.2f}',f'euas_maintenance_forecast_peak_utilization_pct {peak:.2f}',f'euas_workforce_technicians {workforce}',f'euas_parts_shortage_jobs_90d {parts_short}',f'euas_open_outages {open_outages}',f'euas_active_dispatches {active_dispatches}',f'euas_reserved_inventory_units {reserved_units}',f'euas_active_operational_alarms {active_alarms}',f'euas_critical_operational_alarms {critical_alarms}',f'euas_telemetry_channels {telemetry_channels}']
    for code,count in sorted(_REQUEST_METRICS['status'].items()):lines.append(f'euas_http_responses_total{{status="{code}"}} {count}')
    return '\n'.join(lines)+'\n'

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
        for r in rows(conn.execute(ASSET_SELECT+' WHERE a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10',(like,like))):out.append({'module':'assets','id':r['id'],'code':r['asset_no'],'title':r['name'],'subtitle':f"{r.get('site_name') or ''} Â· {r['condition']}"})
        for r in rows(conn.execute(WO_SELECT+' WHERE w.wo_no LIKE ? OR w.title LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10',(like,like,like,like))):out.append({'module':'work','id':r['id'],'code':r['wo_no'],'title':r['title'],'subtitle':f"{r.get('asset_no') or ''} Â· {r['status']}"})
        for r in rows(conn.execute('SELECT d.*,a.asset_no FROM documents d LEFT JOIN assets a ON a.id=d.asset_id WHERE d.document_no LIKE ? OR d.title LIKE ? OR a.asset_no LIKE ? LIMIT 10',(like,like,like))):out.append({'module':'documents','id':r['id'],'code':r['document_no'],'title':r['title'],'subtitle':r['category']})
        for r in rows(conn.execute('SELECT i.*,a.asset_no FROM inspections i LEFT JOIN assets a ON a.id=i.asset_id WHERE i.inspection_no LIKE ? OR i.template_name LIKE ? OR a.asset_no LIKE ? LIMIT 10',(like,like,like))):out.append({'module':'inspections','id':r['id'],'code':r['inspection_no'],'title':r['template_name'],'subtitle':r.get('asset_no') or ''})
        for r in rows(conn.execute('''SELECT o.*,a.asset_no,a.name asset_name FROM asset_outages o JOIN assets a ON a.id=o.asset_id WHERE o.outage_no LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? OR o.cause_code LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'operations','id':r['id'],'code':r['outage_no'],'title':f"Outage â€” {r['asset_no']}",'subtitle':f"{r['status']} Â· {r['outage_type']}"})
        for r in rows(conn.execute('''SELECT d.*,w.wo_no,w.title,u.full_name technician_name FROM dispatch_assignments d JOIN work_orders w ON w.id=d.work_order_id JOIN users u ON u.id=d.technician_user_id WHERE d.dispatch_no LIKE ? OR w.wo_no LIKE ? OR w.title LIKE ? OR u.full_name LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'dispatch','id':r['id'],'code':r['dispatch_no'],'title':f"{r['wo_no']} â€” {r['technician_name']}",'subtitle':r['status']})
        for r in rows(conn.execute('''SELECT oa.*,tc.channel_code,tc.name channel_name,a.asset_no,a.name asset_name FROM operational_alarms oa JOIN telemetry_channels tc ON tc.id=oa.channel_id JOIN assets a ON a.id=oa.asset_id WHERE oa.alarm_no LIKE ? OR tc.channel_code LIKE ? OR tc.name LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10''',(like,like,like,like,like))):out.append({'module':'telemetry','id':r['id'],'code':r['alarm_no'],'title':f"{r['asset_no']} â€” {r['channel_name']} alarm",'subtitle':f"{r['severity']} Â· {r['status']}"})
        for r in rows(conn.execute('''SELECT tc.*,a.asset_no,a.name asset_name FROM telemetry_channels tc JOIN assets a ON a.id=tc.asset_id WHERE tc.channel_code LIKE ? OR tc.name LIKE ? OR a.asset_no LIKE ? OR a.name LIKE ? LIMIT 10''',(like,like,like,like))):out.append({'module':'telemetry','id':r['id'],'code':r['channel_code'],'title':r['name'],'subtitle':f"{r['asset_no']} Â· {r['metric_type']}"})
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
        return {
          'summary':{'mtbf':round(mtbf,1) if mtbf is not None else None,'mttr':round(repair,1),'availability':round(availability,2),'pm_compliance':round(100*(pm_total-pm_over)/pm_total,1) if pm_total else 100,'work_order_completion_rate':round(100*done/max(total,1),1),'reliability_period_days':365},
          'maintenance_by_type':rows(conn.execute('SELECT work_type,COUNT(*) count,COALESCE(SUM(actual_cost),0) cost FROM work_orders GROUP BY work_type ORDER BY count DESC')),
          'cost_by_asset':rows(conn.execute('SELECT a.asset_no,a.name,COALESCE(SUM(w.actual_cost),0) maintenance_cost FROM assets a LEFT JOIN work_orders w ON w.asset_id=a.id GROUP BY a.id,a.asset_no,a.name ORDER BY maintenance_cost DESC')),
          'inventory_by_category':rows(conn.execute('SELECT category,COALESCE(SUM(current_stock*unit_price),0) value,COUNT(*) items FROM inventory_items GROUP BY category')),
          'hse_by_risk':rows(conn.execute("SELECT CASE WHEN risk_score>=15 THEN 'Extreme' WHEN risk_score>=10 THEN 'High' WHEN risk_score>=5 THEN 'Medium' ELSE 'Low' END risk_band,COUNT(*) count FROM safety_incidents GROUP BY risk_band")),
          'monthly_work':monthly,'backlog_by_priority':backlog,'procurement_by_vendor':procurement_vendor,'inventory_health':inventory_health,'asset_reliability':reliability,'site_reliability':site_reliability,'approval_summary':approval_summary,'maintenance_cost_ledger':cost_ledger,'asset_health_scores':health_portfolio,'maintenance_forecast':forecast,'operational_alarm_summary':alarm_summary
        }
@app.get('/api/audit/integrity')
def audit_integrity(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return verify_audit_chain(conn)

@app.get('/api/audit/replay')
def audit_replay(limit:int=Query(1000,ge=1,le=10000),user=Depends(require_roles('admin','maintenance_manager','executive'))):
    """Return the verified, replayable audit timeline for governance evidence.

    The chain is verified first; a tampered chain is rejected with 409 instead
    of ever serving reconstructed history from untrusted records.
    """
    with db() as conn:
        try:
            history=replay_audit_history(conn)
        except AuditIntegrityError as exc:
            raise HTTPException(409,str(exc))
    events=history[-limit:]
    return {'valid':True,'total':len(history),'returned':len(events),'head_hash':(events[-1]['audit_hash'] if events else ''),'events':events}

@app.get('/api/governance/retention')
def retention_policies(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    with db() as conn:return rows(conn.execute('SELECT * FROM retention_policies ORDER BY data_class'))

@app.get('/api/governance/retention/preview')
def retention_preview(user=Depends(require_roles('admin','maintenance_manager','executive'))):
    mapping={
      'Audit Trail':('audit_logs','created_at'),'Work Management':('work_orders','created_at'),'Documents':('documents','uploaded_at'),
      'Notifications':('notifications','created_at'),'Integration Events':('event_outbox','created_at')
    }
    out=[];today=datetime.now()
    with db() as conn:
        for p in rows(conn.execute('SELECT * FROM retention_policies ORDER BY data_class')):
            table_field=mapping.get(p['data_class']);eligible=0;cutoff=(today-timedelta(days=int(p['retention_days']))).isoformat(timespec='seconds')
            if table_field and p['active']:
                table,field=table_field;eligible=conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {field}<?',(cutoff,)).fetchone()[0]
            out.append({**p,'cutoff':cutoff,'eligible_records':eligible})
    return out

class RetentionPatch(BaseModel): retention_days:int=Field(gt=0,le=36500); active:Optional[bool]=None

@app.patch('/api/governance/retention/{policy_id}')
def update_retention(policy_id:int,body:RetentionPatch,user=Depends(require_roles('admin'))):
    with db() as conn:
        old=get_or_404(conn,'SELECT * FROM retention_policies WHERE id=?',(policy_id,),'Retention policy not found');changes={'retention_days':body.retention_days,'updated_at':now()}
        if body.active is not None:changes['active']=1 if body.active else 0
        conn.execute('UPDATE retention_policies SET '+','.join(f'{k}=?' for k in changes)+' WHERE id=?',(*changes.values(),policy_id));audit(conn,user['id'],'UPDATE RETENTION','Governance',old['policy_code'],old,changes);return {'ok':True}

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
def list_users(user=Depends(require_roles('admin'))):
    with db() as conn:return rows(conn.execute('''SELECT u.id,u.username,u.full_name,u.email,u.department,u.phone,u.active,r.code role,r.name role_name FROM users u JOIN roles r ON r.id=u.role_id ORDER BY u.full_name'''))
@app.post('/api/admin/users')
def create_user(body:UserIn,user=Depends(require_roles('admin'))):
    with db() as conn:
        role=get_or_404(conn,'SELECT id FROM roles WHERE code=?',(body.role_code,),'Role not found');cur=conn.execute('INSERT INTO users(username,password_hash,full_name,email,role_id,department,phone,created_at) VALUES(?,?,?,?,?,?,?,?)',(body.username,hash_password(body.password),body.full_name,body.email,role['id'],body.department,body.phone,now()));audit(conn,user['id'],'CREATE','Administration',body.username,'',{'role':body.role_code,'full_name':body.full_name});return {'id':cur.lastrowid}
@app.patch('/api/admin/users/{user_id}/status')
def set_user_status(user_id:int,body:UserStatusIn,user=Depends(require_roles('admin'))):
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
@app.get('/api/admin/roles')
def list_roles(user=Depends(current_user)):
    with db() as conn:return rows(conn.execute('SELECT * FROM roles ORDER BY name'))

app.mount('/uploads',StaticFiles(directory=UPLOAD_DIR),name='uploads')
app.mount('/static',StaticFiles(directory=STATIC_DIR),name='static')
@app.get('/')
def root():return FileResponse(STATIC_DIR/'index.html')
