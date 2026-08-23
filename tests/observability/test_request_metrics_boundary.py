from apps.observability import RequestMetrics


def test_request_metrics_tracks_latency_status_and_errors():
    metrics = RequestMetrics()
    metrics.record(200, 10.5)
    metrics.record(503, 4.5)

    snapshot = metrics.snapshot()

    assert snapshot['requests_total'] == 2
    assert snapshot['errors_total'] == 1
    assert snapshot['latency_ms_total'] == 15.0
    assert snapshot['latency_ms_avg'] == 7.5
    assert snapshot['status'] == {'200': 1, '503': 1}
    assert snapshot['uptime_seconds'] > 0
