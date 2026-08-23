from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
BASE_CSS = ROOT / "static" / "styles.css"
REFRESH_CSS = ROOT / "static" / "ui-refresh.css"
UX_CSS = ROOT / "static" / "ux-enhancements.css"
UX_JS = ROOT / "static" / "ux-enhancements.js"
DASHBOARD_JS = ROOT / "static" / "dashboard-enhancements.js"
PRODUCTIVITY_CSS = ROOT / "static" / "productivity-enhancements.css"
PRODUCTIVITY_JS = ROOT / "static" / "productivity-enhancements.js"
OPERATIONAL_CSS = ROOT / "static" / "operational-enhancements.css"
OPERATIONAL_JS = ROOT / "static" / "operational-enhancements.js"
WORKSPACE_CSS = ROOT / "static" / "workspace-preferences.css"
WORKSPACE_JS = ROOT / "static" / "workspace-preferences.js"
COMMAND_CSS = ROOT / "static" / "command-palette.css"
COMMAND_JS = ROOT / "static" / "command-palette.js"
SHORTCUT_CSS = ROOT / "static" / "shortcut-center.css"
SHORTCUT_JS = ROOT / "static" / "shortcut-center.js"
HELP_SECURITY_JS = ROOT / "static" / "help-security.js"
SERVICE_WORKER = ROOT / "static" / "sw.js"


def test_ui_shell_keeps_application_hooks_and_refresh_layer():
    html = INDEX.read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

    assert BASE_CSS.exists()
    assert REFRESH_CSS.exists()
    assert UX_CSS.exists()
    assert UX_JS.exists()
    assert DASHBOARD_JS.exists()
    assert PRODUCTIVITY_CSS.exists()
    assert PRODUCTIVITY_JS.exists()
    assert OPERATIONAL_CSS.exists()
    assert OPERATIONAL_JS.exists()
    assert WORKSPACE_CSS.exists()
    assert WORKSPACE_JS.exists()
    assert COMMAND_CSS.exists()
    assert COMMAND_JS.exists()
    assert SHORTCUT_CSS.exists()
    assert SHORTCUT_JS.exists()
    assert HELP_SECURITY_JS.exists()
    assert SERVICE_WORKER.exists()
    assert 'href="/static/styles.css"' in html
    assert 'href="/static/ui-refresh.css"' in html
    assert 'href="/static/ux-enhancements.css"' in html
    assert 'href="/static/productivity-enhancements.css"' in html
    assert 'href="/static/operational-enhancements.css"' in html
    assert 'href="/static/workspace-preferences.css"' in html
    assert 'href="/static/command-palette.css"' in html
    assert 'href="/static/shortcut-center.css"' in html
    assert 'src="/static/app.js"' in html
    assert 'src="/static/ux-enhancements.js"' in html
    assert 'src="/static/dashboard-enhancements.js"' in html
    assert 'src="/static/productivity-enhancements.js"' in html
    assert 'src="/static/operational-enhancements.js"' in html
    assert 'src="/static/workspace-preferences.js"' in html
    assert 'src="/static/command-palette.js"' in html
    assert 'src="/static/navigation-history.js"' in html
    assert 'src="/static/shortcut-center.js"' in html
    assert 'src="/static/help-security.js"' in html

    html_assets = set(re.findall(r'(?:href|src)="(/static/[^"]+)"', html))
    assert html_assets
    for asset in sorted(html_assets):
        assert f"'{asset}'" in service_worker, f'{asset} is missing from service-worker precache'

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


