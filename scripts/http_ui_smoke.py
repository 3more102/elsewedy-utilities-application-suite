"""Smoke-test the EUAS browser shell through a live HTTP endpoint.

The check deliberately discovers CSS/JS/manifest references from the served root
page instead of duplicating the asset list in CI. This makes the production
container gate fail when a UI layer is referenced by HTML but missing from the
image, served as an empty/fallback response, returned with an implausible
content type, exposes the ASGI server implementation through a Server header,
accidentally publishes FastAPI documentation/schema introspection routes, or
loses the production cross-origin isolation header contract.

Example:
    python scripts/http_ui_smoke.py http://127.0.0.1:8879
"""
from __future__ import annotations

import argparse
import json
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        candidate = None
        if tag == 'script':
            candidate = values.get('src')
        elif tag == 'link':
            rel = {part.casefold() for part in str(values.get('rel', '')).split()}
            if rel.intersection({'stylesheet', 'manifest'}):
                candidate = values.get('href')
        if candidate and str(candidate).startswith('/static/'):
            self.assets.append(str(candidate))


def _fetch(url: str) -> tuple[int, str, dict[str, str], bytes]:
    request = Request(url, headers={'User-Agent': 'EUAS-production-ui-smoke/1.0'})
    with urlopen(request, timeout=10) as response:
        body = response.read()
        headers = {key.casefold(): value for key, value in response.headers.items()}
        return int(response.status), response.geturl(), headers, body


def _fetch_allow_error(url: str) -> tuple[int, str, dict[str, str], bytes]:
    try:
        return _fetch(url)
    except HTTPError as exc:
        body = exc.read()
        headers = {key.casefold(): value for key, value in exc.headers.items()}
        return int(exc.code), exc.geturl(), headers, body


def _expected_content_type(path: str) -> tuple[str, ...]:
    if path.endswith('.css'):
        return ('text/css',)
    if path.endswith('.js'):
        return ('javascript',)
    if path.endswith('.webmanifest'):
        return ('json', 'manifest')
    return ()


def _assert_security_headers(headers: dict[str, str]) -> None:
    assert 'server' not in headers, headers
    assert headers.get('x-content-type-options', '').casefold() == 'nosniff', headers
    assert headers.get('x-frame-options', '').casefold() == 'deny', headers
    assert headers.get('referrer-policy', '').casefold() == 'strict-origin-when-cross-origin', headers
    assert headers.get('permissions-policy', '').casefold() == 'camera=(), geolocation=(), microphone=()', headers
    assert headers.get('cross-origin-opener-policy', '').casefold() == 'same-origin', headers
    assert headers.get('cross-origin-resource-policy', '').casefold() == 'same-origin', headers
    assert headers.get('cross-origin-embedder-policy', '').casefold() == 'require-corp', headers
    assert headers.get('origin-agent-cluster', '') == '?1', headers
    assert headers.get('x-permitted-cross-domain-policies', '').casefold() == 'none', headers
    csp = headers.get('content-security-policy', '')
    assert "default-src 'self'" in csp, csp
    assert "style-src 'self'" in csp, csp
    assert "style-src-attr 'none'" in csp, csp
    assert "script-src 'self'" in csp, csp
    assert "script-src 'self' 'unsafe-inline'" not in csp, csp
    assert "script-src-attr 'none'" in csp, csp
    assert "form-action 'self'" in csp, csp
    assert "frame-ancestors 'none'" in csp, csp
    assert "'unsafe-inline'" not in csp, csp


def run(base_url: str) -> dict:
    base = base_url.rstrip('/') + '/'
    status, final_url, headers, body = _fetch(base)
    assert status == 200, (status, final_url)
    assert urlparse(final_url).path in ('', '/'), final_url
    content_type = headers.get('content-type', '').casefold()
    assert 'text/html' in content_type, content_type
    assert body, 'root HTML response is empty'
    _assert_security_headers(headers)

    html = body.decode('utf-8')
    assert '<title>EUAS' in html, 'served root is not the EUAS browser shell'
    assert 'id="login-form"' in html, 'login shell hook missing from served root'
    assert 'id="content"' in html, 'application content hook missing from served root'

    private_introspection_paths = (
        '/api/docs',
        '/api/docs/',
        '/openapi.json',
        '/docs/oauth2-redirect',
    )
    for path in private_introspection_paths:
        private_status, private_url, private_headers, private_body = _fetch_allow_error(
            urljoin(base, path.lstrip('/'))
        )
        assert private_status == 404, (path, private_status, private_url)
        assert urlparse(private_url).path == path, (path, private_url)
        assert b'Not Found' in private_body, (path, private_body)
        assert 'no-store' in private_headers.get('cache-control', '').casefold(), (path, private_headers)
        _assert_security_headers(private_headers)

    parser = _AssetParser()
    parser.feed(html)
    assets = list(dict.fromkeys(parser.assets))
    assert assets, 'root page references no static browser assets'
    assert '/static/app.js' in assets, 'core app.js is not referenced by root shell'
    assert '/static/styles.css' in assets, 'core styles.css is not referenced by root shell'
    assert '/static/manifest.webmanifest' in assets, 'web manifest is not referenced by root shell'
    assert '/static/help-security.js' in assets, 'credential-safe shell layer is not referenced by root shell'

    # The service worker and report stylesheet are not direct root-shell
    # dependencies. Include them explicitly so the production image gate verifies
    # every deployment-critical static resource needed by the shell and reports.
    for required_asset in ('/static/sw.js', '/static/report.css'):
        if required_asset not in assets:
            assets.append(required_asset)

    checked: list[dict[str, object]] = []
    for path in assets:
        asset_status, asset_url, asset_headers, asset_body = _fetch(urljoin(base, path.lstrip('/')))
        assert asset_status == 200, (path, asset_status, asset_url)
        assert urlparse(asset_url).path == path, (path, asset_url)
        assert asset_body, f'{path} returned an empty response'

        asset_type = asset_headers.get('content-type', '').casefold()
        assert 'text/html' not in asset_type, f'{path} was served as HTML fallback: {asset_type}'
        expected = _expected_content_type(path)
        if expected:
            assert any(token in asset_type for token in expected), (path, asset_type)
        _assert_security_headers(asset_headers)

        if path == '/static/help-security.js':
            help_script = asset_body.decode('utf-8')
            assert 'helpButton.onclick=openCredentialSafeHelp' in help_script
            assert 'scrubCredentialText' in help_script
            assert 'new MutationObserver' in help_script
            assert 'NodeFilter.SHOW_TEXT' in help_script
            assert 'EUAS@2026' not in help_script
            assert 'Tech@2026' not in help_script
        elif path == '/static/report.css':
            report_css = asset_body.decode('utf-8')
            assert '.report{' in report_css
            assert '.snapshot-report table{' in report_css

        checked.append({'path': path, 'bytes': len(asset_body), 'content_type': asset_type})

    return {
        'root_bytes': len(body),
        'assets_checked': len(checked),
        'private_introspection_paths_checked': len(private_introspection_paths),
        'assets': checked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate the live EUAS browser shell and static assets.')
    parser.add_argument('base_url', nargs='?', default='http://127.0.0.1:8879')
    args = parser.parse_args()
    result = run(args.base_url)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
