from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit_store import append_audit, ensure_audit_chain_lock
from app.audit_verification import verify_audit_chain_report
from app.database import db
from app.main import verify_audit_chain


BOOTSTRAP_WORKERS = 8
APPEND_WORKERS = 16
MODULE = 'AuditConcurrencyPostgreSQL'


def assert_linear_chain(conn) -> int:
    """Validate the whole chain through the shared canonical validator."""
    report = verify_audit_chain_report(conn)
    if not report['valid']:
        raise RuntimeError(
            f"audit chain invalid at id={report['first_invalid_id']} "
            f"(checked={report['checked']}, last_good_head={report['head_hash'] or '-'})"
        )
    return report['checked']


def run_bootstrap_race() -> None:
    # This CI database is disposable. Remove only the synchronization table so
    # multiple sessions exercise first-start initialization against an existing
    # application schema and audit history.
    with db() as conn:
        conn.execute('DROP TABLE IF EXISTS audit_chain_lock')

    barrier = threading.Barrier(BOOTSTRAP_WORKERS)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                ensure_audit_chain_lock(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(BOOTSTRAP_WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('audit lock bootstrap worker did not finish')
    if errors:
        raise RuntimeError(f'audit lock bootstrap race failed: {errors!r}')

    with db() as conn:
        rows = conn.execute('SELECT id,guard FROM audit_chain_lock ORDER BY id').fetchall()
    if len(rows) != 1 or int(rows[0]['id']) != 1:
        raise RuntimeError(f'audit lock bootstrap produced invalid singleton rows: {rows!r}')


def run_append_race() -> None:
    with db() as conn:
        user = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
        if not user:
            raise RuntimeError('audit concurrency smoke requires seeded admin user omar')
        user_id = int(user['id'])
        before = int(conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0])

    barrier = threading.Barrier(APPEND_WORKERS)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            with db() as conn:
                append_audit(
                    conn,
                    user_id,
                    'CONCURRENT_APPEND',
                    MODULE,
                    str(index),
                    {'before': index},
                    {'after': index + 1},
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(APPEND_WORKERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=45)

    if any(thread.is_alive() for thread in threads):
        raise RuntimeError('audit append worker did not finish')
    if errors:
        raise RuntimeError(f'audit append concurrency failed: {errors!r}')

    with db() as conn:
        tagged = int(
            conn.execute(
                'SELECT COUNT(*) FROM audit_logs WHERE module=?', (MODULE,)
            ).fetchone()[0]
        )
        after = assert_linear_chain(conn)
        verified = verify_audit_chain(conn)

    if tagged != APPEND_WORKERS:
        raise RuntimeError(
            f'lost concurrent audit rows: expected {APPEND_WORKERS}, got {tagged}'
        )
    if after != before + APPEND_WORKERS:
        raise RuntimeError(
            f'audit row count mismatch: before={before}, after={after}, '
            f'expected delta={APPEND_WORKERS}'
        )
    if not verified['valid'] or int(verified['checked']) != after:
        raise RuntimeError(f'application audit verifier rejected chain: {verified!r}')


def run_transaction_semantics() -> None:
    module = 'AuditConcurrencyPostgreSQLTransaction'
    with db() as conn:
        user_id = int(
            conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id']
        )
        first = append_audit(conn, user_id, 'FIRST', module, '1', '', {'step': 1})
        second = append_audit(
            conn, user_id, 'SECOND', module, '2', {'step': 1}, {'step': 2}
        )

    with db() as conn:
        rows = conn.execute(
            '''SELECT prev_hash,audit_hash FROM audit_logs
               WHERE module=? ORDER BY id''',
            (module,),
        ).fetchall()
    if len(rows) != 2 or rows[0]['audit_hash'] != first:
        raise RuntimeError('multiple audit appends in one transaction were not persisted')
    if rows[1]['prev_hash'] != first or rows[1]['audit_hash'] != second:
        raise RuntimeError('multiple audit appends in one transaction did not chain locally')

    rollback_module = module + 'Rollback'
    try:
        with db() as conn:
            append_audit(
                conn, user_id, 'ROLLBACK', rollback_module, 'rollback', '', 'discard'
            )
            raise RuntimeError('intentional rollback')
    except RuntimeError as exc:
        if str(exc) != 'intentional rollback':
            raise

    with db() as conn:
        rolled_back = int(
            conn.execute(
                'SELECT COUNT(*) FROM audit_logs WHERE module=?', (rollback_module,)
            ).fetchone()[0]
        )
        verified = verify_audit_chain(conn)
    if rolled_back != 0:
        raise RuntimeError('rolled-back business transaction retained an audit entry')
    if not verified['valid']:
        raise RuntimeError(f'audit chain invalid after rollback test: {verified!r}')


def main() -> None:
    run_bootstrap_race()
    run_append_race()
    run_transaction_semantics()
    print(
        'audit concurrency smoke: PASS '
        f'bootstrap_workers={BOOTSTRAP_WORKERS} append_workers={APPEND_WORKERS}'
    )


if __name__ == '__main__':
    main()
