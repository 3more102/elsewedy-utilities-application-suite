"""EUAS schema migration command.

Examples:
    python scripts/migrate.py status
    python scripts/migrate.py upgrade
    python scripts/migrate.py check

`upgrade` bootstraps the historical v9 base when needed, then executes the
registered migration chain under the database migration lock. `check` is
read-only and exits non-zero unless the configured database exactly satisfies
the application schema contract.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _status() -> dict:
    from app.config import DB_BACKEND, SCHEMA_VERSION
    from app.database import db
    from app.migrations import migration_status

    with db() as conn:
        return migration_status(
            conn,
            backend=DB_BACKEND,
            target_version=SCHEMA_VERSION,
        )


def _render(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    status = payload.get('status', payload)
    print(
        'schema '
        f"current={status['current_version']} target={status['target_version']} "
        f"ready={str(status['ready']).lower()}"
    )
    print(f"applied={status['applied_versions']}")
    print(f"pending={status['pending_versions']}")
    print(f"invalid={status['invalid_versions']}")
    print(f"future={status['future_versions']}")
    if status.get('unregistered_versions'):
        print(f"unregistered={status['unregistered_versions']}")
    if 'applied' in payload:
        print(f"applied_now={payload['applied']}")
        print(f"repaired_now={payload['repaired']}")
        print(f"skipped={payload['skipped']}")


def main() -> int:
    parser = argparse.ArgumentParser(description='Inspect or advance the EUAS schema.')
    parser.add_argument(
        'action',
        choices=('status', 'upgrade', 'check'),
        nargs='?',
        default='status',
    )
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON.')
    args = parser.parse_args()

    try:
        if args.action == 'upgrade':
            from app.auth import hash_password
            from app.migrations import initialize_database

            payload = initialize_database(hash_password)
            _render(payload, args.json)
            return 0

        status = _status()
        _render(status, args.json)
        if args.action == 'check':
            return 0 if status['ready'] else 1
        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, indent=2, sort_keys=True))
        else:
            print(f'migration error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
