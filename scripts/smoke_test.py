"""Clean-process HTTP smoke test for EUAS.

Starts Uvicorn against a temporary SQLite database, validates health/security
headers, authenticates with the seeded admin account, reads the dashboard and
loads the application shell, then removes the temporary database.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8877
BASE = f"http://{HOST}:{PORT}"


def request(path: str, method: str = "GET", data=None, token: str | None = None):
    headers = {}
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=payload, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        raw = response.read()
        ctype = response.headers.get("Content-Type", "")
        body = json.loads(raw) if "json" in ctype else raw.decode(errors="replace")
        return response.status, response.headers, body


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="euas-smoke-") as td:
        db_path = Path(td) / "smoke.db"
        env = os.environ.copy()
        env["EUAS_DB_PATH"] = str(db_path)
        env["EUAS_ENV"] = "test"
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    status, headers, health = request("/api/health")
                    if status == 200:
                        break
                except Exception:
                    time.sleep(0.25)
            else:
                raise RuntimeError("EUAS did not become healthy within 20 seconds")

            assert health["status"] == "ok"
            assert health["database_backend"] == "sqlite"
            assert health["schema_version"] >= 9
            assert headers.get("X-Frame-Options") == "DENY"
            assert headers.get("X-Content-Type-Options") == "nosniff"
            assert headers.get("X-Request-ID")

            status, _, login = request(
                "/api/auth/login",
                method="POST",
                data={"username": "omar", "password": "EUAS@2026"},
            )
            assert status == 200 and login["user"]["role"] == "admin"
            token = login["token"]

            status, _, ready = request("/api/health/ready")
            assert status == 200 and ready["status"] == "ready"

            status, _, dashboard = request("/api/dashboard", token=token)
            assert status == 200
            assert dashboard["kpis"]["total_assets"] >= 1

            status, _, approvals = request("/api/approvals?status=Pending", token=token)
            assert status == 200 and isinstance(approvals, list)

            status, _, automation = request("/api/automation/status", token=token)
            assert status == 200 and "queue" in automation and "sla_breaches" in automation["queue"]

            status, _, sla = request("/api/sla/summary", token=token)
            assert status == 200 and "compliance_percent" in sla

            status, _, outbox = request("/api/events/outbox?limit=5", token=token)
            assert status == 200 and isinstance(outbox, list)

            status, _, health_portfolio = request("/api/assets/health", token=token)
            assert status == 200 and health_portfolio["assets"] and 0 <= health_portfolio["average_score"] <= 100

            status, _, forecast = request("/api/planning/maintenance-forecast?horizon_days=90", token=token)
            assert status == 200 and forecast["weeks"] and forecast["technicians"] >= 1

            status, _, delegations = request("/api/approval-delegations", token=token)
            assert status == 200 and isinstance(delegations, list)

            status, _, workforce = request("/api/workforce/technicians", token=token)
            assert status == 200 and len(workforce) >= 2

            status, _, capacity = request("/api/workforce/capacity?weeks=4", token=token)
            assert status == 200 and capacity["weeks"] and capacity["weeks"][0]["source"] == "workforce_schedule"

            status, _, reliability = request("/api/reliability/assets?period_days=365", token=token)
            assert status == 200 and reliability["assets"]
            assert any(x.get("downtime_source") == "outage_events" for x in reliability["assets"])

            status, _, outages = request("/api/outages", token=token)
            assert status == 200 and isinstance(outages, list) and outages

            status, _, dispatch = request("/api/dispatch", token=token)
            assert status == 200 and isinstance(dispatch, list) and dispatch

            status, _, dispatch_board = request("/api/dispatch/board", token=token)
            assert status == 200 and len(dispatch_board["technicians"]) >= 2

            status, _, rel_sites = request("/api/reliability/sites?period_days=365", token=token)
            assert status == 200 and rel_sites["sites"]

            tr = next(x for x in health_portfolio["assets"] if x["asset_no"] == "TR-001")
            status, _, work = request("/api/work-orders", token=token)
            assert status == 200 and any(x["wo_no"] == "WO-10025" for x in work)
            demo_wo = next(x for x in work if x["wo_no"] == "WO-10025")
            status, _, reservations = request(f"/api/work-orders/{demo_wo['id']}/reservations", token=token)
            assert status == 200 and isinstance(reservations, list) and reservations

            status, _, launchpad = request("/api/launchpad", token=token)
            assert status == 200 and any(x["code"] == "dispatch" for x in launchpad)
            assert any(x["code"] == "telemetry" for x in launchpad)
            assert "open_outages" in dashboard["kpis"] and "active_dispatches" in dashboard["kpis"]
            assert "active_alarms" in dashboard["kpis"] and "critical_alarms" in dashboard["kpis"]

            status, _, channels = request("/api/telemetry/channels", token=token)
            assert status == 200 and isinstance(channels, list) and len(channels) >= 3

            status, _, alarms = request("/api/alarms", token=token)
            assert status == 200 and isinstance(alarms, list) and alarms

            status, _, intelligence = request("/api/operations/intelligence", token=token)
            assert status == 200
            assert intelligence["telemetry_channels"] >= 3
            assert intelligence["active_alarms"] >= 1

            status, _, shell = request("/")
            assert status == 200 and "ELSEWEDY UTILITIES" in shell

            print(
                f"PASS EUAS HTTP smoke: version={health['version']} "
                f"assets={dashboard['kpis']['total_assets']} "
                f"open_work={dashboard['kpis']['open_work_orders']}"
            )
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.returncode not in (0, -15, None):
                output = process.stdout.read() if process.stdout else ""
                if output:
                    print(output, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
