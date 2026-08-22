from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit_store import append_audit, ensure_audit_chain_lock
from app.database import audit_digest, db
from app.main import app, verify_audit_chain


THREADS = 8


def _admin_user_id(conn) -> int:
    row = conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()
    assert row is not None
    return int(row['id'])


def _assert_linear_chain(conn) -> int:
    rows = conn.execute(
        '''SELECT id,user_id,action,module,record_id,old_value,new_value,
                  created_at,prev_hash,audit_hash
           FROM audit_logs ORDER BY id'''
    ).fetchall()
    previous = ''
    for row in rows:
        assert (row['prev_hash'] or '') == previous
        expected = audit_digest(
            previous,
            row['user_id'],
            row['action'],
            row['module'],
            row['record_id'],
            row['old_value'],
            row['new_value'],
            row['created_at'],
        )
        assert row['audit_hash'] == expected
        previous = row['audit_hash']
    return len(rows)


def test_sqlite_audit_lock_bootstrap_is_idempotent_under_concurrency(tmp_path: Path):
    path = tmp_path / 'audit-lock-bootstrap.db'
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def worker() -> None:
        conn = sqlite3.connect(path, timeout=10)
        try:
            barrier.wait(timeout=10)
            ensure_audit_chain_lock(conn)
            conn.commit()
        except BaseException as exc:
            errors.append(exc)
            conn.rollback()
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT id,guard FROM audit_chain_lock').fetchall()
    assert rows == [(1, 0)]


def test_concurrent_sqlite_audit_appends_form_one_cryptographic_chain():
    module = 'AuditConcurrencySQLite'
    errors: list[BaseException] = []
    barrier = threading.Barrier(THREADS)

    with TestClient(app):
        with db() as conn:
            user_id = _admin_user_id(conn)
            before = int(conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0])

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with db() as conn:
                    append_audit(
                        conn,
                        user_id,
                        'CONCURRENT_APPEND',
                        module,
                        str(index),
                        {'before': index},
                        {'after': index + 1},
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []

        with db() as conn:
            tagged = int(
                conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0]
            )
            after = _assert_linear_chain(conn)
            verified = verify_audit_chain(conn)

        assert tagged == THREADS
        assert after == before + THREADS
        assert verified['valid'] is True
        assert verified['checked'] == after


def test_multiple_audit_appends_in_one_transaction_chain_locally():
    module = 'AuditMultiAppend'
    with TestClient(app):
        with db() as conn:
            user_id = _admin_user_id(conn)
            first_hash = append_audit(
                conn, user_id, 'FIRST', module, '1', '', {'step': 1}
            )
            second_hash = append_audit(
                conn, user_id, 'SECOND', module, '2', {'step': 1}, {'step': 2}
            )

        with db() as conn:
            rows = conn.execute(
                '''SELECT record_id,prev_hash,audit_hash
                   FROM audit_logs WHERE module=? ORDER BY id''',
                (module,),
            ).fetchall()
            verified = verify_audit_chain(conn)

        assert len(rows) == 2
        assert rows[0]['audit_hash'] == first_hash
        assert rows[1]['prev_hash'] == first_hash
        assert rows[1]['audit_hash'] == second_hash
        assert verified['valid'] is True


def test_audit_append_rolls_back_with_failed_business_transaction():
    module = 'AuditRollback'
    with TestClient(app):
        with db() as conn:
            user_id = _admin_user_id(conn)
            before = int(
                conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0]
            )

        with pytest.raises(RuntimeError, match='force rollback'):
            with db() as conn:
                append_audit(
                    conn, user_id, 'ROLLED_BACK', module, 'rollback', '', 'never commit'
                )
                raise RuntimeError('force rollback')

        with db() as conn:
            after = int(
                conn.execute(
                    'SELECT COUNT(*) FROM audit_logs WHERE module=?', (module,)
                ).fetchone()[0]
            )
            verified = verify_audit_chain(conn)

        assert after == before
        assert verified['valid'] is True
