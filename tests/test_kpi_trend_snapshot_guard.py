"""Structural guard: snapshot-only metrics must not be registered as trends.

A trend sample claims "this metric, evaluated over that historical window".
Canonical computations that only read current row state (asset statuses,
live reservations, present-day stock or PO status) cannot honour that
claim: registering them fabricates a flat history in which every past
bucket silently shows today's situation.

Regressions introduced by PR #152 were reverted for exactly this reason
(review findings: asset state and parts-readiness/PO-overdue computes
ignore the window). These tests keep them — and any future re-attempt —
out of the trend registry until as-of evaluation genuinely exists.

Known pre-existing exception: ``inventory/stockout_lines`` is also
snapshot-based but was merged earlier with dashboard consumers; it needs a
temporal redesign rather than silent removal.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

SNAPSHOT_ONLY_TREND_REQUESTS = [
    {'family': 'assets', 'metric': 'unavailable_assets'},
    {'family': 'assets', 'metric': 'critical_unavailable_assets'},
    {'family': 'assets', 'metric': 'assets_in_attention_condition'},
    {'family': 'inventory', 'metric': 'work_blocked_by_parts'},
    {'family': 'inventory', 'metric': 'overdue_purchase_orders'},
]


def _auth(client):
    response = client.post(
        '/api/auth/login',
        json={'username': 'omar', 'password': 'EUAS@2026'},
    )
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_snapshot_only_metrics_are_rejected_as_trends():
    """Registering a point-in-time compute as a trend must fail loudly."""
    with TestClient(app) as client:
        headers = _auth(client)
        for params in SNAPSHOT_ONLY_TREND_REQUESTS:
            response = client.get('/api/kpi/trend', headers=headers,
                                  params={'samples': 2, **params})
            assert response.status_code == 404, (params, response.text)
            assert f"unsupported KPI family/metric" in response.json()['detail']


def test_windowed_metrics_remain_trend_capable():
    """Genuinely windowed canonical metrics keep working end to end."""
    with TestClient(app) as client:
        headers = _auth(client)
        for params in (
            {'family': 'reliability', 'metric': 'total_downtime_hours'},
            {'family': 'maintenance', 'metric': 'overdue_work_orders'},
        ):
            response = client.get('/api/kpi/trend', headers=headers,
                                  params={'samples': 2, **params})
            assert response.status_code == 200, (params, response.text)
            payload = response.json()
            assert payload['samples']
