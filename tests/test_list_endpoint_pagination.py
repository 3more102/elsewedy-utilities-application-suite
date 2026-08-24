from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.database import db, now
from app.main import app


def _auth(client):
    r = client.post(
        '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
    )
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _get(client, headers, path, **params):
    response = client.get(path, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _seed_work_orders(client, headers, count: int) -> list[int]:
    ids = []
    for _ in range(count):
        created = client.post(
            '/api/work-orders',
            headers=headers,
            json={'title': f'Pagination probe {uuid.uuid4().hex[:8]}'},
        )
        assert created.status_code == 200, created.text
        ids.append(int(created.json()['id']))
    return ids


def test_work_order_pagination_is_deterministic_and_additive():
    with TestClient(app) as client:
        headers = _auth(client)
        ids = _seed_work_orders(client, headers, 7)

        default_view = _get(client, headers, '/api/work-orders')
        assert len(default_view) <= 200
        assert set(ids).issubset({x['id'] for x in default_view})

        page_one = _get(
            client, headers, '/api/work-orders', **{'q': 'Pagination probe', 'limit': 3}
        )
        page_two = _get(
            client,
            headers,
            '/api/work-orders',
            **{'q': 'Pagination probe', 'limit': 3, 'offset': 3},
        )
        assert len(page_one) == 3 and len(page_two) == 3
        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert not set(ids_one) & set(ids_two)
        # Newest first via the unique id tie-breaker.
        assert ids_one == sorted(ids_one, reverse=True)

        full = _get(
            client,
            headers,
            '/api/work-orders',
            **{'q': 'Pagination probe', 'limit': 1000},
        )
        probed = [x['id'] for x in full]
        assert probed == sorted(probed, reverse=True)


def test_work_order_pagination_validates_bounds():
    with TestClient(app) as client:
        headers = _auth(client)
        for params in ({'limit': 0}, {'limit': -5}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get('/api/work-orders', headers=headers, params=params).status_code
                == 422
            ), params


def test_dispatch_pagination_is_deterministic_and_additive():
    with TestClient(app) as client:
        headers = _auth(client)
        marker = uuid.uuid4().hex[:8]
        with db() as conn:
            technician = conn.execute(
                """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE r.code='technician' ORDER BY u.id LIMIT 1"""
            ).fetchone()
            assert technician
            technician_id = int(technician['id'])
            work_orders = conn.execute(
                'SELECT id FROM work_orders ORDER BY id LIMIT 5'
            ).fetchall()
            dispatcher = conn.execute(
                "SELECT id FROM users WHERE username='omar'"
            ).fetchone()
            assert dispatcher
            seeded = []
            for wo in work_orders:
                created = conn.execute(
                    """INSERT INTO dispatch_assignments(
                         dispatch_no,work_order_id,technician_user_id,dispatched_by,
                         status,notes,dispatched_at
                       ) VALUES(?,?,?,?,'Completed',?,?)""",
                    (
                        f'DSP-PG-{marker}-{wo["id"]}',
                        int(wo['id']),
                        technician_id,
                        int(dispatcher['id']),
                        f'pagination probe {marker}',
                        now(),
                    ),
                )
                seeded.append(int(created.lastrowid))

        default_view = _get(client, headers, '/api/dispatch')
        assert len(default_view) <= 200
        assert set(seeded).issubset({x['id'] for x in default_view})

        page_one = _get(client, headers, '/api/dispatch', limit=2)
        page_two = _get(
            client,
            headers,
            '/api/dispatch',
            limit=2,
            offset=2,
        )
        assert len(page_one) == 2 and len(page_two) == 2
        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert not set(ids_one) & set(ids_two)
        # Pagination must be exact windows of the canonical status-ordered,
        # id-tiebroken sequence.
        full_view = _get(client, headers, '/api/dispatch', limit=1000)
        expected = [x['id'] for x in full_view][:4]
        assert ids_one + ids_two == expected


def test_dispatch_pagination_validates_bounds():
    with TestClient(app) as client:
        headers = _auth(client)
        for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get('/api/dispatch', headers=headers, params=params).status_code
                == 422
            ), params


def test_inspection_pagination_is_deterministic_and_additive():
    with TestClient(app) as client:
        headers = _auth(client)
        template = f'Pagination probe {uuid.uuid4().hex[:8]}'
        seeded = []
        for _ in range(5):
            created = client.post(
                '/api/inspections',
                headers=headers,
                json={'template_name': template},
            )
            assert created.status_code == 200, created.text
            seeded.append(int(created.json()['id']))

        page_one = _get(client, headers, '/api/inspections', limit=2)
        page_two = _get(client, headers, '/api/inspections', limit=2, offset=2)
        ids_one = [x['id'] for x in page_one]
        ids_two = [x['id'] for x in page_two]
        assert len(ids_one) == 2 and len(ids_two) == 2
        assert not set(ids_one) & set(ids_two)
        assert ids_one == sorted(ids_one, reverse=True)

        for params in ({'limit': 0}, {'limit': 1001}, {'offset': -1}):
            assert (
                client.get(
                    '/api/inspections', headers=headers, params=params
                ).status_code
                == 422
            ), params


def test_inspection_pagination_covers_seeded_rows():
    with TestClient(app) as client:
        headers = _auth(client)
        marker = uuid.uuid4().hex[:8]
        created_ids = []
        for _ in range(3):
            created = client.post(
                '/api/inspections', headers=headers, json={'template_name': marker}
            )
            assert created.status_code == 200, created.text
            created_ids.append(int(created.json()['id']))

        full = _get(client, headers, '/api/inspections', limit=1000)
        templates = {x['id']: x['template_name'] for x in full}
        assert all(templates.get(i) == marker for i in created_ids)
        ids = [x['id'] for x in full]
        assert ids == sorted(ids, reverse=True)
