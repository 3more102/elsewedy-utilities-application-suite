from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "static" / "dashboard-enhancements.js"
DASHBOARD_CSS = ROOT / "static" / "dashboard-intelligence.css"
SERVICE_WORKER = ROOT / "static" / "sw.js"


def test_dashboard_intelligence_uses_canonical_kpi_contracts():
    js = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "/api/kpi/trend" in js
    assert "/api/kpi/explanation" in js
    assert "period_end" in js
    assert "site_id" in js
    assert "metric: 'overdue_work_orders'" in js
    assert "metric: 'pm_compliance_pct'" in js
    assert "portfolioOnly: true" in js
    assert "KPI_ROLES" in js
    assert "currentIntelligenceConfig" in js
    assert "drivers" in js
    assert "drill" in js
    assert "attribution" in js
    assert "sample.value != null" in js
    assert "if (value == null) return '—';" in js
    assert "innerHTML" not in js
    assert "onclick=" not in js
    assert "style=" not in js


def test_dashboard_intelligence_rejects_non_equivalent_legacy_cards():
    js = DASHBOARD_JS.read_text(encoding="utf-8")

    # These legacy dashboard cards do not share the same calculation basis as
    # their similarly named canonical KPI, or are point-in-time state that
    # cannot support an honest historical trend. They must not be cross-wired.
    unsafe_metric_bindings = (
        "metric: 'open_work_orders'",
        "metric: 'emergency_work_orders'",
        "metric: 'mtbf_hours'",
        "metric: 'mttr_hours'",
        "metric: 'open_incidents'",
        "metric: 'active_alarms'",
        "metric: 'maintenance_cost_window'",
    )
    for binding in unsafe_metric_bindings:
        assert binding not in js

    assert "if (config.portfolioOnly && siteValue) return null;" in js
    assert "Canonical intelligence for this card is portfolio-only." in js


def test_dashboard_intelligence_is_external_responsive_and_offline_cached():
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

    assert "dashboard-intelligence.css" in js
    assert "'/static/dashboard-intelligence.css'" in service_worker
    assert ".dashboard-kpi-intelligence" in css
    assert ".dashboard-intelligence-panel" in css
    assert ".dashboard-driver-open" in css
    assert "@media(max-width:820px)" in css
    assert "@media(max-width:560px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "@media(prefers-contrast:more)" in css
