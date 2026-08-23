from app.worker_jobs import JobExecution, JobState


def test_job_claim_is_exclusive():
    job = JobExecution(name="report")
    assert job.claim("worker-a") is True
    assert job.claim("worker-b") is False
    assert job.owner == "worker-a"


def test_only_owner_can_complete():
    job = JobExecution(name="sync")
    assert job.claim("worker-a")
    assert job.complete("worker-b") is False
    assert job.complete("worker-a") is True
    assert job.state == JobState.SUCCEEDED


def test_failed_jobs_move_to_dead_letter_after_limit():
    job = JobExecution(name="integration")
    for _ in range(5):
        if job.state != JobState.QUEUED:
            job.state = JobState.QUEUED
        assert job.claim("worker-a")
        assert job.fail("worker-a", max_attempts=5)
    assert job.state == JobState.DEAD_LETTER
