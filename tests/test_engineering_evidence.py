from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'engineering_evidence.py'


def test_engineering_evidence_is_isolated_and_machine_readable():
    env = dict(os.environ)
    # The evidence command must never connect to a caller production database.
    # A deliberately unusable PostgreSQL URL proves the collector overrides it
    # before importing the application/database configuration.
    env['EUAS_DATABASE_URL'] = 'postgresql://invalid:invalid@127.0.0.1:1/forbidden'
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    evidence = json.loads(completed.stdout)

    assert evidence['database_backend'] == 'sqlite'
    assert evidence['schema_version'] >= 1
    assert evidence['api_routes'] > 0
    assert evidence['api_route_methods'] >= evidence['api_routes']
    assert evidence['relational_tables'] > 0
    assert evidence['explicit_indexes'] > 0
    assert evidence['source_test_definitions'] > 0


def test_engineering_evidence_check_detects_snapshot_drift(tmp_path):
    snapshot = tmp_path / 'stale-evidence.json'
    snapshot.write_text('{"application_version":"stale"}\n', encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), '--check', str(snapshot)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 1
    assert 'engineering evidence drift detected' in completed.stderr
    assert 'actual:' in completed.stderr
