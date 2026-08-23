from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _python_files(root: Path):
    return sorted(path for path in root.rglob('*.py') if '__pycache__' not in path.parts)


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ''


def test_domain_apps_never_import_monolithic_main_and_core_never_imports_domains():
    violations = []
    for path in _python_files(ROOT / 'apps'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for module in _imports(tree):
            if module == 'app.main' or module.startswith('app.main.'):
                violations.append(f'{path.relative_to(ROOT)} imports {module}')
    for path in _python_files(ROOT / 'core'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for module in _imports(tree):
            if module == 'apps' or module.startswith('apps.'):
                violations.append(f'{path.relative_to(ROOT)} imports {module}')
    assert violations == []


def test_compatibility_shims_remain_import_only_surfaces():
    shims = [
        ROOT / 'app/audit_store.py', ROOT / 'app/event_store.py', ROOT / 'app/auth.py',
        ROOT / 'app/config.py', ROOT / 'app/database.py',
    ]
    violations = []
    for path in shims:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        executable_defs = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if executable_defs:
            violations.append(f'{path.relative_to(ROOT)} defines business/executable symbols')
    assert violations == []


def test_api_layer_contains_no_direct_database_execute_calls():
    violations = []
    for path in _python_files(ROOT / 'api'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'execute', 'executemany', 'executescript'}:
                violations.append(f'{path.relative_to(ROOT)}:{getattr(node, "lineno", "?")} calls {node.func.attr}')
    assert violations == []
