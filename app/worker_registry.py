"""Worker registry primitives for EUAS distributed execution.

This module extends the worker runtime foundation with registration,
heartbeat tracking, and lease-health evaluation. Persistence adapters can
wrap these primitives without changing worker ownership semantics.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict


@dataclass
class WorkerRecord:
    worker_id: str
    last_heartbeat: datetime
    active: bool = True


class WorkerRegistry:
    def __init__(self):
        self._workers: Dict[str, WorkerRecord] = {}

    def register(self, worker_id: str, now: datetime | None = None) -> WorkerRecord:
        now = now or datetime.now(timezone.utc)
        record = WorkerRecord(worker_id=worker_id, last_heartbeat=now)
        self._workers[worker_id] = record
        return record

    def heartbeat(self, worker_id: str, now: datetime | None = None) -> bool:
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        worker.last_heartbeat = now or datetime.now(timezone.utc)
        worker.active = True
        return True

    def expire_unhealthy(self, timeout_seconds: int, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        expired = []
        for worker in self._workers.values():
            if now - worker.last_heartbeat > timedelta(seconds=timeout_seconds):
                worker.active = False
                expired.append(worker.worker_id)
        return expired

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._workers.get(worker_id)
