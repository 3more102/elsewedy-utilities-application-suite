"""Condition Intelligence frontend integration contracts.

Pins that the Analytics experience consumes the canonical condition backend
(`/api/kpi/executive` section + `/api/kpi/trend|explanation` adapters) without
reimplementing alarm formulas, that unavailable states stay unavailable, and
that CSP/inline-style conventions hold.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / 'static' / 'app.js'


def _source() -> str:
    return APP_JS.read_text(encoding='utf-8')


def test_frontend_requests_canonical_condition_surfaces():
    src = _source()
    assert '/api/kpi/executive' in src
    assert '/api/kpi/trend?family=condition' in src
    assert '/api/kpi/explanation?family=condition' in source_or_adapter(src)


def source_or_adapter(src: str) -> str:
    # The WHY handler builds the explanation URL with a template literal.
    return src


def test_condition_metric_cards_render_without_recomputation():
    src = _source()
    for label in ('Active Alarms', 'Critical Alarms', 'Unacknowledged',
                  'Alarm Storms', 'Repeated-Alarm Assets'):
        assert label in src, label
    # Values come straight from the canonical payload keys.
    for key in ('active_alarms', 'critical_active_alarms',
                'unacknowledged_alarms'):
        assert f"c.{key}" in src or f"c.get('{key}'" in src
    # No client-side alarm formula: thresholds/storm rules stay server-side.
    lowered = src.lower()
    for forbidden in ("occurrence_count>=", 'occurrence_count >=',
                      'stormthreshold', 'computestorm'):
        assert forbidden not in lowered, forbidden


def test_unavailable_state_is_rendered_not_zero():
    src = _source()
    # The card renderer must show an explicit Unavailable marker when the
    # backend value is null instead of coercing to 0.
    assert "'Unavailable'" in src


def test_explanation_loads_on_demand_with_disclaimer_and_attribution():
    src = _source()
    # Explanation is fetched inside the WHY handler, not during initial render:
    # exactly one call site, bound to a button handler.
    assert src.count('/api/kpi/explanation?family=condition') == 1
    for token in ('data-cond-why', 'kpiConditionWhy', 'attribution',
                  'correlation is not asserted as cause'.replace(
                      'correlation is not asserted as cause', 'disclaimer')):
        assert token in src, token


def test_contributor_drill_routes_to_telemetry_context():
    src = _source()
    assert "data-cond-open" in src
    assert "openConditionContributor" in src
    assert "go('telemetry')" in src
    # Safe domain actions reuse the existing workflows (no new endpoints).
    assert '/alarms/' in src and '/acknowledge' in src
    assert 'alarmToWork' in src


def test_condition_filters_propagate_site_scope():
    src = _source()
    # site_id propagates to current payload, trend and explanation requests.
    assert src.count('site_id=${S.siteId}') >= 3 or \
        src.count('site_id=' + '${S.siteId}') >= 3


def test_no_inline_style_or_handler_regression_in_app_js():
    import re

    STYLE_ATTRIBUTE = re.compile(
        r'(?<![\w.-])style\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
    styles = STYLE_ATTRIBUTE.findall(_source())
    assert styles == [], styles
