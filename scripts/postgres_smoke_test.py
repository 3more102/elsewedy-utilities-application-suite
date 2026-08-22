"""Live PostgreSQL HTTP smoke test for EUAS.

Requires EUAS_DATABASE_URL to point at an already-running PostgreSQL instance.
The script starts EUAS in a child Uvicorn process, validates application startup,
authentication and representative read/write paths, then exits non-zero on any
contract failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
    with urllib.request.urlopen(req, timeout=8) as response:
        raw = response.read()
        ctype = response.headers.get('Content-Type', '')
        body = json.loads(raw) if 'json' in ctype else raw.decode(errors='replace')
        return response.status, response.headers, body


def main() -> int:
    url = os.getenv('EUAS_DATABASE_URL', '').strip()
    if not url.startswith(('postgresql://', 'postgres://')):
        print('FAIL: EUAS_DATABASE_URL must point to PostgreSQL.', file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.setdefault('EUAS_ENV', 'test')
    env['EUAS_AUTOMATION_INTERVAL_MINUTES'] = '0'
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', HOST, '--port', str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
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

        status, _, channels = request('/api/telemetry/channels', token=token)
        assert status == 200 and len(channels) >= 3

        status, _, alarms = request('/api/alarms', token=token)
        assert status == 200 and isinstance(alarms, list)

        status, _, automation = request('/api/automation/run', method='POST', token=token)
        assert status == 200 and automation['status'] == 'Succeeded'

        status, _, metrics = request('/api/metrics', token=token)
        assert status == 200 and 'euas_requests_total' in metrics

        print(
            f"PASS EUAS PostgreSQL smoke: version={health['version']} "
            f"assets={dashboard['kpis']['total_assets']} backend={health['database_backend']}"
        )
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.returncode not in (0, -15, None):
            output = process.stdout.read() if process.stdout else ''
            if output:
                print(output, file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
