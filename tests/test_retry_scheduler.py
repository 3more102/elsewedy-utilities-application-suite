from datetime import datetime, timezone

from app.retry_scheduler import RetryPolicy, RetryScheduler


def test_retry_backoff_is_deterministic():
    scheduler = RetryScheduler(RetryPolicy(max_attempts=3, base_delay_seconds=10))
    scheduler.record_failure("job-1")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry = scheduler.next_retry_at("job-1", now)

    assert retry == datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)


def test_dead_letter_after_max_attempts():
    scheduler = RetryScheduler(RetryPolicy(max_attempts=2))
    scheduler.record_failure("job-1")
    scheduler.record_failure("job-1")

    assert scheduler.should_dead_letter("job-1") is True
    assert scheduler.next_retry_at("job-1") is None
