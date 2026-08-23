from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: int = 30


class RetryScheduler:
    """Deterministic retry metadata calculator for background jobs.

    This module intentionally contains scheduling policy only. Persistence remains
    owned by the existing EUAS database/job infrastructure.
    """

    def __init__(self, policy: RetryPolicy | None = None):
        self.policy = policy or RetryPolicy()
        self._attempts: Dict[str, int] = {}

    def record_failure(self, job_id: str) -> int:
        attempt = self._attempts.get(job_id, 0) + 1
        self._attempts[job_id] = attempt
        return attempt

    def next_retry_at(self, job_id: str, now: datetime | None = None):
        attempt = self._attempts.get(job_id, 0)
        if attempt == 0 or attempt >= self.policy.max_attempts:
            return None
        current = now or datetime.now(timezone.utc)
        delay = self.policy.base_delay_seconds * (2 ** (attempt - 1))
        return current + timedelta(seconds=delay)

    def should_dead_letter(self, job_id: str) -> bool:
        return self._attempts.get(job_id, 0) >= self.policy.max_attempts
