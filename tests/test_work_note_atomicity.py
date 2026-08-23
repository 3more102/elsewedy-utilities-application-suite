from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.database import db
from app.main import app


def _auth(client, username='omar', password='EUAS@2026'):
    r = client.post(
        '/api/auth/login', json={'username': username, 'password': password}
    )
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _seed_wo(client, headers) -> int:
    asset = next(x for x in client.get('/api/assets', headers=headers).json())
    wo = client.post(
        '/api/work-orders',
        headers=headers,
        json={
            'title': f'Note append regression {uuid.uuid4().hex[:8]}',
            'asset_id': asset['id'],
            'priority': 'Low',
        },
    )
    assert wo.status_code == 200, wo.text
    return int(wo.json()['id'])


def test_sequential_notes_append_and_are_audited():
    with TestClient(app) as client:
        headers = _auth(client)
        wo_id = _seed_wo(client, headers)
        with db() as conn:
            wo_no = conn.execute(
                'SELECT wo_no FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()[0]

        first = client.post(
            f'/api/work-orders/{wo_id}/notes', headers=headers, json={'note': 'first'}
        )
        second = client.post(
            f'/api/work-orders/{wo_id}/notes', headers=headers, json={'note': 'second'}
        )
        assert first.status_code == 200 and second.status_code == 200

        with db() as conn:
            comments = conn.execute(
                'SELECT comments FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()[0]
            notes_audits = int(
                conn.execute(
                    """SELECT COUNT(*) FROM audit_logs
                       WHERE module='Work Management' AND action='ADD NOTE'
                         AND record_id=?""",
                    (wo_no,),
                ).fetchone()[0]
            )
        assert 'first' in comments and 'second' in comments
        assert notes_audits == 2


def test_concurrent_note_appenders_never_lose_notes():
    with TestClient(app) as client:
        admin = _auth(client)
        tech = _auth(client, 'tech1', 'Tech@2026')
        wo_id = _seed_wo(client, admin)

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def worker(token: str) -> None:
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    f'/api/work-orders/{wo_id}/notes',
                    headers=admin,
                    json={'note': token},
                )
                outcomes.append(f'{token}:{response.status_code}')
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(token,)) for token in ('alpha', 'beta')
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []

        with db() as conn:
            comments = conn.execute(
                'SELECT comments FROM work_orders WHERE id=?', (wo_id,)
            ).fetchone()[0]

        succeeded = [o for o in outcomes if o.endswith(':200')]
        conflicts = [o for o in outcomes if o.endswith(':409')]
        # Every accepted note is present; every rejected note is absent — no
        # silent loss either way.
        for outcome in succeeded:
            assert outcome.split(':')[0] in comments
        for outcome in conflicts:
            assert outcome.split(':')[0] not in comments
        assert len(succeeded) >= 1
        # The atomic service-level claim must agree with the HTTP surface.
        assert len(conflicts) + len(succeeded) == 2
