from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_protected_report_blob_layer_is_strict_loaded_and_precached():
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    report_security = (ROOT / 'static' / 'report-security.js').read_text(encoding='utf-8')
    service_worker = (ROOT / 'static' / 'sw.js').read_text(encoding='utf-8')
    renderer = (ROOT / 'app' / 'report_html.py').read_text(encoding='utf-8')

    assert '/static/report-security.js' in html
    assert html.index('/static/app.js') < html.index('/static/report-security.js')
    assert html.index('/static/report-security.js') < html.index('/static/csp-action-bridge.js')
    assert "'/static/report-security.js'" in service_worker

    assert "contentType!=='text/html'" in report_security
    assert "default-src 'none'" in report_security
    assert "style-src 'self'" in report_security
    assert "script-src 'none'" in report_security
    assert "script-src-attr 'none'" in report_security
    assert "connect-src 'none'" in report_security
    assert "object-src 'none'" in report_security
    assert "form-action 'none'" in report_security
    assert "base-uri 'self'" in report_security
    assert "referrer.content='no-referrer'" in report_security
    assert "window.open(url,'_blank','noopener,noreferrer')" in report_security
    assert "globalThis.openProtected=openProtectedReport" in report_security
    assert 'eval(' not in report_security
    assert 'new Function' not in report_security

    # The blob policy intentionally permits only the external stylesheet used by
    # the escaped server-owned report renderers; those renderers must remain
    # script-free for the policy to stay valid.
    assert "REPORT_STYLESHEET = '/static/report.css'" in renderer
    assert '<script' not in renderer.casefold()
    assert '<style' not in renderer.casefold()
