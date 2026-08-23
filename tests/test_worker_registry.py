from datetime import datetime, timedelta, timezone

from app.worker_registry import WorkerRegistry


def test_worker_registration_and_heartbeat():
    registry = WorkerRegistry()
    now = datetime.now(timezone.utc)

    registry.register("worker-1", now)

    assert registry.get("worker-1") is not None
    assert registry.heartbeat("worker-1", now + timedelta(seconds=1))
    assert registry.get("worker-1").last_heartbeat == now + timedelta(seconds=1)


def test_worker_expiration_detection():
    registry = WorkerRegistry()
    now = datetime.now(timezone.utc)

    registry.register("worker-1", now - timedelta(seconds=100))

    expired = registry.expire_unhealthy(60, now)

    assert expired == ["worker-1"]
    assert registry.get("worker-1").active is False


def test_unknown_worker_heartbeat_fails():
    registry = WorkerRegistry()

    assert registry.heartbeat("missing") is False
