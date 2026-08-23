from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source_test_definitions() -> int:
    """Count test definitions from source without importing the test suite."""
    total = 0
    for path in sorted((ROOT / 'tests').rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith('test_')
        )
    return total


def _api_route_metrics(app) -> tuple[int, int]:
    paths: set[str] = set()
    method_pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = str(getattr(route, 'path', '') or '')
        if not path.startswith('/api/'):
            continue
        paths.add(path)
        for method in set(getattr(route, 'methods', set()) or set()):
            method = str(method).upper()
            if method not in {'HEAD', 'OPTIONS'}:
                method_pairs.add((path, method))
    return len(paths), len(method_pairs)


def collect_evidence() -> dict:
    """Collect deterministic runtime evidence against an isolated fresh SQLite DB.

    The collector deliberately ignores any caller production database URL. This
    makes it safe to run in CI and on operator workstations while still executing
    the real schema initializer and FastAPI composition layer.
    """
    with tempfile.TemporaryDirectory(prefix='euas-evidence-') as temp_dir:
        os.environ['EUAS_DB_PATH'] = str(Path(temp_dir) / 'euas-evidence.db')
        os.environ.pop('EUAS_DATABASE_URL', None)
        os.environ['EUAS_ENV'] = 'development'
        os.environ['EUAS_AUTOMATION_INTERVAL_MINUTES'] = '0'

        # Imports intentionally occur only after isolation environment is fixed.
        from app.auth import hash_password
        from app.config import APP_VERSION, DB_BACKEND, SCHEMA_VERSION
        from app.database import db, init_db
        from app.main import app

        init_db(hash_password)
        api_routes, api_route_methods = _api_route_metrics(app)
        with db() as conn:
            tables = int(
                conn.execute(
                    """SELECT COUNT(*) FROM sqlite_master
                       WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
                ).fetchone()[0]
            )
            explicit_indexes = int(
                conn.execute(
                    """SELECT COUNT(*) FROM sqlite_master
                       WHERE type='index' AND sql IS NOT NULL
                         AND name NOT LIKE 'sqlite_%'"""
                ).fetchone()[0]
            )

        return {
            'application_version': APP_VERSION,
            'schema_version': int(SCHEMA_VERSION),
            'database_backend': DB_BACKEND,
            'api_routes': api_routes,
            'api_route_methods': api_route_methods,
            'relational_tables': tables,
            'explicit_indexes': explicit_indexes,
            'source_test_definitions': _source_test_definitions(),
        }


def _load_snapshot(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise SystemExit(f'evidence snapshot not found: {path}') from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f'evidence snapshot is invalid JSON: {path}: {exc}') from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Collect or verify deterministic EUAS engineering evidence.'
    )
    parser.add_argument('--write', type=Path, help='write evidence JSON to this path')
    parser.add_argument('--check', type=Path, help='fail if this snapshot differs')
    args = parser.parse_args()

    evidence = collect_evidence()
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + '\n'

    if args.write:
        args.write.write_text(rendered, encoding='utf-8')

    if args.check:
        expected = _load_snapshot(args.check)
        if expected != evidence:
            print('engineering evidence drift detected', file=sys.stderr)
            print('expected:', json.dumps(expected, sort_keys=True), file=sys.stderr)
            print('actual:  ', json.dumps(evidence, sort_keys=True), file=sys.stderr)
            raise SystemExit(1)

    print(rendered, end='')


if __name__ == '__main__':
    main()
