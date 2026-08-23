import sqlite3
import pytest
from apps.rcm import RcmError, default_review_due, review_days, validate_payload


def _conn():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('''
      CREATE TABLE cbm_rules(id INTEGER PRIMARY KEY,asset_fmea_id INTEGER,active INTEGER);
      CREATE TABLE maintenance_plans(id INTEGER PRIMARY KEY,asset_id INTEGER,active INTEGER);
    ''')
    return conn


def test_review_cadence_and_strategy_guards():
    assert review_days('Critical') == 90 and review_days('Low') == 730
    assert default_review_due({'risk_band':'Critical'})
    conn=_conn(); fmea={'id':7,'asset_id':3,'status':'Active'}
    with pytest.raises(RcmError):
        validate_payload(conn,fmea,{'consequence_classification':'Safety','strategy_type':'Run-to-Failure'})
    with pytest.raises(RcmError):
        validate_payload(conn,fmea,{'consequence_classification':'Operational','strategy_type':'Condition-Based'},require_ready=True)
