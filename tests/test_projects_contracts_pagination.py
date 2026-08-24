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


def _create_project(client, headers, name: str) -> int:
    created = client.post(
        '/api/projects',
        headers=headers,
        json={'name': name, 'status': 'Active', 'budget': 1000},
    )
    assert created.status_code == 200, created.text
    return int(created.json()['id'])


def _projects(client, headers, **params):
    response = client.get('/api/projects', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_projects_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        seeded = [
            _create_project(
                client, headers, f'Pagination probe {uuid.uuid4().hex[:8]}'
            )
            for _ in range(5)
        ]

        default_view = _projects(client, headers)
        assert len(default_view) <= 200
        assert set(seeded).issubset({p['id'] for p in default_view})

        page_one = _projects(client, headers, limit=2)
        page_two = _projects(client, headers, limit=2, offset=2)
        assert [p['id'] for p in page_one] == sorted(
            seeded, reverse=True
        )[:2]
        assert [p['id'] for p in page_two] == list(reversed(seeded))[2:4]
        ids_one = {p['id'] for p in page_one}
        ids_two = {p['id'] for p in page_two}
        assert not ids_one & ids_two


def test_projects_page_tasks_are_batched_and_complete():
    with TestClient(app) as client:
        headers = _auth(client)
        project_id = _create_project(
            client, headers, f'Task batching probe {uuid.uuid4().hex[:8]}'
        )

        task_ids = []
        for index in range(3):
            created = client.post(
                f'/api/projects/{project_id}/tasks',
                headers=headers,
                json={
                    'task_name': f'Batch probe {index}',
                    'status': 'Open',
                    'progress': 10 * index,
                },
            )
            assert created.status_code == 200, created.text
            task_ids.append(int(created.json()['id']))

        page = _projects(client, headers, limit=1, offset=0)
        assert len(page) == 1
        assert page[0]['id'] == project_id
        returned = [t['id'] for t in page[0]['tasks']]
        assert returned == sorted(task_ids)
        owners_ok = all(
            set(t) >= {'task_name', 'status', 'progress', 'owner_name'}
            for t in page[0]['tasks']
        )
        assert owners_ok


def test_projects_reject_out_of_range_paging():
    with TestClient(app) as client:
        headers = _auth(client)
        assert (
            client.get(
                '/api/projects', headers=headers, params={'limit': 0}
            ).status_code
            == 422
        )
        assert (
            client.get(
                '/api/projects', headers=headers, params={'limit': 1001}
            ).status_code
            == 422
        )
        assert (
            client.get(
                '/api/projects', headers=headers, params={'offset': -1}
            ).status_code
            == 422
        )


def test_contracts_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        vendor = client.post(
            '/api/vendors',
            headers=headers,
            json={
                'name': f'Pagination vendor {uuid.uuid4().hex[:8]}',
                'category': 'Electrical',
            },
        )
        assert vendor.status_code == 200, vendor.text
        seeded = []
        for index in range(5):
            created = client.post(
                '/api/contracts',
                headers=headers,
                json={
                    'title': f'Pagination contract {index}',
                    'vendor_id': int(vendor.json()['id']),
                    'value': 500,
                },
            )
            assert created.status_code == 200, created.text
            seeded.append(int(created.json()['id']))

        default_view = client.get('/api/contracts', headers=headers).json()
        assert len(default_view) <= 200
        assert set(seeded).issubset({c['id'] for c in default_view})

        page_one = client.get(
            '/api/contracts', headers=headers, params={'limit': 2}
        ).json()
        page_two = client.get(
            '/api/contracts',
            headers=headers,
            params={'limit': 2, 'offset': 2},
        ).json()
        assert [c['id'] for c in page_one] == sorted(seeded, reverse=True)[:2]
        assert [c['id'] for c in page_two] == list(reversed(seeded))[2:4]
