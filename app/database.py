from __future__ import annotations
import re
import secrets
import sqlite3
import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from . import config as _euas_config
from .config import DB_PATH, DATABASE_URL, DB_BACKEND, SCHEMA_VERSION

class HybridRow(dict):
    """Mapping row that also supports sqlite-style integer indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_sql(sql: str) -> str:
    """Translate the small SQL subset used by EUAS from sqlite qmark syntax to PostgreSQL."""
    sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
    # qmark placeholders are only used for bind parameters in EUAS SQL.
    sql = sql.replace('?', '%s')
    if 'INSERT INTO' in sql.upper() and ' OR IGNORE ' not in sql.upper():
        # Calls that originated as INSERT OR IGNORE are marked before replacement by their conflict-sensitive tables.
        pass
    return sql


def _pg_insert_or_ignore(sql: str) -> str:
    marker = 'INSERT OR IGNORE INTO'
    if marker not in sql.upper():
        return _pg_sql(sql)
    # Preserve original case-independent replacement and append PostgreSQL conflict behavior.
    converted = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.I)
    converted = converted.replace('?', '%s').rstrip().rstrip(';')
    return converted + ' ON CONFLICT DO NOTHING'


def _postgresize_schema(script: str) -> str:
    script = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'SERIAL PRIMARY KEY', script, flags=re.I)
    script = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', script, flags=re.I)
    return script


class PostgresCursor:
    def __init__(self, cursor, connection):
        self._cursor = cursor
        self._connection = connection
        self.rowcount = cursor.rowcount
        self._lastrowid = None

    @property
    def lastrowid(self):
        if self._lastrowid is None:
            with self._connection.cursor() as c:
                c.execute('SELECT LASTVAL()')
                row = c.fetchone()
                self._lastrowid = row[0] if row else None
        return self._lastrowid

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return HybridRow(row)
        names = [d.name if hasattr(d, 'name') else d[0] for d in self._cursor.description or []]
        return HybridRow(zip(names, row))

    def fetchall(self):
        result = []
        for row in self._cursor.fetchall():
            if isinstance(row, dict):
                result.append(HybridRow(row))
            else:
                names = [d.name if hasattr(d, 'name') else d[0] for d in self._cursor.description or []]
                result.append(HybridRow(zip(names, row)))
        return result


class PostgresConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        cur = self.raw.cursor()
        cur.execute(_pg_insert_or_ignore(sql), tuple(args or ()))
        return PostgresCursor(cur, self.raw)

    def executemany(self, sql, seq):
        cur = self.raw.cursor()
        cur.executemany(_pg_insert_or_ignore(sql), list(seq))
        return PostgresCursor(cur, self.raw)

    def executescript(self, script):
        converted = _postgresize_schema(script)
        last = None
        for statement in (x.strip() for x in converted.split(';')):
            if not statement:
                continue
            cur = self.raw.cursor()
            cur.execute(statement)
            last = PostgresCursor(cur, self.raw)
        return last

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()


@contextmanager
def db():
    if DB_BACKEND == 'postgresql':
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError('PostgreSQL mode requires psycopg. Install requirements.txt.') from exc
        raw = psycopg.connect(DATABASE_URL)
        conn = PostgresConnection(raw)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now():
    return datetime.now().isoformat(timespec='seconds')

def audit_digest(prev_hash, user_id, action, module, record_id, old_value, new_value, created_at):
    """Return the deterministic SHA-256 link used by the tamper-evident audit chain."""
    payload=json.dumps({
        'prev_hash':prev_hash or '', 'user_id':int(user_id), 'action':str(action), 'module':str(module),
        'record_id':str(record_id), 'old_value':str(old_value or ''), 'new_value':str(new_value or ''),
        'created_at':str(created_at)
    },sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def _table_columns(conn, table):
    if DB_BACKEND=='sqlite':
        return {r['name'] if hasattr(r,'keys') else r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    return {r['column_name'] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?",(table,)).fetchall()}

def _ensure_schema_columns(conn):
    cols=_table_columns(conn,'audit_logs')
    if 'prev_hash' not in cols: conn.execute("ALTER TABLE audit_logs ADD COLUMN prev_hash TEXT DEFAULT ''")
    if 'audit_hash' not in cols: conn.execute("ALTER TABLE audit_logs ADD COLUMN audit_hash TEXT DEFAULT ''")
    site_cols=_table_columns(conn,'sites')
    if 'customer_count' not in site_cols: conn.execute('ALTER TABLE sites ADD COLUMN customer_count INTEGER')

def _backfill_audit_chain(conn):
    prev=''
    for r in conn.execute('SELECT id,user_id,action,module,record_id,old_value,new_value,created_at,prev_hash,audit_hash FROM audit_logs ORDER BY id').fetchall():
        if r['audit_hash']:
            prev=r['audit_hash']; continue
        digest=audit_digest(prev,r['user_id'],r['action'],r['module'],r['record_id'],r['old_value'],r['new_value'],r['created_at'])
        conn.execute('UPDATE audit_logs SET prev_hash=?,audit_hash=? WHERE id=?',(prev,digest,r['id']))
        prev=digest

def init_db(hash_password):
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS roles(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS permissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS role_permissions(
          role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
          permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
          PRIMARY KEY(role_id,permission_id)
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
          full_name TEXT NOT NULL, email TEXT UNIQUE, role_id INTEGER NOT NULL REFERENCES roles(id),
          department TEXT DEFAULT '', phone TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sites(
          id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          region TEXT NOT NULL, city TEXT NOT NULL, site_type TEXT NOT NULL, latitude REAL, longitude REAL,
          status TEXT NOT NULL DEFAULT 'Operating', manager TEXT DEFAULT '', customer_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS locations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, location_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          location_type TEXT NOT NULL, site_id INTEGER NOT NULL REFERENCES sites(id),
          parent_id INTEGER REFERENCES locations(id), floor TEXT DEFAULT '', room TEXT DEFAULT '', status TEXT DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS vendors(
          id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          category TEXT NOT NULL, contact_person TEXT DEFAULT '', email TEXT DEFAULT '', phone TEXT DEFAULT '', status TEXT DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS asset_types(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, utility_domain TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, asset_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
          asset_type_id INTEGER REFERENCES asset_types(id), category TEXT NOT NULL, manufacturer TEXT DEFAULT '', model TEXT DEFAULT '', serial_no TEXT DEFAULT '',
          installation_date TEXT, commissioning_date TEXT, purchase_cost REAL DEFAULT 0, replacement_cost REAL DEFAULT 0, current_value REAL DEFAULT 0,
          criticality TEXT NOT NULL, condition TEXT NOT NULL, status TEXT NOT NULL, location_id INTEGER REFERENCES locations(id),
          parent_asset_id INTEGER REFERENCES assets(id), department TEXT DEFAULT '', responsible_user_id INTEGER REFERENCES users(id), vendor_id INTEGER REFERENCES vendors(id),
          warranty_expiry TEXT, maintenance_strategy TEXT DEFAULT 'Preventive', last_maintenance TEXT, next_maintenance TEXT, meter_reading REAL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_assets_no ON assets(asset_no);
        CREATE INDEX IF NOT EXISTS idx_assets_location ON assets(location_id);
        CREATE TABLE IF NOT EXISTS meters(
          id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT UNIQUE NOT NULL, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          meter_type TEXT NOT NULL, unit TEXT NOT NULL, current_reading REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meter_readings(
          id INTEGER PRIMARY KEY AUTOINCREMENT, meter_id INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
          reading REAL NOT NULL, reading_at TEXT NOT NULL, entered_by INTEGER NOT NULL REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS work_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, wo_no TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
          asset_id INTEGER REFERENCES assets(id), location_id INTEGER REFERENCES locations(id), priority TEXT NOT NULL, status TEXT NOT NULL,
          work_type TEXT NOT NULL, failure_code TEXT DEFAULT '', requested_by INTEGER REFERENCES users(id), assigned_to INTEGER REFERENCES users(id), supervisor_id INTEGER REFERENCES users(id),
          target_start TEXT, target_finish TEXT, actual_start TEXT, actual_finish TEXT, estimated_hours REAL DEFAULT 0, actual_hours REAL DEFAULT 0,
          safety_requirements TEXT DEFAULT '', instructions TEXT DEFAULT '', checklist TEXT DEFAULT '', comments TEXT DEFAULT '',
          estimated_cost REAL DEFAULT 0, actual_cost REAL DEFAULT 0, completion_notes TEXT DEFAULT '', technician_signature TEXT DEFAULT '', pm_plan_id INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
        CREATE INDEX IF NOT EXISTS idx_wo_assigned ON work_orders(assigned_to,status);
        CREATE INDEX IF NOT EXISTS idx_wo_due ON work_orders(target_finish,status);
        CREATE INDEX IF NOT EXISTS idx_wo_asset ON work_orders(asset_id);
        CREATE TABLE IF NOT EXISTS work_order_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          sequence_no INTEGER NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending', completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS labor_entries(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id), hours REAL NOT NULL, labor_rate REAL NOT NULL DEFAULT 0, notes TEXT DEFAULT '', work_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_order_materials(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          inventory_item_id INTEGER NOT NULL, quantity REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0, issued_at TEXT NOT NULL, issued_by INTEGER NOT NULL REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS maintenance_plans(
          id INTEGER PRIMARY KEY AUTOINCREMENT, pm_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, asset_id INTEGER NOT NULL REFERENCES assets(id),
          trigger_type TEXT NOT NULL, interval_days INTEGER, meter_interval REAL, last_meter REAL DEFAULT 0, next_due TEXT,
          priority TEXT NOT NULL DEFAULT 'Medium', job_plan TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, compliance_target REAL DEFAULT 95,
          last_generated TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pm_due ON maintenance_plans(next_due,active);
        CREATE TABLE IF NOT EXISTS warehouses(
          id INTEGER PRIMARY KEY AUTOINCREMENT, warehouse_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, site_id INTEGER NOT NULL REFERENCES sites(id), status TEXT DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS inventory_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, item_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '', category TEXT NOT NULL,
          warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), current_stock REAL NOT NULL DEFAULT 0, reserved_stock REAL NOT NULL DEFAULT 0,
          min_level REAL NOT NULL DEFAULT 0, max_level REAL NOT NULL DEFAULT 0, reorder_point REAL NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
          unit TEXT NOT NULL DEFAULT 'ea', vendor_id INTEGER REFERENCES vendors(id), bin TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_warehouse ON inventory_items(warehouse_id,category);
        CREATE TABLE IF NOT EXISTS inventory_transactions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES inventory_items(id), tx_type TEXT NOT NULL,
          quantity REAL NOT NULL, from_warehouse_id INTEGER REFERENCES warehouses(id), to_warehouse_id INTEGER REFERENCES warehouses(id),
          work_order_id INTEGER REFERENCES work_orders(id), reference TEXT DEFAULT '', user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS purchase_requisitions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, pr_no TEXT UNIQUE NOT NULL, title TEXT NOT NULL, requester_id INTEGER REFERENCES users(id),
          site_id INTEGER REFERENCES sites(id), work_order_id INTEGER REFERENCES work_orders(id), project_id INTEGER, status TEXT NOT NULL,
          justification TEXT DEFAULT '', total_estimate REAL DEFAULT 0, created_at TEXT NOT NULL, approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS purchase_requisition_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, pr_id INTEGER NOT NULL REFERENCES purchase_requisitions(id) ON DELETE CASCADE,
          inventory_item_id INTEGER REFERENCES inventory_items(id), description TEXT NOT NULL, quantity REAL NOT NULL, estimated_unit_cost REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS quotations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, quote_no TEXT UNIQUE NOT NULL, pr_id INTEGER NOT NULL REFERENCES purchase_requisitions(id),
          vendor_id INTEGER NOT NULL REFERENCES vendors(id), amount REAL NOT NULL, valid_until TEXT, status TEXT NOT NULL DEFAULT 'Received'
        );
        CREATE TABLE IF NOT EXISTS purchase_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, po_no TEXT UNIQUE NOT NULL, pr_id INTEGER REFERENCES purchase_requisitions(id), vendor_id INTEGER NOT NULL REFERENCES vendors(id),
          status TEXT NOT NULL, order_date TEXT NOT NULL, expected_delivery TEXT, actual_receipt TEXT, total_cost REAL NOT NULL DEFAULT 0,
          work_order_id INTEGER REFERENCES work_orders(id), project_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS purchase_order_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
          inventory_item_id INTEGER REFERENCES inventory_items(id), description TEXT NOT NULL, quantity REAL NOT NULL, unit_cost REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inspections(
          id INTEGER PRIMARY KEY AUTOINCREMENT, inspection_no TEXT UNIQUE NOT NULL, template_name TEXT NOT NULL, asset_id INTEGER REFERENCES assets(id),
          work_order_id INTEGER REFERENCES work_orders(id), inspector_id INTEGER REFERENCES users(id), status TEXT NOT NULL, result TEXT,
          inspected_at TEXT, remarks TEXT DEFAULT '', corrective_wo_id INTEGER REFERENCES work_orders(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inspection_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
          item_name TEXT NOT NULL, response TEXT, reading TEXT DEFAULT '', remarks TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_asset ON inspections(asset_id,status);
        CREATE TABLE IF NOT EXISTS safety_incidents(
          id INTEGER PRIMARY KEY AUTOINCREMENT, incident_no TEXT UNIQUE NOT NULL, incident_type TEXT NOT NULL, title TEXT NOT NULL,
          site_id INTEGER REFERENCES sites(id), location_id INTEGER REFERENCES locations(id), asset_id INTEGER REFERENCES assets(id), reported_by INTEGER REFERENCES users(id),
          severity INTEGER NOT NULL, probability INTEGER NOT NULL, risk_score INTEGER NOT NULL, status TEXT NOT NULL, description TEXT NOT NULL,
          corrective_action TEXT DEFAULT '', occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, manager_id INTEGER REFERENCES users(id),
          site_id INTEGER REFERENCES sites(id), start_date TEXT, finish_date TEXT, budget REAL NOT NULL DEFAULT 0, actual_cost REAL NOT NULL DEFAULT 0,
          progress REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS project_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          task_name TEXT NOT NULL, owner_id INTEGER REFERENCES users(id), due_date TEXT, status TEXT NOT NULL DEFAULT 'Open', progress REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_no TEXT UNIQUE NOT NULL, title TEXT NOT NULL, vendor_id INTEGER REFERENCES vendors(id),
          start_date TEXT, end_date TEXT, value REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS documents(
          id INTEGER PRIMARY KEY AUTOINCREMENT, document_no TEXT UNIQUE NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL,
          file_name TEXT DEFAULT '', stored_name TEXT DEFAULT '', mime_type TEXT DEFAULT '', asset_id INTEGER REFERENCES assets(id), work_order_id INTEGER REFERENCES work_orders(id),
          location_id INTEGER REFERENCES locations(id), project_id INTEGER REFERENCES projects(id), vendor_id INTEGER REFERENCES vendors(id),
          uploaded_by INTEGER NOT NULL REFERENCES users(id), uploaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id), role_code TEXT, title TEXT NOT NULL, message TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'Info', link_module TEXT DEFAULT '', link_id TEXT DEFAULT '', is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,is_read,created_at);
        CREATE TABLE IF NOT EXISTS approval_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, approval_no TEXT UNIQUE NOT NULL, module TEXT NOT NULL, record_type TEXT NOT NULL,
          record_id INTEGER NOT NULL, record_code TEXT NOT NULL, title TEXT NOT NULL, requested_by INTEGER NOT NULL REFERENCES users(id),
          assigned_role TEXT, assigned_user_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'Pending', requested_at TEXT NOT NULL,
          decided_at TEXT, decided_by INTEGER REFERENCES users(id), comments TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_queue ON approval_requests(status,assigned_role,assigned_user_id,requested_at);
        CREATE TABLE IF NOT EXISTS workflow_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL, record_type TEXT NOT NULL, record_id INTEGER NOT NULL, record_code TEXT NOT NULL,
          event TEXT NOT NULL, from_status TEXT DEFAULT '', to_status TEXT DEFAULT '', actor_id INTEGER NOT NULL REFERENCES users(id),
          notes TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_record ON workflow_events(module,record_type,record_id,created_at);
        CREATE TABLE IF NOT EXISTS job_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_no TEXT UNIQUE NOT NULL, trigger_source TEXT NOT NULL, status TEXT NOT NULL,
          actor_id INTEGER REFERENCES users(id), as_of TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary_json TEXT DEFAULT '', error_message TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status,started_at);
        CREATE TABLE IF NOT EXISTS sla_policies(
          id INTEGER PRIMARY KEY AUTOINCREMENT, policy_code TEXT UNIQUE NOT NULL, priority TEXT UNIQUE NOT NULL,
          response_minutes INTEGER NOT NULL, resolution_minutes INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_order_sla(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER UNIQUE NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          policy_id INTEGER NOT NULL REFERENCES sla_policies(id), response_due TEXT NOT NULL, resolution_due TEXT NOT NULL,
          first_response_at TEXT, resolved_at TEXT, response_status TEXT NOT NULL DEFAULT 'Pending', resolution_status TEXT NOT NULL DEFAULT 'Pending',
          escalated_level INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_work_order_sla_due ON work_order_sla(response_status,response_due,resolution_status,resolution_due);
        CREATE TABLE IF NOT EXISTS sla_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1, message TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(work_order_id,event_type,level)
        );
        CREATE INDEX IF NOT EXISTS idx_sla_events_work ON sla_events(work_order_id,created_at);
        CREATE TABLE IF NOT EXISTS event_outbox(
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_no TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
          payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending', attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, processed_at TEXT, last_error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_status ON event_outbox(status,created_at);
        CREATE TABLE IF NOT EXISTS maintenance_cost_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, entry_no TEXT UNIQUE NOT NULL, work_order_id INTEGER REFERENCES work_orders(id),
          asset_id INTEGER REFERENCES assets(id), cost_type TEXT NOT NULL, amount REAL NOT NULL, quantity REAL NOT NULL DEFAULT 1,
          reference TEXT DEFAULT '', posted_by INTEGER NOT NULL REFERENCES users(id), posted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cost_ledger_asset ON maintenance_cost_ledger(asset_id,posted_at);
        CREATE INDEX IF NOT EXISTS idx_cost_ledger_work ON maintenance_cost_ledger(work_order_id,posted_at);
        CREATE TABLE IF NOT EXISTS report_snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT, report_no TEXT UNIQUE NOT NULL, report_type TEXT NOT NULL, scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL, title TEXT NOT NULL, snapshot_json TEXT NOT NULL, content_hash TEXT NOT NULL,
          generated_by INTEGER NOT NULL REFERENCES users(id), generated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_report_snapshots_scope ON report_snapshots(scope_type,scope_id,generated_at);
        CREATE TABLE IF NOT EXISTS backup_records(
          id INTEGER PRIMARY KEY AUTOINCREMENT, backup_no TEXT UNIQUE NOT NULL, database_backend TEXT NOT NULL, application_version TEXT NOT NULL,
          file_name TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retention_policies(
          id INTEGER PRIMARY KEY AUTOINCREMENT, policy_code TEXT UNIQUE NOT NULL, data_class TEXT UNIQUE NOT NULL, retention_days INTEGER NOT NULL,
          protected INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approval_delegations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, delegator_user_id INTEGER NOT NULL REFERENCES users(id), delegate_user_id INTEGER NOT NULL REFERENCES users(id),
          module TEXT NOT NULL DEFAULT '*', start_at TEXT NOT NULL, end_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_approval_delegations_active ON approval_delegations(delegate_user_id,active,start_at,end_at);
        CREATE TABLE IF NOT EXISTS asset_health_snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE, score REAL NOT NULL,
          risk_band TEXT NOT NULL, factors_json TEXT NOT NULL, calculated_at TEXT NOT NULL, calculated_by INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_asset_health_asset ON asset_health_snapshots(asset_id,calculated_at);
        CREATE TABLE IF NOT EXISTS crafts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, craft_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, hourly_rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS technician_profiles(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          craft_id INTEGER REFERENCES crafts(id), home_site_id INTEGER REFERENCES sites(id), weekly_hours REAL NOT NULL DEFAULT 40,
          efficiency_pct REAL NOT NULL DEFAULT 100, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_technician_profiles_site ON technician_profiles(home_site_id,active);
        CREATE TABLE IF NOT EXISTS shift_templates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, shift_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
          paid_hours REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS technician_shift_assignments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, shift_id INTEGER NOT NULL REFERENCES shift_templates(id),
          effective_from TEXT NOT NULL, effective_to TEXT, days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4', active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_shift_assignments_user ON technician_shift_assignments(user_id,active,effective_from,effective_to);
        CREATE TABLE IF NOT EXISTS technician_absences(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          absence_type TEXT NOT NULL, hours_per_day REAL NOT NULL DEFAULT 8, status TEXT NOT NULL DEFAULT 'Approved', notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_technician_absences_user ON technician_absences(user_id,status,start_date,end_date);
        CREATE TABLE IF NOT EXISTS work_order_requirements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id), quantity REAL NOT NULL, required_by TEXT, status TEXT NOT NULL DEFAULT 'Required',
          UNIQUE(work_order_id,inventory_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_work_requirements_work ON work_order_requirements(work_order_id,status);
        CREATE TABLE IF NOT EXISTS work_order_craft_requirements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          craft_id INTEGER NOT NULL REFERENCES crafts(id), planned_hours REAL NOT NULL DEFAULT 0,
          UNIQUE(work_order_id,craft_id)
        );
        CREATE INDEX IF NOT EXISTS idx_work_craft_work ON work_order_craft_requirements(work_order_id,craft_id);
        CREATE TABLE IF NOT EXISTS inventory_reservations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_no TEXT UNIQUE NOT NULL,
          work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id), quantity REAL NOT NULL,
          issued_quantity REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Reserved',
          reserved_by INTEGER NOT NULL REFERENCES users(id), reserved_at TEXT NOT NULL,
          released_at TEXT, notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_work ON inventory_reservations(work_order_id,status);
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_item ON inventory_reservations(inventory_item_id,status);
        CREATE TABLE IF NOT EXISTS asset_outages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, outage_no TEXT UNIQUE NOT NULL,
          asset_id INTEGER NOT NULL REFERENCES assets(id), site_id INTEGER REFERENCES sites(id), work_order_id INTEGER REFERENCES work_orders(id),
          outage_type TEXT NOT NULL DEFAULT 'Forced', status TEXT NOT NULL DEFAULT 'Open', cause_code TEXT DEFAULT '', impact TEXT DEFAULT '',
          lost_capacity REAL NOT NULL DEFAULT 0, capacity_unit TEXT DEFAULT '', start_at TEXT NOT NULL, end_at TEXT,
          reported_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_asset_outages_asset ON asset_outages(asset_id,start_at,end_at);
        CREATE INDEX IF NOT EXISTS idx_asset_outages_site ON asset_outages(site_id,status,start_at);
        CREATE TABLE IF NOT EXISTS dispatch_assignments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_no TEXT UNIQUE NOT NULL,
          work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
          technician_user_id INTEGER NOT NULL REFERENCES users(id), dispatched_by INTEGER NOT NULL REFERENCES users(id),
          status TEXT NOT NULL DEFAULT 'Dispatched', eta_minutes INTEGER, notes TEXT DEFAULT '',
          dispatched_at TEXT NOT NULL, accepted_at TEXT, enroute_at TEXT, arrived_at TEXT, completed_at TEXT, cancelled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dispatch_technician ON dispatch_assignments(technician_user_id,status,dispatched_at);
        CREATE INDEX IF NOT EXISTS idx_dispatch_work ON dispatch_assignments(work_order_id,status);
        CREATE TABLE IF NOT EXISTS telemetry_channels(
          id INTEGER PRIMARY KEY AUTOINCREMENT, channel_code TEXT UNIQUE NOT NULL,
          asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE, name TEXT NOT NULL,
          metric_type TEXT NOT NULL, unit TEXT NOT NULL, source_system TEXT NOT NULL DEFAULT 'Manual',
          warning_low REAL, critical_low REAL, warning_high REAL, critical_high REAL,
          active INTEGER NOT NULL DEFAULT 1, last_value REAL, last_quality TEXT DEFAULT '', last_reading_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_telemetry_channels_asset ON telemetry_channels(asset_id,active);
        CREATE INDEX IF NOT EXISTS idx_telemetry_channels_metric ON telemetry_channels(metric_type,active);
        CREATE TABLE IF NOT EXISTS telemetry_readings(
          id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL REFERENCES telemetry_channels(id) ON DELETE CASCADE,
          value REAL NOT NULL, quality TEXT NOT NULL DEFAULT 'Good', source TEXT NOT NULL DEFAULT 'Manual',
          captured_at TEXT NOT NULL, ingested_at TEXT NOT NULL, ingested_by INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_telemetry_readings_channel_time ON telemetry_readings(channel_id,captured_at);
        CREATE TABLE IF NOT EXISTS operational_alarms(
          id INTEGER PRIMARY KEY AUTOINCREMENT, alarm_no TEXT UNIQUE NOT NULL,
          channel_id INTEGER NOT NULL REFERENCES telemetry_channels(id), asset_id INTEGER NOT NULL REFERENCES assets(id), site_id INTEGER REFERENCES sites(id),
          severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Open', alarm_type TEXT NOT NULL DEFAULT 'Threshold', message TEXT NOT NULL,
          trigger_value REAL NOT NULL, threshold_value REAL, opened_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          acknowledged_at TEXT, acknowledged_by INTEGER REFERENCES users(id), cleared_at TEXT, closed_at TEXT, closed_by INTEGER REFERENCES users(id),
          work_order_id INTEGER REFERENCES work_orders(id), occurrence_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_operational_alarms_status ON operational_alarms(status,severity,opened_at);
        CREATE INDEX IF NOT EXISTS idx_operational_alarms_asset ON operational_alarms(asset_id,status,opened_at);
        CREATE TABLE IF NOT EXISTS schema_migrations(
          version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, module TEXT NOT NULL,
          record_id TEXT NOT NULL, old_value TEXT DEFAULT '', new_value TEXT DEFAULT '', created_at TEXT NOT NULL,
          prev_hash TEXT DEFAULT '', audit_hash TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_chain ON audit_logs(id,audit_hash);
        CREATE TABLE IF NOT EXISTS cbm_recommendations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, recommendation_no TEXT UNIQUE NOT NULL,
          asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          channel_id INTEGER REFERENCES telemetry_channels(id) ON DELETE SET NULL,
          condition_type TEXT NOT NULL, severity TEXT NOT NULL,
          evidence_json TEXT NOT NULL DEFAULT '{}', suggested_action TEXT NOT NULL DEFAULT '',
          confidence TEXT NOT NULL DEFAULT 'deterministic', status TEXT NOT NULL DEFAULT 'Open',
          work_order_id INTEGER REFERENCES work_orders(id),
          created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
          decided_at TEXT, decided_by INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cbm_asset_status ON cbm_recommendations(asset_id,status);
        CREATE INDEX IF NOT EXISTS idx_cbm_channel_status ON cbm_recommendations(channel_id,status);
        CREATE TABLE IF NOT EXISTS fmea_records(
          id INTEGER PRIMARY KEY AUTOINCREMENT, fmea_no TEXT UNIQUE NOT NULL,
          asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
          function_text TEXT NOT NULL DEFAULT '', failure_mode TEXT NOT NULL,
          failure_cause TEXT DEFAULT '', failure_effect TEXT DEFAULT '',
          severity INTEGER NOT NULL, occurrence INTEGER NOT NULL, detection INTEGER NOT NULL,
          rpn INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Draft',
          created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, approved_by INTEGER REFERENCES users(id), approved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fmea_asset ON fmea_records(asset_id,status);
        ''')
        _ensure_schema_columns(conn)
        _backfill_audit_chain(conn)
        conn.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)',(SCHEMA_VERSION,now()))
        # A database written by a NEWER release must never be opened by an
        # older binary: columns and constraints it relies on may not exist
        # here. Compare against the unpinned config constant because
        # initialize_auth_database temporarily pins this module's
        # SCHEMA_VERSION to stage the base schema before the auth migration.
        db_version=int(conn.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0])
        if db_version>int(_euas_config.SCHEMA_VERSION):
            raise RuntimeError(
                f'Database schema version {db_version} is newer than application schema version '
                f'{_euas_config.SCHEMA_VERSION}; refusing to start. Upgrade the application before using this database.'
            )

        if conn.execute('SELECT COUNT(*) FROM roles').fetchone()[0] == 0:
            roles=[
              ('admin','System Administrator'),('asset_manager','Asset Manager'),('maintenance_manager','Maintenance Manager'),
              ('planner','Maintenance Planner'),('supervisor','Supervisor'),('technician','Technician'),('storekeeper','Storekeeper'),
              ('procurement','Procurement Officer'),('hse','HSE Officer'),('project_manager','Project Manager'),('executive','Executive Viewer')]
            conn.executemany('INSERT INTO roles(code,name) VALUES(?,?)', roles)
            permissions=[('assets.manage','Manage assets'),('work.manage','Manage work'),('inventory.manage','Manage inventory'),('procurement.manage','Manage procurement'),('hse.manage','Manage HSE'),('admin.manage','Administration')]
            conn.executemany('INSERT INTO permissions(code,name) VALUES(?,?)', permissions)
            role_ids={r['code']:r['id'] for r in conn.execute('SELECT * FROM roles')}
            perm_ids={p['code']:p['id'] for p in conn.execute('SELECT * FROM permissions')}
            for role in role_ids:
                allowed=list(perm_ids) if role=='admin' else []
                if role in ('asset_manager','maintenance_manager','planner','supervisor','technician'): allowed += ['assets.manage','work.manage']
                if role in ('storekeeper','maintenance_manager','planner'): allowed += ['inventory.manage']
                if role in ('procurement','maintenance_manager'): allowed += ['procurement.manage']
                if role=='hse': allowed += ['hse.manage']
                for code in set(allowed): conn.execute('INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)',(role_ids[role],perm_ids[code]))

        if conn.execute('SELECT COUNT(*) FROM sla_policies').fetchone()[0] == 0:
            conn.executemany('INSERT INTO sla_policies(policy_code,priority,response_minutes,resolution_minutes,active,updated_at) VALUES(?,?,?,?,1,?)',[
              ('SLA-EMERGENCY','Emergency',15,240,now()),('SLA-CRITICAL','Critical',30,480,now()),
              ('SLA-HIGH','High',120,1440,now()),('SLA-MEDIUM','Medium',480,4320,now()),('SLA-LOW','Low',1440,10080,now())])

        if conn.execute('SELECT COUNT(*) FROM retention_policies').fetchone()[0] == 0:
            conn.executemany('INSERT INTO retention_policies(policy_code,data_class,retention_days,protected,active,updated_at) VALUES(?,?,?,?,1,?)',[
              ('RET-AUDIT','Audit Trail',2555,1,now()),('RET-WORK','Work Management',3650,1,now()),
              ('RET-DOCS','Documents',3650,0,now()),('RET-NOTIFY','Notifications',365,0,now()),('RET-EVENTS','Integration Events',730,0,now())])

        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            role_ids={r['code']:r['id'] for r in conn.execute('SELECT * FROM roles')}
            users=[
              ('omar','EUAS@2026','Omar Islam','omar@euas.local','admin','Digital Transformation','+20 100 000 0001'),
              ('seif','EUAS@2026','Seif','seif@euas.local','maintenance_manager','Utilities Maintenance','+20 100 000 0002'),
              ('planner','Planner@2026','Mariam Hassan','mariam@euas.local','planner','Planning',''),
              ('supervisor','Supervisor@2026','Ahmed Nabil','ahmed.nabil@euas.local','supervisor','Maintenance',''),
              ('tech1','Tech@2026','Mahmoud Ali','mahmoud.ali@euas.local','technician','Field Service',''),
              ('tech2','Tech2@2026','Mostafa Samir','mostafa.samir@euas.local','technician','Field Service',''),
              ('store','Store@2026','Sara Adel','sara.adel@euas.local','storekeeper','Stores',''),
              ('proc','Proc@2026','Nour Khaled','nour.khaled@euas.local','procurement','Procurement',''),
              ('hse','HSE@2026','Youssef Sameh','youssef.sameh@euas.local','hse','HSE',''),
              ('exec','Viewer@2026','Executive Viewer','executive@euas.local','executive','Management','')]
            for u,p,n,e,r,d,ph in users:
                conn.execute('INSERT INTO users(username,password_hash,full_name,email,role_id,department,phone,created_at) VALUES(?,?,?,?,?,?,?,?)',(u,hash_password(p),n,e,role_ids[r],d,ph,now()))

        if not conn.execute("SELECT id FROM users WHERE username='system'").fetchone():
            role_ids={r['code']:r['id'] for r in conn.execute('SELECT * FROM roles')}
            conn.execute('INSERT INTO users(username,password_hash,full_name,email,role_id,department,phone,active,created_at) VALUES(?,?,?,?,?,?,?,0,?)',('system',hash_password(secrets.token_urlsafe(48)),'EUAS Automation Service','system@euas.local',role_ids['admin'],'Platform Automation','',now()))

        if conn.execute('SELECT COUNT(*) FROM sites').fetchone()[0] == 0:
            conn.executemany('INSERT INTO sites(site_code,name,region,city,site_type,latitude,longitude,status,manager) VALUES(?,?,?,?,?,?,?,?,?)',[
              ('CAI-OPS','Cairo Utility Operations','Greater Cairo','Cairo','Operations Centre',30.0444,31.2357,'Operating','Seif'),
              ('NCS-01','New Cairo Substation','Greater Cairo','New Cairo','Electrical Substation',30.0074,31.4913,'Operating','Ahmed Nabil'),
              ('ALX-OPS','Alexandria Operations Centre','Alexandria','Alexandria','Operations Centre',31.2001,29.9187,'Operating','Mariam Hassan'),
              ('IWP-01','Industrial Water Plant','Suez Canal','Ain Sokhna','Water Treatment Plant',29.6002,32.3167,'Operating','Mahmoud Ali')])
            s={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}
            locs=[
              ('NCS-YARD','Substation Yard','Site',s['NCS-01'],None,'',''),('NCS-TR-BAY','Transformer Bay','Area',s['NCS-01'],1,'',''),('NCS-SWGR','11 kV Switchgear Room','Building',s['NCS-01'],1,'Ground',''),
              ('CAI-WH','Cairo Central Warehouse','Building',s['CAI-OPS'],None,'Ground','WH-01'),('IWP-PUMP','Main Pump Hall','Building',s['IWP-01'],None,'Ground',''),('ALX-MEP','MEP Plant Room','Room',s['ALX-OPS'],None,'B1','MEP')]
            for code,name,typ,site,parent,floor,room in locs: conn.execute('INSERT INTO locations(location_code,name,location_type,site_id,parent_id,floor,room) VALUES(?,?,?,?,?,?,?)',(code,name,typ,site,parent,floor,room))

        if conn.execute('SELECT COUNT(*) FROM crafts').fetchone()[0] == 0:
            conn.executemany('INSERT INTO crafts(craft_code,name,hourly_rate,active) VALUES(?,?,?,1)',[
              ('ELEC-HV','High Voltage Electrical',32),('MECH','Mechanical',28),('HVAC','HVAC',26),('GEN','General Maintenance',22)])

        if conn.execute('SELECT COUNT(*) FROM shift_templates').fetchone()[0] == 0:
            conn.executemany('INSERT INTO shift_templates(shift_code,name,start_time,end_time,paid_hours,active) VALUES(?,?,?,?,?,1)',[
              ('DAY-8','Day Shift','08:00','16:00',8),('ROT-A','Operations Rotation A','07:00','19:00',12)])

        if conn.execute('SELECT COUNT(*) FROM technician_profiles').fetchone()[0] == 0:
            users_map={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            site_map={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}
            craft_map={r['craft_code']:r['id'] for r in conn.execute('SELECT id,craft_code FROM crafts')}
            conn.executemany('INSERT INTO technician_profiles(user_id,craft_id,home_site_id,weekly_hours,efficiency_pct,active,updated_at) VALUES(?,?,?,?,?,1,?)',[
              (users_map['tech1'],craft_map['ELEC-HV'],site_map['NCS-01'],40,92,now()),
              (users_map['tech2'],craft_map['ELEC-HV'],site_map['NCS-01'],40,88,now())])
            shifts={r['shift_code']:r['id'] for r in conn.execute('SELECT id,shift_code FROM shift_templates')}
            today=date.today()
            conn.executemany('INSERT INTO technician_shift_assignments(user_id,shift_id,effective_from,effective_to,days_of_week,active) VALUES(?,?,?,?,?,1)',[
              (users_map['tech1'],shifts['DAY-8'],(today-timedelta(days=180)).isoformat(),None,'0,1,2,3,4'),
              (users_map['tech2'],shifts['DAY-8'],(today-timedelta(days=180)).isoformat(),None,'0,1,2,3,4')])
            conn.execute('INSERT INTO technician_absences(user_id,start_date,end_date,absence_type,hours_per_day,status,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
              (users_map['tech2'],(today+timedelta(days=14)).isoformat(),(today+timedelta(days=15)).isoformat(),'Annual Leave',8,'Approved','Demo planned leave',users_map['supervisor'],now()))

        if conn.execute('SELECT COUNT(*) FROM vendors').fetchone()[0] == 0:
            conn.executemany('INSERT INTO vendors(vendor_code,name,category,contact_person,email,phone) VALUES(?,?,?,?,?,?)',[
              ('VND-ABB','ABB Egypt','Electrical OEM','Karim Adel','service@example.local','+20 2 0000 1001'),
              ('VND-SCH','Schneider Electric','Electrical OEM','Mona Samir','support@example.local','+20 2 0000 1002'),
              ('VND-PMP','FlowServe Services','Pumps','Hany Fawzy','service@example.local','+20 2 0000 1003'),
              ('VND-HVAC','Carrier Service Partner','HVAC','Dina Ashraf','service@example.local','+20 2 0000 1004')])

        if conn.execute('SELECT COUNT(*) FROM asset_types').fetchone()[0] == 0:
            conn.executemany('INSERT INTO asset_types(code,name,utility_domain) VALUES(?,?,?)',[
              ('TRANSFORMER','Power Transformer','Electrical'),('BREAKER','Circuit Breaker','Electrical'),('SWITCHGEAR','Switchgear','Electrical'),('PUMP','Pump','Water'),('GENERATOR','Generator','Infrastructure'),('AHU','Air Handling Unit','Infrastructure'),('METER','Utility Meter','Cross-utility')])

        if conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == 0:
            u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}; v={r['vendor_code']:r['id'] for r in conn.execute('SELECT id,vendor_code FROM vendors')}; t={r['code']:r['id'] for r in conn.execute('SELECT id,code FROM asset_types')}; l={r['location_code']:r['id'] for r in conn.execute('SELECT id,location_code FROM locations')}
            assets=[
              ('TR-001','33/11 kV Power Transformer','Primary transformer serving New Cairo Substation',t['TRANSFORMER'],'Transformer','ABB','PowerTrafo 40MVA','TR33-11001','2020-04-10','2020-06-01',450000,620000,390000,'Critical','Warning','Operating',l['NCS-TR-BAY'],None,'Electrical Operations',u['supervisor'],v['VND-ABB'],'2027-06-01','Condition Based','2026-05-20','2026-08-20',78.4),
              ('SWGR-001','11 kV Main Switchgear','Main switchgear lineup',t['SWITCHGEAR'],'Switchgear','Schneider Electric','PIX','SW-11001','2020-05-01','2020-06-01',210000,290000,180000,'High','Good','Operating',l['NCS-SWGR'],None,'Electrical Operations',u['supervisor'],v['VND-SCH'],'2027-05-01','Preventive','2026-06-01','2026-09-01',0),
              ('CB-101','11 kV Circuit Breaker','Feeder circuit breaker',t['BREAKER'],'Circuit Breaker','Schneider Electric','Evolis','CB-101-A','2020-05-01','2020-06-01',24000,32000,19000,'High','Good','Operating',l['NCS-SWGR'],None,'Electrical Operations',u['tech1'],v['VND-SCH'],'2027-05-01','Preventive','2026-07-10','2026-10-10',3250),
              ('PMP-301','Water Distribution Pump','Duty pump for industrial water distribution',t['PUMP'],'Pump','FlowServe','HPX','PMP301-22','2021-02-11','2021-03-01',68000,85000,51000,'High','Good','Operating',l['IWP-PUMP'],None,'Water Operations',u['tech1'],v['VND-PMP'],'2026-12-31','Condition Based','2026-07-25','2026-09-25',8440),
              ('GEN-201','Emergency Generator','Backup generator for Cairo operations',t['GENERATOR'],'Generator','Cummins','C500D5','GEN201-16','2019-01-10','2019-02-01',92000,125000,61000,'High','Good','Standby',l['CAI-WH'],None,'Facilities',u['tech1'],None,None,'Preventive','2026-07-01','2026-09-01',3210),
              ('HVAC-401','AHU Unit 401','Operations centre air handling unit',t['AHU'],'HVAC','Carrier','39HQ','AHU401-7','2022-01-15','2022-02-01',17000,23000,12000,'Medium','Fair','Operating',l['ALX-MEP'],None,'Facilities',u['tech1'],v['VND-HVAC'],'2027-01-15','Preventive','2026-08-01','2026-09-01',6240)]
            for a in assets:
                conn.execute('''INSERT INTO assets(asset_no,name,description,asset_type_id,category,manufacturer,model,serial_no,installation_date,commissioning_date,purchase_cost,replacement_cost,current_value,criticality,condition,status,location_id,parent_asset_id,department,responsible_user_id,vendor_id,warranty_expiry,maintenance_strategy,last_maintenance,next_maintenance,meter_reading,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',a+(now(),now()))
            ids={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}
            conn.execute('UPDATE assets SET parent_asset_id=? WHERE asset_no=?',(ids['SWGR-001'],'CB-101'))
            conn.executemany('INSERT INTO meters(meter_code,asset_id,meter_type,unit,current_reading) VALUES(?,?,?,?,?)',[
              ('MTR-TR001-TEMP',ids['TR-001'],'Temperature','°C',78.4),('MTR-CB101-OPS',ids['CB-101'],'Operation Count','ops',3250),('MTR-PMP301-HRS',ids['PMP-301'],'Runtime','h',8440)])

        if conn.execute('SELECT COUNT(*) FROM warehouses').fetchone()[0] == 0:
            sites={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}
            conn.executemany('INSERT INTO warehouses(warehouse_code,name,site_id) VALUES(?,?,?)',[
              ('WH-CAI','Cairo Central Warehouse',sites['CAI-OPS']),('WH-NCS','New Cairo Substation Store',sites['NCS-01']),('WH-IWP','Industrial Water Plant Store',sites['IWP-01'])])

        if conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0] == 0:
            w={r['warehouse_code']:r['id'] for r in conn.execute('SELECT id,warehouse_code FROM warehouses')}; v={r['vendor_code']:r['id'] for r in conn.execute('SELECT id,vendor_code FROM vendors')}
            conn.executemany('''INSERT INTO inventory_items(item_no,name,description,category,warehouse_id,current_stock,reserved_stock,min_level,max_level,reorder_point,unit_price,unit,vendor_id,bin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
              ('OIL-FLT-TR','Transformer Oil Filter','Replacement filtration cartridge','Electrical',w['WH-NCS'],3,1,2,12,4,165,'ea',v['VND-ABB'],'E-01-02'),
              ('BRK-COIL-11KV','11 kV Breaker Trip Coil','Trip coil spare for breaker','Electrical',w['WH-NCS'],2,0,1,6,2,410,'ea',v['VND-SCH'],'E-02-01'),
              ('PUMP-SEAL-301','Pump Mechanical Seal Kit','Seal kit compatible with PMP-301','Mechanical',w['WH-IWP'],1,0,1,5,2,520,'kit',v['VND-PMP'],'M-01-04'),
              ('AHU-FLT-600','AHU Filter 600x600','MERV filter','HVAC',w['WH-CAI'],18,2,8,30,10,28,'ea',v['VND-HVAC'],'H-03-06')])

        if conn.execute('SELECT COUNT(*) FROM maintenance_plans').fetchone()[0] == 0:
            a={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}; today=date.today()
            conn.executemany('''INSERT INTO maintenance_plans(pm_no,name,asset_id,trigger_type,interval_days,meter_interval,last_meter,next_due,priority,job_plan,active,compliance_target,last_generated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
              ('PM-TR-001','Transformer Quarterly Inspection',a['TR-001'],'Calendar',90,None,0,(today-timedelta(days=1)).isoformat(),'High','Inspect oil level, leaks, temperature, bushings, grounding and abnormal noise.',1,95,None),
              ('PM-CB-101','Breaker Operation Service',a['CB-101'],'Meter',None,500,3000,None,'Medium','Inspect mechanism, lubricate, verify trip/close coils and operation count.',1,95,None),
              ('PM-PMP-301','Pump Runtime Service',a['PMP-301'],'Meter',None,1000,8000,None,'Medium','Inspect coupling, seal leakage, vibration and bearings.',1,95,None)])

        if conn.execute('SELECT COUNT(*) FROM work_orders').fetchone()[0] == 0:
            a={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}; u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}; l={r['location_code']:r['id'] for r in conn.execute('SELECT id,location_code FROM locations')}; today=date.today()
            rows=[
              ('WO-10025','Investigate Transformer Oil Temperature','Oil temperature trend above normal operating baseline.',a['TR-001'],l['NCS-TR-BAY'],'High','Assigned','Corrective','TEMP-HIGH',u['omar'],u['tech1'],u['supervisor'],today.isoformat(),(today+timedelta(days=1)).isoformat(),None,None,4,0,'Electrical isolation and PTW required','Check oil level, inspect cooling fans, capture temperature reading.','Oil Level;Oil Leakage;Temperature;Noise;Grounding','',250,0,'',None),
              ('WO-10021','Inspect 11 kV Breaker Mechanism','Routine inspection of breaker mechanism.',a['CB-101'],l['NCS-SWGR'],'Medium','In Progress','Preventive','',u['planner'],u['tech1'],u['supervisor'],(today-timedelta(days=1)).isoformat(),today.isoformat(),(today-timedelta(days=1)).isoformat(),None,3,1.5,'Arc-flash PPE','Inspect mechanism and trip circuit.','','',90,40,'',None),
              ('WO-10018','Pump Seal Leakage','Minor leakage observed on distribution pump.',a['PMP-301'],l['IWP-PUMP'],'High','Completed','Corrective','LEAK-SEAL',u['supervisor'],u['tech1'],u['supervisor'],(today-timedelta(days=7)).isoformat(),(today-timedelta(days=6)).isoformat(),(today-timedelta(days=7)).isoformat(),(today-timedelta(days=6)).isoformat(),5,4,'LOTO required','Replace seal kit and align coupling.','','',620,590,'Seal replaced and pump returned to service.',None)]
            for r in rows: conn.execute('''INSERT INTO work_orders(wo_no,title,description,asset_id,location_id,priority,status,work_type,failure_code,requested_by,assigned_to,supervisor_id,target_start,target_finish,actual_start,actual_finish,estimated_hours,actual_hours,safety_requirements,instructions,checklist,comments,estimated_cost,actual_cost,completion_notes,pm_plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',r+(now(),now()))
            wo={r['wo_no']:r['id'] for r in conn.execute('SELECT id,wo_no FROM work_orders')}
            conn.executemany('INSERT INTO work_order_tasks(work_order_id,sequence_no,task,status) VALUES(?,?,?,?)',[(wo['WO-10025'],1,'Verify permit to work','Pending'),(wo['WO-10025'],2,'Inspect transformer oil system','Pending'),(wo['WO-10025'],3,'Record temperature and observations','Pending')])
            items={r['item_no']:r['id'] for r in conn.execute('SELECT id,item_no FROM inventory_items')}
            conn.executemany('INSERT OR IGNORE INTO work_order_requirements(work_order_id,inventory_item_id,quantity,required_by,status) VALUES(?,?,?,?,?)',[
              (wo['WO-10025'],items['OIL-FLT-TR'],2,(today+timedelta(days=1)).isoformat(),'Required'),
              (wo['WO-10021'],items['BRK-COIL-11KV'],1,today.isoformat(),'Required')])
            crafts_map={r['craft_code']:r['id'] for r in conn.execute('SELECT id,craft_code FROM crafts')}
            conn.executemany('INSERT OR IGNORE INTO work_order_craft_requirements(work_order_id,craft_id,planned_hours) VALUES(?,?,?)',[
              (wo['WO-10025'],crafts_map['ELEC-HV'],4),(wo['WO-10021'],crafts_map['ELEC-HV'],3),(wo['WO-10018'],crafts_map['MECH'],5)])

        if conn.execute('SELECT COUNT(*) FROM inventory_reservations').fetchone()[0] == 0:
            wo_map={r['wo_no']:r['id'] for r in conn.execute('SELECT id,wo_no FROM work_orders')}; item_map={r['item_no']:r['id'] for r in conn.execute('SELECT id,item_no FROM inventory_items')}; user_map={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            if 'WO-10025' in wo_map and 'OIL-FLT-TR' in item_map:
                conn.execute("INSERT INTO inventory_reservations(reservation_no,work_order_id,inventory_item_id,quantity,issued_quantity,status,reserved_by,reserved_at,notes) VALUES(?,?,?,?,0,'Reserved',?,?,?)",('RSV-20001',wo_map['WO-10025'],item_map['OIL-FLT-TR'],1,user_map['planner'],now(),'Seeded reservation for transformer investigation'))

        if conn.execute('SELECT COUNT(*) FROM asset_outages').fetchone()[0] == 0:
            assets_map={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}; site_map={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}; wo_map={r['wo_no']:r['id'] for r in conn.execute('SELECT id,wo_no FROM work_orders')}; user_map={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}; d=date.today()-timedelta(days=7)
            conn.execute("INSERT INTO asset_outages(outage_no,asset_id,site_id,work_order_id,outage_type,status,cause_code,impact,lost_capacity,capacity_unit,start_at,end_at,reported_by,created_at,updated_at) VALUES(?,?,?,?,?,'Closed',?,?,?,?,?,?,?,?,?)",('OUT-30001',assets_map['PMP-301'],site_map['IWP-01'],wo_map.get('WO-10018'),'Forced','LEAK-SEAL','Distribution pump unavailable during seal repair',100,'%',f'{d.isoformat()}T08:00:00',f'{d.isoformat()}T12:00:00',user_map['supervisor'],now(),now()))

        if conn.execute('SELECT COUNT(*) FROM dispatch_assignments').fetchone()[0] == 0:
            wo_map={r['wo_no']:r['id'] for r in conn.execute('SELECT id,wo_no FROM work_orders')}; user_map={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            if 'WO-10025' in wo_map:
                conn.execute("INSERT INTO dispatch_assignments(dispatch_no,work_order_id,technician_user_id,dispatched_by,status,eta_minutes,notes,dispatched_at) VALUES(?,?,?,?,?,?,?,?)",('DSP-40001',wo_map['WO-10025'],user_map['tech1'],user_map['supervisor'],'Dispatched',25,'Attend transformer bay and investigate abnormal oil temperature.',now()))

        if conn.execute('SELECT COUNT(*) FROM telemetry_channels').fetchone()[0] == 0:
            amap={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}
            stamp=now()
            conn.executemany('INSERT INTO telemetry_channels(channel_code,asset_id,name,metric_type,unit,source_system,warning_low,critical_low,warning_high,critical_high,active,last_value,last_quality,last_reading_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[
              ('TEL-TR001-OIL-TEMP',amap['TR-001'],'Transformer Oil Temperature','Temperature','°C','SCADA',None,None,80,90,1,84.0,'Good',stamp,stamp,stamp),
              ('TEL-TR001-LOAD',amap['TR-001'],'Transformer Loading','Load','%','SCADA',None,None,90,100,1,72.0,'Good',stamp,stamp,stamp),
              ('TEL-PMP301-VIB',amap['PMP-301'],'Pump Vibration','Vibration','mm/s','Condition Monitor',None,None,4.5,7.1,1,3.2,'Good',stamp,stamp,stamp)])
            cmap={r['channel_code']:r['id'] for r in conn.execute('SELECT id,channel_code FROM telemetry_channels')}
            system=conn.execute("SELECT id FROM users WHERE username='system'").fetchone(); system_id=system['id'] if system else None
            for code,vals in [('TEL-TR001-OIL-TEMP',[76.8,78.2,81.4,84.0]),('TEL-TR001-LOAD',[65,69,74,72]),('TEL-PMP301-VIB',[2.8,3.0,3.1,3.2])]:
                for idx,val in enumerate(vals):
                    captured=(datetime.now()-timedelta(hours=(len(vals)-idx-1)*2)).isoformat(timespec='seconds')
                    conn.execute('INSERT INTO telemetry_readings(channel_id,value,quality,source,captured_at,ingested_at,ingested_by) VALUES(?,?,?,?,?,?,?)',(cmap[code],val,'Good','Seed',captured,stamp,system_id))
            site=conn.execute('SELECT s.id FROM assets a LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id WHERE a.id=?',(amap['TR-001'],)).fetchone()
            conn.execute("INSERT INTO operational_alarms(alarm_no,channel_id,asset_id,site_id,severity,status,alarm_type,message,trigger_value,threshold_value,opened_at,last_seen_at,occurrence_count) VALUES(?,?,?,?,?,'Open','Threshold',?,?,?,?,?,1)",('ALM-50001',cmap['TEL-TR001-OIL-TEMP'],amap['TR-001'],site['id'] if site else None,'Warning','Transformer Oil Temperature high: 84 °C',84.0,80.0,stamp,stamp))

        if conn.execute('SELECT COUNT(*) FROM inspections').fetchone()[0] == 0:
            a={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')}; u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            cur=conn.execute('INSERT INTO inspections(inspection_no,template_name,asset_id,inspector_id,status,result,created_at) VALUES(?,?,?,?,?,?,?)',('INS-5001','Transformer Inspection',a['TR-001'],u['tech1'],'Draft',None,now()))
            iid=cur.lastrowid
            for name in ['Oil Level','Oil Leakage','Temperature','Voltage','Current','Noise','Grounding','Physical Damage']:
                conn.execute('INSERT INTO inspection_items(inspection_id,item_name) VALUES(?,?)',(iid,name))

        if conn.execute('SELECT COUNT(*) FROM safety_incidents').fetchone()[0] == 0:
            s={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}; u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            conn.execute('''INSERT INTO safety_incidents(incident_no,incident_type,title,site_id,reported_by,severity,probability,risk_score,status,description,corrective_action,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',('HSE-7001','Near Miss','Loose cable cover near switchgear access',s['NCS-01'],u['hse'],2,2,4,'Closed','Cable cover found unsecured during routine walkdown.','Cover secured and access route inspected.',(date.today()-timedelta(days=5)).isoformat(),now()))

        if conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 0:
            s={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}; u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            cur=conn.execute('INSERT INTO projects(project_no,name,manager_id,site_id,start_date,finish_date,budget,actual_cost,progress,status) VALUES(?,?,?,?,?,?,?,?,?,?)',('PRJ-3001','New Cairo Reliability Improvement',u['seif'],s['NCS-01'],'2026-07-01','2026-12-15',850000,214000,38,'Active'))
            pid=cur.lastrowid
            conn.executemany('INSERT INTO project_tasks(project_id,task_name,owner_id,due_date,status,progress) VALUES(?,?,?,?,?,?)',[(pid,'Transformer cooling review',u['supervisor'],'2026-08-31','In Progress',55),(pid,'11 kV spare strategy',u['planner'],'2026-09-15','Open',20)])

        if conn.execute('SELECT COUNT(*) FROM purchase_requisitions').fetchone()[0] == 0:
            u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}; s={r['site_code']:r['id'] for r in conn.execute('SELECT id,site_code FROM sites')}; i={r['item_no']:r['id'] for r in conn.execute('SELECT id,item_no FROM inventory_items')}
            cur=conn.execute('INSERT INTO purchase_requisitions(pr_no,title,requester_id,site_id,status,justification,total_estimate,created_at) VALUES(?,?,?,?,?,?,?,?)',('PR-8001','Replenish critical electrical spares',u['store'],s['NCS-01'],'Submitted','Stock reached reorder threshold.',1340,now()))
            pr=cur.lastrowid
            conn.execute('INSERT INTO purchase_requisition_items(pr_id,inventory_item_id,description,quantity,estimated_unit_cost) VALUES(?,?,?,?,?)',(pr,i['BRK-COIL-11KV'],'11 kV Breaker Trip Coil',3,410))

        if conn.execute('SELECT COUNT(*) FROM contracts').fetchone()[0] == 0:
            v={r['vendor_code']:r['id'] for r in conn.execute('SELECT id,vendor_code FROM vendors')}
            conn.execute('INSERT INTO contracts(contract_no,title,vendor_id,start_date,end_date,value,status) VALUES(?,?,?,?,?,?,?)',('CTR-4001','New Cairo Electrical OEM Support',v['VND-ABB'],'2026-01-01','2027-12-31',185000,'Active'))

        if conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 0:
            a={r['asset_no']:r['id'] for r in conn.execute('SELECT id,asset_no FROM assets')};u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            conn.executemany('INSERT INTO documents(document_no,title,category,file_name,stored_name,mime_type,asset_id,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?)',[
              ('DOC-6001','TR-001 Transformer Datasheet','Datasheet','','','',a['TR-001'],u['omar'],now()),
              ('DOC-6002','TR-001 Quarterly Inspection Report','Report','','','',a['TR-001'],u['planner'],now())])

        if conn.execute('SELECT COUNT(*) FROM maintenance_cost_ledger').fetchone()[0] == 0:
            system=conn.execute("SELECT id FROM users WHERE username='system'").fetchone()
            poster=system['id'] if system else conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()['id']
            seq=1
            for w in conn.execute('SELECT id,wo_no,asset_id,actual_cost,created_at FROM work_orders WHERE actual_cost>0 ORDER BY id').fetchall():
                conn.execute('INSERT INTO maintenance_cost_ledger(entry_no,work_order_id,asset_id,cost_type,amount,quantity,reference,posted_by,posted_at) VALUES(?,?,?,?,?,?,?,?,?)',(f'COST-{seq:06d}',w['id'],w['asset_id'],'Historical',w['actual_cost'],1,w['wo_no'],poster,w['created_at']))
                seq+=1

        if conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0:
            u={r['username']:r['id'] for r in conn.execute('SELECT id,username FROM users')}
            conn.executemany('INSERT INTO notifications(user_id,role_code,title,message,severity,link_module,link_id,is_read,created_at) VALUES(?,?,?,?,?,?,?,?,?)',[
              (u['tech1'],None,'Work order assigned','WO-10025 — Investigate Transformer Oil Temperature','High','work','WO-10025',0,now()),
              (u['planner'],None,'PM overdue','Transformer Quarterly Inspection is due.','Warning','maintenance','PM-TR-001',0,now()),
              (u['proc'],None,'Approval waiting','PR-8001 is waiting for procurement action.','Info','procurement','PR-8001',0,now()),
              (None,None,'Critical asset condition','TR-001 transformer is in Warning condition at New Cairo Substation.','High','assets','TR-001',0,now())])

        if conn.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0] == 0:
            pr=conn.execute("SELECT * FROM purchase_requisitions WHERE pr_no='PR-8001'").fetchone();store=conn.execute("SELECT id FROM users WHERE username='store'").fetchone()
            if pr and store:
                conn.execute("INSERT INTO approval_requests(approval_no,module,record_type,record_id,record_code,title,requested_by,assigned_role,status,requested_at) VALUES(?,?,?,?,?,?,?,'procurement','Pending',?)",('APR-9001','Procurement','purchase_requisition',pr['id'],pr['pr_no'],f"Approve {pr['pr_no']} — {pr['title']}",store['id'],now()))
                conn.execute("INSERT INTO workflow_events(module,record_type,record_id,record_code,event,from_status,to_status,actor_id,notes,created_at) VALUES(?,?,?,?,?,'','Submitted',?,'Seeded demo workflow',?)",('Procurement','purchase_requisition',pr['id'],pr['pr_no'],'SUBMIT',store['id'],now()))
