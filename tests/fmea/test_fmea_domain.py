import sqlite3
import pytest
from apps.fmea import FmeaError, calculate_risk, would_create_failure_mode_cycle


def test_rpn_ranges_and_validation():
    assert calculate_risk(10, 10, 3) == (300, 'Critical')
    assert calculate_risk(4, 4, 5) == (80, 'Medium')
    assert calculate_risk(2, 2, 2) == (8, 'Low')
    with pytest.raises(FmeaError):
        calculate_risk(0, 5, 5)


def test_failure_mode_cycle_detection():
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE failure_modes(id INTEGER PRIMARY KEY,parent_id INTEGER)')
    conn.executemany('INSERT INTO failure_modes VALUES(?,?)', [(1,None),(2,1),(3,2)])
    assert would_create_failure_mode_cycle(conn, 1, 3) is True
    assert would_create_failure_mode_cycle(conn, 3, 1) is False
