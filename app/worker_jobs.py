"""Persistent worker job primitives.

Extends worker runtime ownership with deterministic job lifecycle rules.
This module is intentionally storage-agnostic so existing EUAS database
repositories can provide the persistence backend.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class JobState(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    DEAD_LETTER = "DeadLetter"


@dataclass
class JobExecution:
    job_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    payload: dict = field(default_factory=dict)
    state: JobState = JobState.QUEUED
    attempts: int = 0
    owner: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def claim(self, worker_id: str) -> bool:
        if self.state != JobState.QUEUED:
            return False
        self.state = JobState.RUNNING
        self.owner = worker_id
        self.attempts += 1
        self.updated_at = datetime.now(timezone.utc)
        return True

    def complete(self, worker_id: str) -> bool:
        if self.state != JobState.RUNNING or self.owner != worker_id:
            return False
        self.state = JobState.SUCCEEDED
        self.updated_at = datetime.now(timezone.utc)
        return True

    def fail(self, worker_id: str, max_attempts: int = 5) -> bool:
        if self.state != JobState.RUNNING or self.owner != worker_id:
            return False
        self.state = JobState.DEAD_LETTER if self.attempts >= max_attempts else JobState.FAILED
        self.updated_at = datetime.now(timezone.utc)
        return True
