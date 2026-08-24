from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app


def _auth(client):
    r = client.post(
        '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
    )
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _procurement(client, headers, **params):
    response = client.get('/api/procurement', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _create_pr(client, headers, items: list[dict]) -> int:
    created = client.post(
        '/api/procurement/requisitions',
        headers=headers,
        json={
            'title': f'Pagination probe {uuid.uuid4().hex[:8]}',
            'justification': 'bounded-list regression',
            'items': items,
        },
    )
    assert created.status_code == 200, created.text
    return int(created.json()['id'])


def test_procurement_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        seeded = [
            _create_pr(client, headers, [{'description': f'item-{n}', 'quantity': n}])
            for n in range(5)
        ]

        default_view = _procurement(client, headers)
        assert len(default_view['requisitions']) <= 200
        assert len(default_view['purchase_orders']) <= 200
        assert len(default_view['quotations']) <= 200
        default_ids = {x['id'] for x in default_view['requisitions']}
        assert set(seeded).issubset(default_ids)

        page_one = _procurement(client, headers, limit=2, offset=0)['requisitions']
        page_two = _procurement(client, headers, limit=2, offset=2)['requisitions']
        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert len(ids_one) == 2 and len(ids_two) == 2
        assert not set(ids_one) & set(ids_two)
        # Newest requisition first via the unique id tie-breaker.
        assert ids_one == sorted(ids_one, reverse=True)

        full = _procurement(client, headers, limit=1000, offset=0)['requisitions']
        all_ids = [x['id'] for x in full]
        assert all_ids == sorted(all_ids, reverse=True)


def test_procurement_requisition_items_survive_batched_fetch():
    with TestClient(app) as client:
        headers = _auth(client)
        pr_id = _create_pr(
            client,
            headers,
            [
                {'description': 'bearing', 'quantity': 4},
                {'description': 'gasket', 'quantity': 9},
            ],
        )

        payload = _procurement(client, headers, limit=1000)
        row = next(x for x in payload['requisitions'] if x['id'] == pr_id)
        descriptions = sorted(x['description'] for x in row['items'])
        assert descriptions == ['bearing', 'gasket']
        assert sum(int(x['quantity']) for x in row['items']) == 13


def test_procurement_pagination_validates_bounds():
    with TestClient(app) as client:
        headers = _auth(client)
        for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get(
                    '/api/procurement', headers=headers, params=params
                ).status_code
                == 422
            ), params
