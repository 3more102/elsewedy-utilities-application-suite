from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ATTENTION_JS = ROOT / "static" / "dashboard-attention.js"
ATTENTION_CSS = ROOT / "static" / "dashboard-attention.css"
INDEX = ROOT / "static" / "index.html"
SERVICE_WORKER = ROOT / "static" / "sw.js"


def test_dashboard_attention_uses_existing_severity_without_new_kpi_math():
    js = ATTENTION_JS.read_text(encoding="utf-8")

    assert "kpi-critical" in js
    assert "kpi-attention" in js
    assert "ATTENTION_LIMIT = 6" in js
    assert "No client-side risk score is calculated" in js
    assert "MODULE_BY_SIGNAL" in js
    assert "navButtonFor" in js
    assert "querySelectorAll('.nav-btn')" in js
    assert "button.click()" in js
    assert "dashboard-kpi-action" in js
    assert "Why?" in js
    assert "/api/" not in js
    assert "fetch(" not in js
    assert "innerHTML" not in js
    assert "onclick=" not in js
    assert "style=" not in js


def test_dashboard_attention_assets_are_responsive_and_offline_cached():
    html = INDEX.read_text(encoding="utf-8")
    css = ATTENTION_CSS.read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

    assert 'href="/static/dashboard-attention.css"' in html
    assert 'src="/static/dashboard-attention.js"' in html
    assert "'/static/dashboard-attention.css'" in service_worker
    assert "'/static/dashboard-attention.js'" in service_worker
    assert ".dashboard-attention-center" in css
    assert ".dashboard-attention-row.critical" in css
    assert ".dashboard-attention-state.attention" in css
    assert ".dashboard-attention-action:focus-visible" in css
    assert "@media(max-width:820px)" in css
    assert "@media(max-width:560px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "@media(prefers-contrast:more)" in css
