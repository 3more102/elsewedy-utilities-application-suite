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
    assert "open_work_orders" in js
    assert "overdue_work_orders" in js
    assert "pm_compliance_pct" in js
    assert "active_alarms" in js
    assert "maintenance_cost_window" in js
    assert "drivers" in js
    assert "drill" in js
    assert "attribution" in js
    assert "sample.value != null" in js
    assert "if (value == null) return '—';" in js
    assert "innerHTML" not in js
    assert "onclick=" not in js
    assert "style=" not in js


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
