from __future__ import annotations

import json

from core.database import now


class WorkerNotFound(LookupError):
    pass


def register_worker(conn, *, worker_id: str, name: str = '', metadata: dict | None = None) -> dict:
    worker_id = str(worker_id or '').strip()
    if not worker_id:
        raise ValueError('worker_id is required')
    stamp = now()
    existing = conn.execute('SELECT id FROM workers WHERE worker_id=?', (worker_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE workers SET name=?,status='Active',last_heartbeat_at=?,metadata_json=? WHERE worker_id=?",
            (name or worker_id, stamp, json.dumps(metadata or {}, sort_keys=True), worker_id),
        )
    else:
        conn.execute(
            "INSERT INTO workers(worker_id,name,status,registered_at,last_heartbeat_at,metadata_json) VALUES(?,?,'Active',?,?,?)",
            (worker_id, name or worker_id, stamp, stamp, json.dumps(metadata or {}, sort_keys=True)),
        )
    return dict(conn.execute('SELECT * FROM workers WHERE worker_id=?', (worker_id,)).fetchone())


def heartbeat_worker(conn, worker_id: str) -> dict:
    stamp = now()
    updated = conn.execute(
        "UPDATE workers SET status='Active',last_heartbeat_at=? WHERE worker_id=?",
        (stamp, worker_id),
    )
    if updated.rowcount != 1:
        raise WorkerNotFound('Worker not found')
    return dict(conn.execute('SELECT * FROM workers WHERE worker_id=?', (worker_id,)).fetchone())


def deactivate_worker(conn, worker_id: str) -> dict:
    stamp = now()
    updated = conn.execute(
        "UPDATE workers SET status='Inactive',last_heartbeat_at=? WHERE worker_id=?",
        (stamp, worker_id),
    )
    if updated.rowcount != 1:
        raise WorkerNotFound('Worker not found')
    return dict(conn.execute('SELECT * FROM workers WHERE worker_id=?', (worker_id,)).fetchone())
