from fastapi.testclient import TestClient

from app.main import app


def _auth(client):
    response = client.post(
        '/api/auth/login', json={'username': 'omar', 'password': 'EUAS@2026'}
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _assets(client, headers, **params):
    response = client.get('/api/assets', headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_assets_pagination_is_additive_and_deterministic():
    with TestClient(app) as client:
        headers = _auth(client)
        default_view = _assets(client, headers)
        assert len(default_view) <= 200

        first_page = _assets(client, headers, limit=2, offset=0)
        second_page = _assets(client, headers, limit=2, offset=2)

        assert first_page == default_view[:2]
        assert second_page == default_view[2:4]
        assert {row['id'] for row in first_page}.isdisjoint(
            {row['id'] for row in second_page}
        )


def test_assets_pagination_composes_with_existing_filters_and_sort():
    with TestClient(app) as client:
        headers = _auth(client)
        default_view = _assets(client, headers)
        assert default_view
        probe = default_view[0]

        filtered = _assets(
            client,
            headers,
            q=probe['asset_no'],
            condition=probe['condition'],
            status=probe['status'],
            sort='name',
            limit=1,
            offset=0,
        )

        assert len(filtered) == 1
        assert filtered[0]['id'] == probe['id']


def test_assets_reject_out_of_range_paging():
    with TestClient(app) as client:
        headers = _auth(client)
        assert client.get('/api/assets', headers=headers, params={'limit': 0}).status_code == 422
        assert client.get('/api/assets', headers=headers, params={'limit': 1001}).status_code == 422
        assert client.get('/api/assets', headers=headers, params={'offset': -1}).status_code == 422
