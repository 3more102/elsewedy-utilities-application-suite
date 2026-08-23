from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import db
from app.main import app
from app.outage_store import (
    OutageTransitionConflict,
    close_outage_atomic,
)


def _auth(client, username='omar', password='EUAS@2026'):
    response = client.post(
        '/api/auth/login', json={'username': username, 'password': password}
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def _open_outage(client, headers, suffix: str, asset_id: int | None = None):
    if asset_id is None:
        assets = client.get('/api/assets', headers=headers).json()
        asset = next(a for a in assets if a['status'] in ('Operating', 'Standby'))
        asset_id = int(asset['id'])
    started = (datetime.now() - timedelta(minutes=5)).isoformat(timespec='seconds')
    created = client.post(
        '/api/outages',
        headers=headers,
        json={
            'asset_id': asset_id,
            'outage_type': 'Forced',
            'cause_code': f'REG-{suffix}',
            'start_at': started,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    return int(body['id']), asset_id, body['outage_no']


def _close_event_count(asset_id: int) -> int:
    with db() as conn:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM event_outbox
                   WHERE event_type='asset.outage.closed'
                     AND aggregate_type='asset' AND aggregate_id=?""",
                (asset_id,),
            ).fetchone()[0]
        )


def _evidence(outage_id: int, asset_id: int, outage_no: str) -> dict:
    with db() as conn:
        outage = dict(
            conn.execute(
                'SELECT status,end_at FROM asset_outages WHERE id=?', (outage_id,)
            ).fetchone()
        )
        asset_status = conn.execute(
            'SELECT status FROM assets WHERE id=?', (asset_id,)
        ).fetchone()[0]
        audits = int(
            conn.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE module='Operations' AND action='CLOSE OUTAGE'
                     AND record_id=?""",
                (outage_no,),
            ).fetchone()[0]
        )
    return {
        'outage': outage,
        'asset_status': asset_status,
        'close_audits': audits,
    }


def test_repeated_close_is_terminal_and_audited_once():
    with TestClient(app) as client:
        admin = _auth(client)
        outage_id, asset_id, outage_no = _open_outage(
            client, admin, uuid.uuid4().hex[:8]
        )
        events_before = _close_event_count(asset_id)

        first = client.post(f'/api/outages/{outage_id}/close', headers=admin, json={})
        assert first.status_code == 200, first.text
        second = client.post(f'/api/outages/{outage_id}/close', headers=admin, json={})
        assert second.status_code == 409, second.text

        evidence = _evidence(outage_id, asset_id, outage_no)
        assert evidence['outage']['status'] == 'Closed'
        assert evidence['close_audits'] == 1
        assert _close_event_count(asset_id) - events_before == 1
        assert evidence['asset_status'] == 'Operating'


def test_concurrent_closes_commit_exactly_one_generation():
    with TestClient(app) as client:
        admin = _auth(client)
        outage_id, asset_id, outage_no = _open_outage(
            client, admin, uuid.uuid4().hex[:8]
        )
        events_before = _close_event_count(asset_id)

        with db() as conn:
            user = dict(
                conn.execute(
                    """SELECT u.id,u.full_name,r.code role FROM users u
                       JOIN roles r ON r.id=u.role_id WHERE u.username='omar'"""
                ).fetchone()
            )

        barrier = threading.Barrier(4)
        results: list[dict] = []
        conflicts: list[str] = []
        errors: list[BaseException] = []

        class _Body:
            end_at = None
            impact = None

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    results.append(
                        close_outage_atomic(conn, outage_id, _Body(), user)
                    )
            except OutageTransitionConflict as exc:
                conflicts.append(str(exc))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert sum(1 for r in results if r.get('status') == 'Closed') == 1
        assert len(conflicts) == 3

        evidence = _evidence(outage_id, asset_id, outage_no)
        assert evidence['outage']['status'] == 'Closed'
        assert evidence['close_audits'] == 1
        assert _close_event_count(asset_id) - events_before == 1


def test_close_keeps_asset_unavailable_while_other_outages_remain_open():
    with TestClient(app) as client:
        admin = _auth(client)
        first_id, asset_id, _ = _open_outage(client, admin, uuid.uuid4().hex[:8])
        second_id, _, _ = _open_outage(
            client, admin, uuid.uuid4().hex[:8], asset_id=asset_id
        )

        assert (
            client.post(f'/api/outages/{first_id}/close', headers=admin, json={}).status_code
            == 200
        )

        with db() as conn:
            asset_status = conn.execute(
                'SELECT status FROM assets WHERE id=?', (asset_id,)
            ).fetchone()[0]
        assert asset_status == 'Under Maintenance'

        assert (
            client.post(f'/api/outages/{second_id}/close', headers=admin, json={}).status_code
            == 200
        )
        with db() as conn:
            asset_status = conn.execute(
                'SELECT status FROM assets WHERE id=?', (asset_id,)
            ).fetchone()[0]
        assert asset_status == 'Operating'
