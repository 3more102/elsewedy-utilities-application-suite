"""Persistent-worker primitives for EUAS background execution.

This module intentionally provides domain-neutral lease semantics. Existing
business workflows, outbox delivery and database repositories remain owners of
business mutations.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional


@dataclass(frozen=True)
class WorkerLease:
    worker_id: str
    job_id: str
    expires_at: datetime

    def active(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now < self.expires_at


class WorkerLeaseManager:
    """In-memory lease contract used as a deterministic foundation.

    Production persistence can be backed by EUAS database repositories while
    keeping the same ownership rules: one active owner per job lease.
    """

    def __init__(self):
        self._leases: Dict[str, WorkerLease] = {}

    def acquire(self, job_id: str, worker_id: str, seconds: int = 60,
                now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        current = self._leases.get(job_id)
        if current and current.active(now):
            return False
        self._leases[job_id] = WorkerLease(
            worker_id=worker_id,
            job_id=job_id,
            expires_at=now + timedelta(seconds=seconds),
        )
        return True

    def heartbeat(self, job_id: str, worker_id: str, seconds: int = 60,
                  now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        lease = self._leases.get(job_id)
        if not lease or lease.worker_id != worker_id:
            return False
        self._leases[job_id] = WorkerLease(
            worker_id=worker_id,
            job_id=job_id,
            expires_at=now + timedelta(seconds=seconds),
        )
        return True

    def owner(self, job_id: str) -> Optional[str]:
        lease = self._leases.get(job_id)
        return lease.worker_id if lease else None
