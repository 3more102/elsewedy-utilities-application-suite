from __future__ import annotations

import uuid

from app import application as _application
from app.database import db, now
from app.main import app  # noqa: F401 - installs production hardening composition
from app.scheduler_store import SCHEDULER_TRIGGER, run_scheduled_automation_once


def _actor(conn) -> int:
    role = conn.execute("SELECT id FROM roles WHERE code='admin'").fetchone()
    assert role
    username = f'scheduler-generation-{uuid.uuid4().hex[:10]}'
    created = conn.execute(
        '''INSERT INTO users(
             username,password_hash,full_name,role_id,active,created_at
           ) VALUES(?,?,?,?,1,?)''',
        (username, 'scheduler-test-only', username, int(role['id']), now()),
    )
    return int(created.lastrowid)


def _run(conn, actor_id: int, status: str, suffix: str) -> None:
    conn.execute(
        '''INSERT INTO job_runs(
             run_no,trigger_source,status,actor_id,as_of,started_at,finished_at,
             summary_json,error_message
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            f'JOB-SCHED-GEN-{suffix}-{uuid.uuid4().hex[:8]}',
            SCHEDULER_TRIGGER,
            status,
            actor_id,
            '2026-08-23',
            now(),
            now(),
            '{}',
            'expected generation regression failure' if status == 'Failed' else '',
        ),
    )


def test_newer_failure_overrides_older_recent_success_for_failover(monkeypatch):
    """The newest failed generation must not be hidden by an older success."""
    with db() as conn:
        actor_id = _actor(conn)
        _run(conn, actor_id, 'Succeeded', 'SUCCESS')
        _run(conn, actor_id, 'Failed', 'FAILED')

    calls = []

    def fake_execute(conn, received_actor_id, trigger_source='manual', as_of=None):
        calls.append((received_actor_id, trigger_source, as_of))
        return {'status': 'Succeeded', 'run_no': 'JOB-SCHED-GEN-FAILOVER'}

    monkeypatch.setattr(_application, '_execute_automation', fake_execute)
    with db() as conn:
        result = run_scheduled_automation_once(conn, actor_id, interval_minutes=60)

    assert result == {'status': 'Succeeded', 'run_no': 'JOB-SCHED-GEN-FAILOVER'}
    assert calls == [(actor_id, SCHEDULER_TRIGGER, None)]
