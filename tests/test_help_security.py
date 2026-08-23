from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'static' / 'index.html'
HELP_SECURITY = ROOT / 'static' / 'help-security.js'
SERVICE_WORKER = ROOT / 'static' / 'sw.js'
APP_JS = ROOT / 'static' / 'app.js'
LEGACY_CREDENTIAL_DISPLAY = re.compile(
    r'\b(?:omar|seif|planner|supervisor|tech1|tech2|store|proc|hse|exec)\s*/\s*\S+@\d{4}\b',
    re.IGNORECASE,
)


def test_help_override_is_last_credential_free_and_precached():
    html = INDEX.read_text(encoding='utf-8')
    help_js = HELP_SECURITY.read_text(encoding='utf-8')
    service_worker = SERVICE_WORKER.read_text(encoding='utf-8')
    app_js = APP_JS.read_text(encoding='utf-8')
    scripts = re.findall(r'<script src="([^"]+)"', html)

    assert HELP_SECURITY.exists()
    assert scripts[-1] == '/static/help-security.js'
    assert scripts.index('/static/help-security.js') > scripts.index('/static/app.js')
    assert 'helpButton.onclick=openCredentialSafeHelp' in help_js
    assert 'Credentials are managed by your administrator' in help_js
    assert 'scrubCredentialText' in help_js
    assert 'new MutationObserver' in help_js
    assert 'NodeFilter.SHOW_TEXT' in help_js
    assert 'EUAS@2026' not in help_js
    assert 'Tech@2026' not in help_js

    # PR #92 removes the historical credential display strings from the source
    # monolith. Keep the last-loaded scrubber as defense in depth for future
    # dynamically generated legacy content, but require the known source debt to
    # stay at zero rather than depending on the guard for current app.js text.
    assert LEGACY_CREDENTIAL_DISPLAY.findall(app_js) == []

    assert "const CACHE='euas-shell-v3.9.0-ui12'" in service_worker
    assert "'/static/help-security.js'" in service_worker
