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


def _hse(client, headers, **params):
    response = client.get('/api/hse', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_hse_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        seeded = []
        for _ in range(5):
            created = client.post(
                '/api/hse',
                headers=headers,
                json={
                    'incident_type': 'Hazard',
                    'title': f'Pagination probe {uuid.uuid4().hex[:8]}',
                    'severity': 2,
                    'probability': 2,
                    'description': 'bounded-list regression',
                },
            )
            assert created.status_code == 200, created.text
            seeded.append(int(created.json()['id']))

        default_view = _hse(client, headers)
        assert len(default_view) <= 200
        assert set(seeded).issubset({x['id'] for x in default_view})

        page_one = _hse(client, headers, limit=2)
        page_two = _hse(client, headers, limit=2, offset=2)
        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert len(ids_one) == 2 and len(ids_two) == 2
        assert not set(ids_one) & set(ids_two)
        assert ids_one == sorted(ids_one, reverse=True)

        full = [x['id'] for x in _hse(client, headers, limit=1000)]
        assert full == sorted(full, reverse=True)

        for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get('/api/hse', headers=headers, params=params).status_code
                == 422
            ), params


def _documents(client, headers, **params):
    response = client.get('/api/documents', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _upload_document(client, headers) -> int:
    response = client.post(
        '/api/documents/upload',
        headers=headers,
        data={'title': f'Pagination probe {uuid.uuid4().hex[:8]}', 'category': 'Report'},
        files={'file': ('probe.txt', b'bounded-list regression', 'text/plain')},
    )
    assert response.status_code == 200, response.text
    return int(response.json()['id'])


def test_documents_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        seeded = [_upload_document(client, headers) for _ in range(3)]

        default_view = _documents(client, headers)
        assert len(default_view) <= 200
        assert set(seeded).issubset({x['id'] for x in default_view})

        page_one = _documents(client, headers, limit=2)
        ids_one = [x['id'] for x in page_one]
        assert len(ids_one) == 2
        assert ids_one == sorted(ids_one, reverse=True)

        everything = [x['id'] for x in _documents(client, headers, limit=1000)]
        assert everything == sorted(everything, reverse=True)

        for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get(
                    '/api/documents', headers=headers, params=params
                ).status_code
                == 422
            ), params
