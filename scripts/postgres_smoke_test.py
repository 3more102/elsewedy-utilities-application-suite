"""Live PostgreSQL HTTP smoke test for EUAS.

Requires EUAS_DATABASE_URL to point at an already-running PostgreSQL instance.
The script starts EUAS in a child Uvicorn process and validates startup,
authentication, representative reads, generated-ID writes, telemetry/alarm
persistence, automation and metrics.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = '127.0.0.1'
PORT = 8878
BASE = f'http://{HOST}:{PORT}'


def request(path: str, method: str = 'GET', data=None, token: str | None = None):
    headers = {}
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(BASE + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read()
            ctype = response.headers.get('Content-Type', '')
            body = json.loads(raw) if 'json' in ctype else raw.decode(errors='replace')
            return response.status, response.headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='replace')
        raise RuntimeError(f'{method} {path} returned HTTP {exc.code}: {body}') from exc


def main() -> int:
    url = os.getenv('EUAS_DATABASE_URL', '').strip()
    if not url.startswith(('postgresql://', 'postgres://')):
        print('FAIL: EUAS_DATABASE_URL must point to PostgreSQL.', file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.setdefault('EUAS_ENV', 'test')
    env['EUAS_AUTOMATION_INTERVAL_MINUTES'] = '0'
    # Inherit stdout/stderr so any Uvicorn/FastAPI traceback is visible directly
    # in CI. A failed HTTP smoke must leave enough evidence to diagnose the
    # backend path instead of only reporting a generic HTTP 500.
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', HOST, '--port', str(PORT)],
        cwd=ROOT,
        env=env,
    )

    try:
        deadline = time.time() + 40
        health = None
        headers = None
        while time.time() < deadline:
            try:
                status, headers, health = request('/api/health')
                if status == 200:
                    break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError(f'EUAS exited during startup with code {process.returncode}')
                time.sleep(0.5)
        else:
            raise RuntimeError('EUAS did not become healthy against PostgreSQL within 40 seconds')

        assert health['status'] == 'ok'
        assert health['database_backend'] == 'postgresql'
        assert health['schema_version'] >= 9
        assert headers.get('X-Request-ID')
        assert headers.get('X-Content-Type-Options') == 'nosniff'

        status, _, ready = request('/api/health/ready')
        assert status == 200 and ready['status'] == 'ready'

        status, _, login = request(
            '/api/auth/login',
            method='POST',
            data={'username': 'omar', 'password': 'EUAS@2026'},
        )
        assert status == 200 and login['user']['role'] == 'admin'
        token = login['token']

        status, _, dashboard = request('/api/dashboard', token=token)
        assert status == 200 and dashboard['kpis']['total_assets'] >= 1

        # Prove ordinary generated-ID CRUD, not only seeded read paths.
        status, _, assets = request('/api/assets', token=token)
        assert status == 200 and len(assets) >= 2
        transformer = next(x for x in assets if x['asset_no'] == 'TR-001')
        breaker = next(x for x in assets if x['asset_no'] == 'CB-101')

        status, _, work = request(
            '/api/work-orders',
            method='POST',
            token=token,
            data={
                'title': 'PostgreSQL CI transactional smoke',
                'asset_id': transformer['id'],
                'priority': 'Medium',
                'work_type': 'Corrective Maintenance',
                'estimated_hours': 1.5,
            },
        )
        assert status == 200 and int(work['id']) > 0
        work_id = int(work['id'])
        status, _, work_detail = request(f'/api/work-orders/{work_id}', token=token)
        assert status == 200 and work_detail['title'] == 'PostgreSQL CI transactional smoke'

        # Prove time-series insertion, threshold evaluation and alarm/work linkage.
        channel_code = 'PG-CI-CB101-CURRENT'
        status, _, channel = request(
            '/api/telemetry/channels',
            method='POST',
            token=token,
            data={
                'channel_code': channel_code,
                'asset_id': breaker['id'],
                'name': 'PostgreSQL CI breaker current',
                'metric_type': 'Current',
                'unit': 'A',
                'source_system': 'GitHub Actions PostgreSQL',
                'warning_high': 50,
                'critical_high': 75,
            },
        )
        assert status == 200 and int(channel['id']) > 0
        channel_id = int(channel['id'])

        status, _, ingestion = request(
            '/api/telemetry/ingest',
            method='POST',
            token=token,
            data={'readings': [{'channel_code': channel_code, 'value': 60, 'quality': 'Good'}]},
        )
        assert status == 200 and ingestion['alarms_opened'] == 1
        alarm_id = int(ingestion['results'][0]['alarm_id'])

        status, _, readings = request(
            f'/api/telemetry/readings?channel_id={channel_id}&hours=24', token=token
        )
        assert status == 200 and readings and readings[0]['channel_code'] == channel_code

        status, _, alarm_work = request(
            f'/api/alarms/{alarm_id}/work-order', method='POST', token=token, data={}
        )
        assert status == 200 and int(alarm_work['id']) > 0
        status, _, linked_work = request(f"/api/work-orders/{alarm_work['id']}", token=token)
        assert status == 200 and linked_work['status'] == 'Submitted'

        status, _, channels = request('/api/telemetry/channels', token=token)
        assert status == 200 and any(x['id'] == channel_id for x in channels)

        status, _, alarms = request('/api/alarms', token=token)
        assert status == 200 and any(x['id'] == alarm_id for x in alarms)

        status, _, automation = request('/api/automation/run', method='POST', token=token)
        assert status == 200 and automation['status'] == 'Succeeded'

        status, _, metrics = request('/api/metrics', token=token)
        assert status == 200 and 'euas_requests_total' in metrics

        print(
            f"PASS EUAS PostgreSQL smoke: version={health['version']} "
            f"assets={dashboard['kpis']['total_assets']} work_id={work_id} "
            f"channel_id={channel_id} alarm_id={alarm_id} backend={health['database_backend']}"
        )
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
