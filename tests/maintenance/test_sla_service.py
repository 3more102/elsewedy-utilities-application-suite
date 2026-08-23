import sqlite3
from datetime import datetime, timedelta

from apps.maintenance import ensure_work_sla, mark_sla_resolution, mark_sla_response


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE work_orders(id INTEGER PRIMARY KEY,priority TEXT,status TEXT,created_at TEXT,actual_start TEXT,actual_finish TEXT);
      CREATE TABLE sla_policies(id INTEGER PRIMARY KEY,priority TEXT,response_minutes INTEGER,resolution_minutes INTEGER,active INTEGER);
      CREATE TABLE work_order_sla(
        work_order_id INTEGER PRIMARY KEY,policy_id INTEGER,response_due TEXT,resolution_due TEXT,first_response_at TEXT,resolved_at TEXT,
        response_status TEXT,resolution_status TEXT,updated_at TEXT);
      INSERT INTO sla_policies VALUES(1,'High',60,240,1);
    ''')
    return conn


def test_sla_due_dates_and_response_resolution_marking():
    conn = _conn()
    created = datetime(2026, 8, 23, 8, 0, 0)
    conn.execute("INSERT INTO work_orders VALUES(1,'High','Submitted',?,NULL,NULL)", (created.isoformat(timespec='seconds'),))
    sla = ensure_work_sla(conn, 1)
    assert sla['response_due'] == (created + timedelta(minutes=60)).isoformat(timespec='seconds')
    assert sla['resolution_due'] == (created + timedelta(minutes=240)).isoformat(timespec='seconds')
    mark_sla_response(conn, 1, (created + timedelta(minutes=30)).isoformat(timespec='seconds'))
    mark_sla_resolution(conn, 1, (created + timedelta(minutes=300)).isoformat(timespec='seconds'))
    row = conn.execute('SELECT * FROM work_order_sla WHERE work_order_id=1').fetchone()
    assert row['response_status'] == 'Met' and row['resolution_status'] == 'Breached'
