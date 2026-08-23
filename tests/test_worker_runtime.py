from datetime import datetime, timezone, timedelta

from app.worker_runtime import WorkerLeaseManager


def test_single_owner_lease():
    manager = WorkerLeaseManager()
    now = datetime.now(timezone.utc)

    assert manager.acquire("job-1", "worker-a", now=now)
    assert not manager.acquire("job-1", "worker-b", now=now)
    assert manager.owner("job-1") == "worker-a"


def test_expired_lease_can_be_recovered():
    manager = WorkerLeaseManager()
    start = datetime.now(timezone.utc)

    assert manager.acquire("job-1", "worker-a", seconds=1, now=start)
    expired = start + timedelta(seconds=2)

    assert manager.acquire("job-1", "worker-b", now=expired)
    assert manager.owner("job-1") == "worker-b"


def test_heartbeat_requires_current_owner():
    manager = WorkerLeaseManager()
    now = datetime.now(timezone.utc)

    assert manager.acquire("job-1", "worker-a", now=now)
    assert not manager.heartbeat("job-1", "worker-b", now=now)
    assert manager.heartbeat("job-1", "worker-a", now=now)
