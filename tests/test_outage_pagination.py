from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _auth(client):
    r = client.post(
        '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
    )
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_outages(client, headers, count: int, asset_id: int) -> None:
    base = datetime.now() - timedelta(hours=count + 1)
    for index in range(count):
        start = (base + timedelta(hours=index)).isoformat(timespec='seconds')
        created = client.post(
            '/api/outages',
            headers=headers,
            json={
                'asset_id': asset_id,
                'outage_type': 'Planned',
                'cause_code': f'PAGE-{uuid.uuid4().hex[:8]}',
                'start_at': start,
            },
        )
        assert created.status_code == 200, created.text


def _paged(client, headers, **params):
    response = client.get('/api/outages', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_outage_pagination_is_deterministic_and_additive():
    with TestClient(app) as client:
        headers = _auth(client)
        asset = next(x for x in client.get('/api/assets', headers=headers).json())
        with db() as conn:
            conn.execute('DELETE FROM asset_outages WHERE asset_id=?', (asset['id'],))
        total = 7
        _seed_outages(client, headers, total, int(asset['id']))

        # Legacy-shaped request (filters only) still works and is bounded by
        # the same default convention as /api/alarms.
        default_view = _paged(client, headers)
        assert len(default_view) <= 200
        seeded_ids = {
            x['id']
            for x in _paged(client, headers, asset_id=int(asset['id']), limit=1000)
        }
        assert seeded_ids.issubset({x['id'] for x in default_view})

        page_one = _paged(client, headers, limit=3, offset=0)
        page_two = _paged(client, headers, limit=3, offset=3)
        assert len(page_one) == 3 and len(page_two) == 3

        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert not set(ids_one) & set(ids_two)

        full = _paged(client, headers, limit=1000, offset=0)
        mine = [x for x in full if x['asset_id'] == asset['id']]
        assert [x['id'] for x in mine] == ids_one + ids_two + [
            x['id'] for x in mine[6:]
        ]

        # Deterministic ordering: newest start first, unique id tie-breaker.
        starts = [(x['start_at'], x['id']) for x in mine]
        assert starts == sorted(starts, reverse=True)


def test_outage_pagination_validates_bounds():
    with TestClient(app) as client:
        headers = _auth(client)
        assert (
            client.get(
                '/api/outages', headers=headers, params={'limit': 0}
            ).status_code
            == 422
        )
        assert (
            client.get(
                '/api/outages', headers=headers, params={'limit': -5}
            ).status_code
            == 422
        )
        assert (
            client.get(
                '/api/outages', headers=headers, params={'limit': 1001}
            ).status_code
            == 422
        )
        assert (
            client.get(
                '/api/outages', headers=headers, params={'offset': -1}
            ).status_code
            == 422
        )


def test_outage_filters_compose_with_pagination():
    with TestClient(app) as client:
        headers = _auth(client)
        assets = client.get('/api/assets', headers=headers).json()
        asset_a = int(assets[0]['id'])
        asset_b = int(assets[1]['id'])
        _seed_outages(client, headers, 2, asset_a)
        _seed_outages(client, headers, 1, asset_b)

        filtered = _paged(
            client, headers, asset_id=asset_b, status='Open', limit=1000
        )
        assert all(
            x['asset_id'] == asset_b and x['status'] == 'Open' for x in filtered
        )
        assert len(filtered) >= 1


def test_outage_csv_export_remains_unbounded_and_separate():
    with TestClient(app) as client:
        headers = _auth(client)
        export = client.get('/api/exports/outages.csv', headers=headers)
        assert export.status_code == 200
        assert 'text/csv' in export.headers.get('content-type', '')
