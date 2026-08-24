from __future__ import annotations

from fastapi.testclient import TestClient

from app.application import _csv_safe_cell, csv_response
from app.main import app  # noqa: F401  (installs hardened auth routes)


def _login(username: str, password: str) -> dict[str, str]:
    with TestClient(app) as client:
        response = client.post(
            '/api/auth/login', json={'username': username, 'password': password}
        )
        assert response.status_code == 200, response.text
        return {'Authorization': f"Bearer {response.json()['token']}"}


def test_csv_safe_cell_neutralizes_formula_prefixes_only():
    hostile = [
        ('=cmd|\' /c calc\'!A0', "'=cmd|' /c calc'!A0"),
        ('+1+2', "'+1+2"),
        ('-2-3', "'-2-3"),
        ('@SUM(A1)', "'@SUM(A1)"),
        ('\tTAB', "'\tTAB"),
    ]
    for raw, expected in hostile:
        assert _csv_safe_cell(raw) == expected

    # Benign values are never altered.
    assert _csv_safe_cell('Transformer T-100') == 'Transformer T-100'
    assert _csv_safe_cell('wo_no WO-10026') == 'wo_no WO-10026'
    assert _csv_safe_cell(42.5) == 42.5
    assert _csv_safe_cell(None) is None
    assert _csv_safe_cell('a=b in the middle') == 'a=b in the middle'


def test_csv_response_escapes_hostile_cells():
    import asyncio

    response = csv_response(
        'unit.csv',
        ['Code', 'Name'],
        [['AST-1', '=HYPERLINK("http://evil.test","pwn")']],
    )

    async def _read():
        return ''.join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(_read())
    line = body.splitlines()[1]
    assert "'=HYPERLINK" in line


def test_asset_export_rejects_anonymous_and_escapes_hostile_names():
    with TestClient(app) as client:
        assert client.get('/api/assets-export.csv').status_code == 401

        headers = _login('planner', 'Planner@2026')
        created = client.post(
            '/api/assets',
            headers=headers,
            json={'name': "=cmd|' /c calc'!A0", 'category': 'PUMP'},
        )
        assert created.status_code == 200, created.text
        asset_id = created.json()['id']

        export = client.get('/api/assets-export.csv', headers=headers)
        assert export.status_code == 200, export.text
        rows = [
            row
            for row in export.text.splitlines()
            if row.startswith('AST-')
        ]
        assert any("'=cmd" in row for row in rows), rows[:3]

        # The stored business record itself is untouched; only the export
        # representation is neutralized.
        detail = client.get(f'/api/assets/{asset_id}', headers=headers)
        assert detail.json()['name'] == "=cmd|' /c calc'!A0"

        delete = client.delete(
            f'/api/assets/{asset_id}', headers=_login('omar', 'EUAS@2026')
        )
        assert delete.status_code in (200, 409), delete.text


def test_work_order_export_escapes_hostile_titles():
    with TestClient(app) as client:
        headers = _login('planner', 'Planner@2026')
        assets = client.get('/api/assets', headers=headers)
        asset_id = assets.json()[0]['id']
        created = client.post(
            '/api/work-orders',
            headers=headers,
            json={
                'title': '=WEBSERVICE("http://evil.test")',
                'asset_id': asset_id,
                'priority': 'Low',
                'work_type': 'Corrective',
            },
        )
        assert created.status_code == 200, created.text
        wo_id = created.json().get('id')

        export = client.get('/api/exports/work-orders.csv', headers=headers)
        assert export.status_code == 200, export.text
        assert "'=WEBSERVICE" in export.text

        if wo_id is not None:
            client.post(
                f'/api/work-orders/{wo_id}/transition',
                headers=headers,
                json={'to_status': 'Cancelled'},
            )
