from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.application import notify_once
from app.database import db
from app.main import app


def _link() -> str:
    return 'ALM-NTF-' + uuid.uuid4().hex[:10]


def _notify(link_id: str, message: str, role_code: str = 'maintenance_manager') -> bool:
    with db() as conn:
        return notify_once(
            conn,
            'Operational alarm',
            message,
            'Critical',
            None,
            role_code,
            'operations',
            link_id,
        )


def _unread_count(link_id: str) -> int:
    with db() as conn:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE title='Operational alarm' AND link_module='operations'
                     AND link_id=? AND is_read=0""",
                (link_id,),
            ).fetchone()[0]
        )


def test_notify_once_is_idempotent_until_read():
    with TestClient(app):
        link = _link()
        assert _notify(link, f'{link} first') is True
        assert _notify(link, f'{link} duplicate') is False
        assert _unread_count(link) == 1

        with db() as conn:
            conn.execute(
                'UPDATE notifications SET is_read=1 WHERE link_id=?', (link,)
            )
        # Once acknowledged, a genuinely new occurrence may notify again.
        assert _notify(link, f'{link} recurrence') is True
        assert _unread_count(link) == 1


def test_concurrent_notify_once_creates_single_notification():
    with TestClient(app):
        link = _link()
        workers = 8
        barrier = threading.Barrier(workers)
        results: list[bool] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(_notify(link, f'{link} raced'))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        # Exactly one racer wins the insert; the rest are absorbed by the
        # partial unique index instead of duplicating the notification.
        assert results.count(True) == 1
        assert _unread_count(link) == 1


def test_distinct_dedup_keys_do_not_collide():
    with TestClient(app):
        link_a = _link()
        link_b = _link()
        assert _notify(link_a, 'a') is True
        assert _notify(link_b, 'b') is True
        # A different recipient role for the same event is a distinct key.
        assert _notify(link_a, 'c', role_code='asset_manager') is True
        with db() as conn:
            rows = int(
                conn.execute(
                    'SELECT COUNT(*) FROM notifications WHERE link_id IN (?,?)',
                    (link_a, link_b),
                ).fetchone()[0]
            )
        assert rows == 3
