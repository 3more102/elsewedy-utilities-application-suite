"""Run one auditable EUAS automation cycle.

Designed for cron, Task Scheduler, Kubernetes CronJob, or manual operations use.
It uses the inactive internal `system` principal for audit ownership.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth import hash_password
from app.database import db, init_db
from app.main import _execute_automation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of', help='Optional YYYY-MM-DD business date')
    args = parser.parse_args()

    init_db(hash_password)
    with db() as conn:
        actor = conn.execute("SELECT id FROM users WHERE username='system'").fetchone()
        if not actor:
            raise SystemExit('EUAS system principal is missing')
        result = _execute_automation(conn, actor['id'], 'external-scheduler', args.as_of)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'Succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