def test_help_security_override_is_last_and_credential_free():
    html = INDEX.read_text(encoding="utf-8")
    help_js = HELP_SECURITY_JS.read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([^"]+)"', html)

    assert scripts[-1] == '/static/help-security.js'
    assert scripts.index('/static/help-security.js') > scripts.index('/static/app.js')
    assert "helpButton.onclick=openCredentialSafeHelp" in help_js
    assert "Credentials are managed by your administrator" in help_js
    assert "EUAS@2026" not in help_js
    assert "Tech@2026" not in help_js


def test_ui_refresh_includes_responsive_and_motion_safety_rules():
    css = REFRESH_CSS.read_text(encoding="utf-8")
    ux_css = UX_CSS.read_text(encoding="utf-8")
    ux_js = UX_JS.read_text(encoding="utf-8")
    dashboard_js = DASHBOARD_JS.read_text(encoding="utf-8")
    productivity_css = PRODUCTIVITY_CSS.read_text(encoding="utf-8")
    productivity_js = PRODUCTIVITY_JS.read_text(encoding="utf-8")
    operational_css = OPERATIONAL_CSS.read_text(encoding="utf-8")
    operational_js = OPERATIONAL_JS.read_text(encoding="utf-8")
    workspace_css = WORKSPACE_CSS.read_text(encoding="utf-8")
    workspace_js = WORKSPACE_JS.read_text(encoding="utf-8")
    command_css = COMMAND_CSS.read_text(encoding="utf-8")
    command_js = COMMAND_JS.read_text(encoding="utf-8")
    shortcut_css = SHORTCUT_CSS.read_text(encoding="utf-8")
    shortcut_js = SHORTCUT_JS.read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

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

    assert ".table-wrap.has-horizontal-overflow" in productivity_css
    assert ".data-table thead th" in productivity_css
    assert ".required-field" in productivity_css
    assert ".field-invalid" in productivity_css
    assert ".toolbar" in productivity_css
    assert ".module-finder" in productivity_css
    assert ".nav-section-label" in productivity_css
    assert ".module-filter-shortcut" in productivity_css
    assert "@media(max-width:820px)" in productivity_css
    assert "@media(prefers-reduced-motion:reduce)" in productivity_css

    assert "decorateTable" in productivity_js
    assert "decorateForm" in productivity_js
    assert "aria-rowcount" in productivity_js
    assert "aria-required" in productivity_js
    assert "event.ctrlKey || event.metaKey" in productivity_js
    assert "ensureModuleFinder" in productivity_js
    assert "decorateNavigation" in productivity_js
    assert "applyModuleFilter" in productivity_js
    assert "event.altKey" in productivity_js
    assert "Assets & Maintenance" in productivity_js
    assert "MutationObserver" in productivity_js

    assert ".network-activity" in operational_css
    assert ".connection-status" in operational_css
    assert ".operational-error-state" in operational_css
    assert "form[data-euas-submitting=\"true\"]" in operational_css
    assert ".toast.toast-error" in operational_css
    assert "@media(prefers-reduced-motion:reduce)" in operational_css

    assert "installFetchTracking" in operational_js
    assert "markFormBusy" in operational_js
    assert "clearSubmittingForms" in operational_js
    assert "decorateContentState" in operational_js
    assert "navigator.onLine" in operational_js
    assert "operational-retry" in operational_js
    assert "data-euas-submitting" in operational_js
    assert "MutationObserver" in operational_js

    assert ".workspace-sidebar-collapsed" in workspace_css
    assert "body.density-compact" in workspace_css
    assert ".sidebar-collapse-btn" in workspace_css
    assert "@media(min-width:821px)" in workspace_css
    assert "@media(prefers-reduced-motion:reduce)" in workspace_css

    assert "euas_ui_density" in workspace_js
    assert "euas_sidebar_collapsed" in workspace_js
    assert "setDensity" in workspace_js
    assert "setSidebarCollapsed" in workspace_js
    assert "localStorage" in workspace_js
    assert "matchMedia" in workspace_js
    assert "density-toggle" in workspace_js
    assert "sidebar-collapse" in workspace_js

    assert ".command-palette-layer" in command_css
    assert ".command-palette-item.is-active" in command_css
    assert ".command-palette-toggle" in command_css
    assert "@media(max-width:560px)" in command_css
    assert "@media(prefers-reduced-motion:reduce)" in command_css
    assert "@media(prefers-contrast:more)" in command_css

    assert "euas_recent_modules" in command_js
    assert "command-palette-toggle" in command_js
    assert "Control+Shift+P" in command_js
    assert "aria-activedescendant" in command_js
    assert "ArrowDown" in command_js
    assert "ArrowUp" in command_js
    assert "trapFocus" in command_js
    assert "rememberModule" in command_js
    assert "nonRecentModules" in command_js
    assert "MutationObserver" in command_js

    assert ".shortcut-center-layer" in shortcut_css
    assert ".shortcut-center-item" in shortcut_css
    assert ".shortcut-center-toggle" in shortcut_css
    assert "@media(max-width:560px)" in shortcut_css
    assert "@media(prefers-reduced-motion:reduce)" in shortcut_css
    assert "@media(prefers-contrast:more)" in shortcut_css

    assert "Keyboard Shortcuts" in shortcut_js
    assert "shortcut-center-toggle" in shortcut_js
    assert "aria-keyshortcuts" in shortcut_js
    assert "event.key === '?'" in shortcut_js
    assert "isEditable" in shortcut_js
    assert "trapFocus" in shortcut_js
    assert "event.key === 'Escape'" in shortcut_js
    assert "aria-describedby" in shortcut_js
    assert "Browser Back / Forward" in shortcut_js

    assert "euas-shell-v3.9.0-ui12" in service_worker
    assert "'/static/command-palette.css'" in service_worker
    assert "'/static/command-palette.js'" in service_worker
    assert "'/static/shortcut-center.css'" in service_worker
    assert "'/static/shortcut-center.js'" in service_worker
    assert "'/static/navigation-history.js'" in service_worker
    assert "'/static/help-security.js'" in service_worker
    assert "'/static/workspace-preferences.css'" in service_worker
    assert "'/static/operational-enhancements.js'" in service_worker
    assert "url.origin===self.location.origin" in service_worker
    assert "url.pathname==='/'||url.pathname.startsWith('/static/')" in service_worker
    assert "response.ok&&response.type==='basic'" in service_worker
    assert "await cache.put(request,response.clone())" in service_worker
    assert "request.mode==='navigate'" in service_worker
    assert "throw error" in service_worker
