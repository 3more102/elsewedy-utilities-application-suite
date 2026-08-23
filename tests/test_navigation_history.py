from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
NAVIGATION_JS = ROOT / "static" / "navigation-history.js"
SERVICE_WORKER = ROOT / "static" / "sw.js"


def test_navigation_history_preserves_existing_navigation_contract():
    html = INDEX.read_text(encoding="utf-8")
    js = NAVIGATION_JS.read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

    assert NAVIGATION_JS.exists()
    assert 'src="/static/navigation-history.js"' in html
    assert "'/static/navigation-history.js'" in service_worker
    assert "euas-shell-v3.9.0-ui" in service_worker

    assert ".nav-btn[data-view]" in js
    assert "button.click()" in js
    assert "history.pushState" in js
    assert "history.replaceState" in js
    assert "popstate" in js
    assert "hashchange" in js
    assert "URLSearchParams" in js
    assert "MutationObserver" in js
    assert "ROUTE_KEY = 'module'" in js
