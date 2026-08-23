from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application import notify_once
from app.database import db
from app.main import app  # noqa: F401 - installs schema and production composition


WORKERS = 12


def _seed_link() -> str:
    return f'ALM-NTF-PG-{uuid.uuid4().hex[:12]}'


def _notify(link: str, message: str, role_code='maintenance_manager') -> bool:
    with db() as conn:
        return notify_once(
            conn, 'Operational alarm', message, 'Critical', None, role_code,
            'operations', link,
        )


def _unread_count(link: str) -> int:
    with db() as conn:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE title='Operational alarm' AND link_module='operations'
                     AND link_id=? AND is_read=0""",
                (link,),
            ).fetchone()[0]
        )


def _parallel(workers, fn, timeout=45):
    barrier = threading.Barrier(workers)
    results = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            results.append(fn(index))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('notification worker did not finish (possible deadlock)')
    if errors:
        raise RuntimeError(f'notification concurrency worker failed: {errors!r}')
    return results


def _race_creates_single_notification() -> None:
    link = _seed_link()
    results = _parallel(WORKERS, lambda _index: _notify(link, f'{link} raced'))
    winners = sum(1 for value in results if value is True)
    if winners != 1:
        raise RuntimeError(f'expected exactly one dedup winner, got {winners!r}')
    count = _unread_count(link)
    if count != 1:
        raise RuntimeError(f'expected one unread notification, got {count}')


def _idempotent_until_read() -> None:
    link = _seed_link()
    if _notify(link, 'first') is not True or _notify(link, 'duplicate') is not False:
        raise RuntimeError('sequential notify_once dedup broken')
    with db() as conn:
        conn.execute('UPDATE notifications SET is_read=1 WHERE link_id=?', (link,))
    if _notify(link, 'recurrence') is not True:
        raise RuntimeError('notify_once did not allow post-read recurrence')
    if _unread_count(link) != 1:
        raise RuntimeError('post-read recurrence duplicated unread notification')


def _distinct_keys_do_not_collide() -> None:
    link_a, link_b = _seed_link(), _seed_link()
    if _notify(link_a, 'a') is not True:
        raise RuntimeError('distinct key insert failed')
    if _notify(link_b, 'b') is not True:
        raise RuntimeError('distinct key insert failed')
    if _notify(link_a, 'c', role_code='asset_manager') is not True:
        raise RuntimeError('distinct recipient role insert failed')
    with db() as conn:
        rows = int(
            conn.execute(
                'SELECT COUNT(*) FROM notifications WHERE link_id IN (?,?)',
                (link_a, link_b),
            ).fetchone()[0]
        )
    if rows != 3:
        raise RuntimeError(f'distinct keys collapsed: rows={rows}')


def main() -> None:
    _race_creates_single_notification()
    _idempotent_until_read()
    _distinct_keys_do_not_collide()
    print(
        'notification dedup concurrency smoke: PASS '
        f'race_winners=1 idempotent=1 distinct_keys=1 workers={WORKERS}'
    )


if __name__ == '__main__':
    main()
