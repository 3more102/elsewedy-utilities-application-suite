from __future__ import annotations

import time


class RequestMetrics:
    """In-process HTTP request metrics for the reference deployment."""

    def __init__(self) -> None:
        self._started_at = time.time()
        self._requests_total = 0
        self._errors_total = 0
        self._latency_ms_total = 0.0
        self._status: dict[str, int] = {}

    def record(self, status_code: int, latency_ms: float) -> None:
        self._requests_total += 1
        self._latency_ms_total += float(latency_ms)
        code = str(status_code)
        self._status[code] = self._status.get(code, 0) + 1
        if status_code >= 500:
            self._errors_total += 1

    def snapshot(self) -> dict:
        uptime = max(time.time() - self._started_at, 0.001)
        total = self._requests_total
        return {
            'started_at': self._started_at,
            'uptime_seconds': uptime,
            'requests_total': total,
            'errors_total': self._errors_total,
            'latency_ms_total': self._latency_ms_total,
            'latency_ms_avg': self._latency_ms_total / max(total, 1),
            'status': dict(self._status),
        }


request_metrics = RequestMetrics()


def record_request(status_code: int, latency_ms: float) -> None:
    request_metrics.record(status_code, latency_ms)


def request_metrics_snapshot() -> dict:
    return request_metrics.snapshot()
