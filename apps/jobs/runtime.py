from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from core.database import db

from .queue import claim_next_job, complete_job, fail_job, start_job
from .workers import heartbeat_worker, register_worker

JobHandler = Callable[[dict, 'JobContext'], object]


@dataclass(frozen=True)
class JobContext:
    job_id: str
    correlation_id: str
    worker_id: str
    attempt_count: int


class JobHandlerRegistry:
    def __init__(self):
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        if not job_type or not callable(handler):
            raise ValueError('job_type and callable handler are required')
        self._handlers[job_type] = handler

    def resolve(self, job_type: str) -> JobHandler | None:
        return self._handlers.get(job_type)

    def job_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


class WorkerRuntime:
    def __init__(self, worker_id: str, registry: JobHandlerRegistry, *, lease_seconds: int = 60):
        self.worker_id = worker_id
        self.registry = registry
        self.lease_seconds = max(5, int(lease_seconds))

    def run_once(self):
        with db() as conn:
            register_worker(conn, worker_id=self.worker_id)
            heartbeat_worker(conn, self.worker_id)
            claimed = claim_next_job(conn, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            if not claimed:
                return None
            started = start_job(conn, job_id=claimed['id'], worker_id=self.worker_id)

        handler = self.registry.resolve(started['job_type'])
        if handler is None:
            with db() as conn:
                return fail_job(
                    conn,
                    job_id=started['id'],
                    worker_id=self.worker_id,
                    error=f"No handler registered for {started['job_type']}",
                )

        payload = json.loads(started['payload_json'] or '{}')
        context = JobContext(
            job_id=started['job_id'],
            correlation_id=started['correlation_id'],
            worker_id=self.worker_id,
            attempt_count=started['attempt_count'],
        )
        try:
            handler(payload, context)
        except Exception as exc:
            with db() as conn:
                return fail_job(conn, job_id=started['id'], worker_id=self.worker_id, error=str(exc))
        with db() as conn:
            return complete_job(conn, job_id=started['id'], worker_id=self.worker_id)
