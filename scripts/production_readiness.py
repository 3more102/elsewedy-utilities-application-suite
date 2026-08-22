"""EUAS production-readiness gate.

This command is intentionally deterministic and dependency-light so it can be
used locally, in CI, or as a deployment preflight.

Examples:
    python scripts/production_readiness.py
    EUAS_ENV=production EUAS_DATABASE_URL=postgresql://... \
      python scripts/production_readiness.py --strict-production --require-postgres --check-db
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


def _as_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def evaluate_configuration(
    env: Mapping[str, str], *, require_postgres: bool = False, strict_production: bool = False
) -> list[Check]:
    checks: list[Check] = []
    environment = env.get("EUAS_ENV", "development").strip().lower()
    database_url = env.get("EUAS_DATABASE_URL", "").strip()
    webhook_url = env.get("EUAS_EVENT_WEBHOOK_URL", "").strip()
    webhook_secret = env.get("EUAS_EVENT_WEBHOOK_SECRET", "").strip()
    automation_minutes = _as_int(env, "EUAS_AUTOMATION_INTERVAL_MINUTES", 0)
    session_hours = _as_int(env, "EUAS_SESSION_HOURS", 12)
    upload_mb = _as_int(env, "EUAS_MAX_UPLOAD_MB", 25)

    if strict_production:
        checks.append(
            Check(
                "environment",
                "PASS" if environment == "production" else "FAIL",
                f"EUAS_ENV={environment!r}; strict mode requires 'production'.",
            )
        )
    else:
        checks.append(Check("environment", "PASS", f"EUAS_ENV={environment!r}."))

    is_postgres = database_url.startswith(("postgresql://", "postgres://"))
    if require_postgres:
        checks.append(
            Check(
                "database_backend",
                "PASS" if is_postgres else "FAIL",
                "PostgreSQL URL configured." if is_postgres else "EUAS_DATABASE_URL must be PostgreSQL.",
            )
        )
    else:
        checks.append(
            Check(
                "database_backend",
                "PASS" if is_postgres else "WARN",
                "PostgreSQL configured." if is_postgres else "SQLite/reference mode configured.",
            )
        )

    if webhook_url and not webhook_secret:
        checks.append(Check("webhook_signing", "FAIL", "Webhook URL is set but signing secret is empty."))
    elif webhook_url:
        checks.append(Check("webhook_signing", "PASS", "Webhook delivery has a signing secret."))
    else:
        checks.append(Check("webhook_signing", "WARN", "No outbound webhook target configured."))

    checks.append(
        Check(
            "automation_scheduler",
            "PASS" if automation_minutes > 0 else "WARN",
            f"Automation interval is {automation_minutes} minute(s)."
            if automation_minutes > 0
            else "In-process scheduler disabled; an external scheduler/worker must trigger automation.",
        )
    )

    checks.append(
        Check(
            "session_lifetime",
            "PASS" if 1 <= session_hours <= 24 else "WARN",
            f"Session lifetime is {session_hours} hour(s).",
        )
    )
    checks.append(
        Check(
            "upload_limit",
            "PASS" if 1 <= upload_mb <= 100 else "WARN",
            f"Maximum upload size is {upload_mb} MiB.",
        )
    )
    return checks


def run_database_checks() -> list[Check]:
    # Import after CLI/environment handling so app.config sees deployment values.
    from app.auth import hash_password
    from app.config import DB_BACKEND, SCHEMA_VERSION
    from app.database import db, init_db

    checks: list[Check] = []
    try:
        init_db(hash_password)
    except Exception as exc:  # pragma: no cover - exercised by deployment/CI failures
        return [Check("database_initialization", "FAIL", f"Schema initialization failed: {exc}")]

    critical_tables = {
        "users",
        "assets",
        "work_orders",
        "inventory_items",
        "audit_logs",
        "automation_runs",
        "event_outbox",
        "telemetry_channels",
        "telemetry_readings",
        "operational_alarms",
    }
    try:
        with db() as conn:
            if DB_BACKEND == "postgresql":
                rows = conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                ).fetchall()
                present = {r["table_name"] for r in rows}
            else:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                present = {r["name"] for r in rows}

            missing = sorted(critical_tables - present)
            checks.append(
                Check(
                    "critical_tables",
                    "FAIL" if missing else "PASS",
                    f"Missing: {', '.join(missing)}" if missing else f"All {len(critical_tables)} critical tables present.",
                )
            )
            user_count = int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()[0])
            asset_count = int(conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()[0])
            checks.append(Check("seed_integrity", "PASS" if user_count and asset_count else "FAIL", f"users={user_count}, assets={asset_count}"))
            checks.append(Check("schema_contract", "PASS", f"Application schema contract version={SCHEMA_VERSION}."))
            checks.append(Check("database_connectivity", "PASS", f"Connected through EUAS {DB_BACKEND} adapter."))
    except Exception as exc:  # pragma: no cover - exercised by deployment/CI failures
        checks.append(Check("database_connectivity", "FAIL", str(exc)))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate EUAS deployment readiness.")
    parser.add_argument("--require-postgres", action="store_true", help="Fail unless PostgreSQL is configured.")
    parser.add_argument("--strict-production", action="store_true", help="Require EUAS_ENV=production.")
    parser.add_argument("--check-db", action="store_true", help="Initialize and validate the configured database.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    checks = evaluate_configuration(
        os.environ,
        require_postgres=args.require_postgres,
        strict_production=args.strict_production,
    )
    if args.check_db:
        checks.extend(run_database_checks())

    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{check.status:4} {check.name}: {check.detail}")

    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
