from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
BASE_CSS = ROOT / "static" / "styles.css"
REFRESH_CSS = ROOT / "static" / "ui-refresh.css"
UX_CSS = ROOT / "static" / "ux-enhancements.css"
UX_JS = ROOT / "static" / "ux-enhancements.js"
DASHBOARD_JS = ROOT / "static" / "dashboard-enhancements.js"


def test_ui_shell_keeps_application_hooks_and_refresh_layer():
    html = INDEX.read_text(encoding="utf-8")

    assert BASE_CSS.exists()
    assert REFRESH_CSS.exists()
    assert UX_CSS.exists()
    assert UX_JS.exists()
    assert DASHBOARD_JS.exists()
    assert 'href="/static/styles.css"' in html
    assert 'href="/static/ui-refresh.css"' in html
    assert 'href="/static/ux-enhancements.css"' in html
    assert 'src="/static/app.js"' in html
    assert 'src="/static/ux-enhancements.js"' in html
    assert 'src="/static/dashboard-enhancements.js"' in html

    required_ids = {
        "login",
        "login-form",
        "app",
        "sidebar",
        "nav",
        "logout-btn",
        "mobile-menu",
        "breadcrumb",
        "page-title",
        "global-search",
        "search-results",
        "site-selector",
        "help-btn",
        "notification-btn",
        "profile-btn",
        "content",
        "modal-layer",
        "modal-title",
        "modal-close",
        "modal-body",
        "drawer",
        "drawer-close",
        "drawer-body",
        "toast",
    }
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    assert required_ids <= ids


def test_ui_shell_has_accessibility_landmarks():
    html = INDEX.read_text(encoding="utf-8")

    assert 'class="skip-link" href="#content"' in html
    assert 'aria-label="Primary navigation"' in html
    assert 'aria-label="Global search"' in html
    assert 'role="listbox"' in html
    assert 'aria-controls="sidebar"' in html
    assert 'aria-expanded="false"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'role="status" aria-live="polite"' in html
    assert 'role="alert" aria-live="polite"' in html


def test_ui_refresh_includes_responsive_and_motion_safety_rules():
    css = REFRESH_CSS.read_text(encoding="utf-8")
    ux_css = UX_CSS.read_text(encoding="utf-8")
    ux_js = UX_JS.read_text(encoding="utf-8")
    dashboard_js = DASHBOARD_JS.read_text(encoding="utf-8")

    assert ".nav-btn.active" in css
    assert ":focus-visible" in css
    assert "@media(max-width:820px)" in css
    assert "@media(max-width:560px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "@media(prefers-contrast:more)" in css
    assert "@media print" in css

    assert ".mobile-scrim" in ux_css
    assert ".loading-state" in ux_css
    assert ".search-item.keyboard-active" in ux_css
    assert ".dashboard-status-strip" in ux_css
    assert ".dashboard-kpi-grid" in ux_css
    assert ".kpi-attention" in ux_css
    assert "@media(prefers-reduced-motion:reduce)" in ux_css

    assert "aria-activedescendant" in ux_js
    assert "ArrowDown" in ux_js
    assert "ArrowUp" in ux_js
    assert "event.key === 'Escape'" in ux_js
    assert "trapTab" in ux_js
    assert "MutationObserver" in ux_js

    assert "decorateDashboard" in dashboard_js
    assert "decorateKpis" in dashboard_js
    assert "Executive snapshot" in dashboard_js
    assert "Attention signals" in dashboard_js
    assert "dashboard-chart-panel" in dashboard_js
    assert "aria-label" in dashboard_js
