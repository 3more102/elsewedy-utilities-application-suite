from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
BASE_CSS = ROOT / "static" / "styles.css"
REFRESH_CSS = ROOT / "static" / "ui-refresh.css"


def test_ui_shell_keeps_application_hooks_and_refresh_layer():
    html = INDEX.read_text(encoding="utf-8")

    assert BASE_CSS.exists()
    assert REFRESH_CSS.exists()
    assert 'href="/static/styles.css"' in html
    assert 'href="/static/ui-refresh.css"' in html

    required_ids = {
        "login",
        "login-form",
        "app",
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
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'role="status" aria-live="polite"' in html
    assert 'role="alert" aria-live="polite"' in html


def test_ui_refresh_includes_responsive_and_motion_safety_rules():
    css = REFRESH_CSS.read_text(encoding="utf-8")

    assert ".nav-btn.active" in css
    assert ":focus-visible" in css
    assert "@media(max-width:820px)" in css
    assert "@media(max-width:560px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "@media(prefers-contrast:more)" in css
    assert "@media print" in css
