from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _bearer(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def _login(username: str, password: str) -> dict[str, str]:
    with TestClient(app) as client:
        response = client.post(
            '/api/auth/login', json={'username': username, 'password': password}
        )
        assert response.status_code == 200, response.text
        return _bearer(response.json()['token'])


def _upload(hostile_mime: str = 'text/html') -> int:
    headers = _login('tech1', 'Tech@2026')
    with TestClient(app) as client:
        response = client.post(
            '/api/documents/upload',
            headers=headers,
            data={
                'title': 'Download security regression',
                'category': 'Reports',
            },
            files={
                'file': (
                    'payload.txt',
                    io.BytesIO(b'<script>alert(1)</script>'),
                    hostile_mime,
                )
            },
        )
        assert response.status_code == 200, response.text
        return int(response.json()['id'])


def test_anonymous_download_is_rejected():
    doc_id = _upload()
    with TestClient(app) as client:
        assert client.get(f'/api/documents/{doc_id}/download').status_code == 401


def test_download_media_type_derives_from_stored_suffix_not_client_mime():
    doc_id = _upload('text/html')
    headers = _login('tech1', 'Tech@2026')
    with TestClient(app) as client:
        response = client.get(f'/api/documents/{doc_id}/download', headers=headers)
        assert response.status_code == 200, response.text
        # The client-supplied "text/html" must never win: the served type is
        # derived from the server-generated stored name (.txt).
        assert response.headers['content-type'].startswith('text/plain')
        disposition = response.headers['content-disposition']
        assert disposition.startswith('attachment')


def test_download_filename_never_echoes_control_characters():
    headers = _login('tech1', 'Tech@2026')
    with TestClient(app) as client:
        response = client.post(
            '/api/documents/upload',
            headers=headers,
            data={'title': 'Hostile filename', 'category': 'Reports'},
            files={
                'file': (
                    'bad"name\r\nX-Injected: 1.txt',
                    io.BytesIO(b'benign'),
                    'text/plain',
                )
            },
        )
        assert response.status_code == 200, response.text
        doc_id = int(response.json()['id'])

        download = client.get(f'/api/documents/{doc_id}/download', headers=headers)
        assert download.status_code == 200, download.text
        disposition = download.headers['content-disposition']
        assert disposition.startswith('attachment')
        # No raw control characters may reach the response header, and the
        # hostile quote must be percent-encoded by the disposition encoder.
        assert '\r' not in disposition and '\n' not in disposition
        assert '"' not in disposition.lower().split('filename', 1)[1]

        with db() as conn:
            conn.execute('DELETE FROM documents WHERE id=?', (doc_id,))


def test_download_rejects_unknown_suffix_with_inert_content_type():
    headers = _login('tech1', 'Tech@2026')
    with TestClient(app) as client:
        response = client.post(
            '/api/documents/upload',
            headers=headers,
            data={'title': 'Inert type check', 'category': 'Reports'},
            files={'file': ('drawing.dwg', io.BytesIO(b'AC1015'), 'application/acad')},
        )
        assert response.status_code == 200, response.text
        doc_id = int(response.json()['id'])

        download = client.get(f'/api/documents/{doc_id}/download', headers=headers)
        assert download.status_code == 200, download.text
        assert (
            download.headers['content-type'] == 'application/octet-stream'
        ), download.headers['content-type']
