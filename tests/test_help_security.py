from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'static' / 'index.html'
HELP_SECURITY = ROOT / 'static' / 'help-security.js'
SERVICE_WORKER = ROOT / 'static' / 'sw.js'
APP_JS = ROOT / 'static' / 'app.js'


def test_help_override_is_last_credential_free_and_precached():
    html = INDEX.read_text(encoding='utf-8')
    help_js = HELP_SECURITY.read_text(encoding='utf-8')
    service_worker = SERVICE_WORKER.read_text(encoding='utf-8')
    legacy_app = APP_JS.read_text(encoding='utf-8')
    scripts = re.findall(r'<script src="([^"]+)"', html)

    assert HELP_SECURITY.exists()
    assert scripts[-1] == '/static/help-security.js'
    assert scripts.index('/static/help-security.js') > scripts.index('/static/app.js')
    assert 'helpButton.onclick=openCredentialSafeHelp' in help_js
    assert 'Credentials are managed by your administrator' in help_js
    assert 'scrubCredentialText' in help_js
    assert 'new MutationObserver' in help_js
    assert "NodeFilter.SHOW_TEXT" in help_js
    assert 'EUAS@2026' not in help_js
    assert 'Tech@2026' not in help_js

    # Keep this regression tied to the legacy risk it mitigates: the monolithic
    # app still has historical reference-account copy in multiple render paths,
    # and the last-loaded guard must protect all subsequent DOM writes until the
    # monolith can be decomposed safely.
    assert legacy_app.count('@2026') >= 2

    assert "const CACHE='euas-shell-v3.9.0-ui12'" in service_worker
    assert "'/static/help-security.js'" in service_worker
