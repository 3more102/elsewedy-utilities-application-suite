import asyncio
import re
from ipaddress import ip_network
from pathlib import Path

from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.production import (
    PRODUCTION_BROWSER_HEADERS,
    PRODUCTION_ISOLATION_HEADERS,
    ProductionSecurityHeaders,
    STRICT_CONTENT_SECURITY_POLICY,
    STRICT_TRANSPORT_SECURITY,
    TrustedProxyScheme,
)


ROOT = Path(__file__).resolve().parents[1]


def _run_wrapper(inner_headers, *, scheme='http'):
    async def legacy_app(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': list(inner_headers),
        })
        await send({'type': 'http.response.body', 'body': b'ok'})

    sent = []

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    asyncio.run(
        ProductionSecurityHeaders(legacy_app)(
            {
                'type': 'http',
                'method': 'GET',
                'path': '/',
                'scheme': scheme,
            },
            receive,
            send,
        )
    )
    return sent[0]


def _run_forwarded_scheme(peer: str, forwarded_proto: str, *, scheme='http'):
    observed_schemes = []

    async def inner(scope, receive, send):
        observed_schemes.append(scope.get('scheme'))
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    sent = []

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    application = TrustedProxyScheme(
        ProductionSecurityHeaders(inner),
        trusted_networks=(ip_network('10.0.0.0/8'),),
    )
    asyncio.run(
        application(
            {
                'type': 'http',
                'method': 'GET',
                'path': '/',
                'scheme': scheme,
                'client': (peer, 43123),
                'headers': [(b'x-forwarded-proto', forwarded_proto.encode('ascii'))],
            },
            receive,
            send,
        )
    )
    return observed_schemes[0], sent[0]


def _run_trusted_host_wrapper(host: str):
    async def inner(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    sent = []

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    application = ProductionSecurityHeaders(
        TrustedHostMiddleware(
            inner,
            allowed_hosts=['euas.example.com', '*.ops.example.com'],
            www_redirect=False,
        )
    )
    asyncio.run(
        application(
            {
                'type': 'http',
                'method': 'GET',
                'path': '/',
                'scheme': 'https',
                'headers': [(b'host', host.encode('ascii'))],
            },
            receive,
            send,
        )
    )
    return sent[0]


def test_production_wrapper_replaces_legacy_inline_script_policy():
    start = _run_wrapper([
        (b'content-security-policy', b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"),
        (b'x-content-type-options', b'nosniff'),
        (b'strict-transport-security', b'max-age=0'),
    ])

    csp_values = [
        value.decode('ascii')
        for name, value in start['headers']
        if name.lower() == b'content-security-policy'
    ]
    hsts_values = [
        value.decode('ascii')
        for name, value in start['headers']
        if name.lower() == b'strict-transport-security'
    ]
    assert csp_values == [STRICT_CONTENT_SECURITY_POLICY]
    assert hsts_values == []
    csp = csp_values[0]
    assert "script-src 'self'" in csp
    assert "script-src-elem 'self'" in csp
    assert "script-src-attr 'none'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "style-src 'self'" in csp
    assert "style-src-elem 'self'" in csp
    assert "style-src-attr 'none'" in csp
    assert "style-src 'self' 'unsafe-inline'" not in csp
    assert "style-src-elem 'self' 'unsafe-inline'" not in csp
    assert "style-src-attr 'unsafe-inline'" not in csp
    assert "font-src 'self'" in csp
    assert "media-src 'self'" in csp
    assert "worker-src 'self'" in csp
    assert "manifest-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert '*' not in csp


def test_production_wrapper_emits_one_year_hsts_only_for_https_scope():
    start = _run_wrapper(
        [
            (b'strict-transport-security', b'max-age=0; includeSubDomains'),
            (b'content-security-policy', b"default-src 'self'"),
        ],
        scheme='https',
    )

    hsts_values = [
        value.decode('ascii')
        for name, value in start['headers']
        if name.lower() == b'strict-transport-security'
    ]
    assert hsts_values == [STRICT_TRANSPORT_SECURITY]
    assert hsts_values[0] == 'max-age=31536000'
    assert 'includeSubDomains' not in hsts_values[0]
    assert 'preload' not in hsts_values[0]


def test_trusted_proxy_can_supply_forwarded_https_scheme():
    observed_scheme, start = _run_forwarded_scheme('10.0.0.10', 'HTTPS')
    headers = {name.lower(): value for name, value in start['headers']}
    assert observed_scheme == 'https'
    assert headers[b'strict-transport-security'] == STRICT_TRANSPORT_SECURITY.encode('ascii')

    observed_scheme, start = _run_forwarded_scheme('10.0.0.10', 'http', scheme='https')
    headers = {name.lower(): value for name, value in start['headers']}
    assert observed_scheme == 'http'
    assert b'strict-transport-security' not in headers


def test_forwarded_scheme_is_ignored_outside_trusted_proxy_boundary():
    observed_scheme, start = _run_forwarded_scheme('203.0.113.9', 'https')
    headers = {name.lower(): value for name, value in start['headers']}
    assert observed_scheme == 'http'
    assert b'strict-transport-security' not in headers

    observed_scheme, start = _run_forwarded_scheme('10.0.0.10', 'https,http')
    headers = {name.lower(): value for name, value in start['headers']}
    assert observed_scheme == 'http'
    assert b'strict-transport-security' not in headers


def test_forwarded_http_scheme_does_not_corrupt_websocket_scope():
    observed = []

    async def inner(scope, receive, send):
        observed.append((scope.get('type'), scope.get('scheme')))

    async def receive():
        return {'type': 'websocket.disconnect'}

    async def send(message):
        raise AssertionError(f'unexpected WebSocket send: {message}')

    application = TrustedProxyScheme(
        inner,
        trusted_networks=(ip_network('10.0.0.0/8'),),
    )
    asyncio.run(
        application(
            {
                'type': 'websocket',
                'path': '/socket',
                'scheme': 'wss',
                'client': ('10.0.0.10', 43123),
                'headers': [(b'x-forwarded-proto', b'https')],
            },
            receive,
            send,
        )
    )
    assert observed == [('websocket', 'wss')]


def test_production_wrapper_replaces_browser_and_isolation_headers():
    start = _run_wrapper([
        (b'x-content-type-options', b'legacy'),
        (b'x-frame-options', b'SAMEORIGIN'),
        (b'referrer-policy', b'unsafe-url'),
        (b'permissions-policy', b'camera=(*), geolocation=(*), microphone=(*)'),
        (b'cross-origin-opener-policy', b'unsafe-none'),
        (b'cross-origin-resource-policy', b'cross-origin'),
        (b'x-permitted-cross-domain-policies', b'all'),
    ])
    headers = {
        name.lower(): value
        for name, value in start['headers']
    }
    for name, expected in PRODUCTION_BROWSER_HEADERS.items():
        assert headers[name] == expected
    for name, expected in PRODUCTION_ISOLATION_HEADERS.items():
        assert headers[name] == expected
    assert headers[b'x-content-type-options'] == b'nosniff'
    assert headers[b'x-frame-options'] == b'DENY'
    assert headers[b'referrer-policy'] == b'strict-origin-when-cross-origin'
    assert headers[b'permissions-policy'] == b'camera=(self), geolocation=(self), microphone=()'
    assert headers[b'cross-origin-opener-policy'] == b'same-origin'
    assert headers[b'cross-origin-resource-policy'] == b'same-origin'
    assert headers[b'x-permitted-cross-domain-policies'] == b'none'

    assert _run_trusted_host_wrapper('euas.example.com')['status'] == 204
    assert _run_trusted_host_wrapper('north.ops.example.com')['status'] == 204
    rejected = _run_trusted_host_wrapper('attacker.invalid')
    assert rejected['status'] == 400
    rejected_headers = {name.lower(): value for name, value in rejected['headers']}
    assert rejected_headers[b'x-content-type-options'] == b'nosniff'
    assert rejected_headers[b'x-frame-options'] == b'DENY'
    assert rejected_headers[b'content-security-policy'] == STRICT_CONTENT_SECURITY_POLICY.encode('ascii')
    assert rejected_headers[b'strict-transport-security'] == STRICT_TRANSPORT_SECURITY.encode('ascii')


def test_production_entrypoint_matches_external_script_shell_contract():
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    app_js = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
    action_bridge = (ROOT / 'static' / 'csp-action-bridge.js').read_text(encoding='utf-8')
    postgres_smoke = (ROOT / 'scripts' / 'postgres_smoke_test.py').read_text(encoding='utf-8')
    production_source = (ROOT / 'app' / 'production.py').read_text(encoding='utf-8')

    assert '"app.production:app"' in dockerfile
    assert '"app.main:app"' not in dockerfile
    assert '"--no-proxy-headers"' in dockerfile
    assert '"--proxy-headers"' not in dockerfile
    assert "'app.production:app'" in postgres_smoke
    assert "'app.main:app'" not in postgres_smoke
    assert 'TrustedProxyScheme' in production_source
    assert 'TRUSTED_PROXY_NETWORKS' in production_source
    assert 'TrustedHostMiddleware' in production_source
    assert 'www_redirect=False' in production_source
    assert "must not contain the unrestricted '*' wildcard" in production_source

    script_tags = re.findall(r'<script\b[^>]*>.*?</script>', html, flags=re.I | re.S)
    assert script_tags
    assert all(re.search(r'\bsrc="/static/[^"]+"', tag, flags=re.I) for tag in script_tags)
    assert not re.search(r'\son[a-z]+\s*=', html, flags=re.I)
    assert '<script src="/static/app.js"></script><script src="/static/csp-action-bridge.js"></script>' in html

    handlers = re.findall(r'onclick="([A-Za-z_$][\w$]*)\(([^\"]*)\)"', app_js)
    assert handlers, 'legacy generated actions unexpectedly disappeared; remove the bridge instead of leaving dead compatibility code'
    bridge_signatures = dict(re.findall(r"^\s{4}([A-Za-z_$][\w$]*):'(n|nn|s|ns)',?$", action_bridge, flags=re.M))
    assert bridge_signatures
    assert {name for name, _ in handlers} <= bridge_signatures.keys()
    for name, arguments in handlers:
        parts = [part.strip() for part in arguments.split(',', 1)]
        if len(parts) == 1:
            source_signature = 'n' if parts[0].startswith('${') else 's'
        else:
            source_signature = 'nn' if parts[1].startswith('${') else 'ns'
        assert bridge_signatures[name] == source_signature, (name, arguments, bridge_signatures[name], source_signature)

    assert "removeAttribute('onclick')" in action_bridge
    assert "attributeFilter:['onclick']" in action_bridge
    assert "new MutationObserver" in action_bridge
    assert "closest?.('[data-euas-action]')" in action_bridge
    assert 'globalThis[name]' in action_bridge
    assert 'JSON.parse(decodeURIComponent(' in action_bridge
    assert 'eval(' not in action_bridge
    assert 'new Function' not in action_bridge

    application_source = (ROOT / 'app' / 'application.py').read_text(encoding='utf-8')
    report_renderer_source = (ROOT / 'app' / 'report_html.py').read_text(encoding='utf-8')
    report_css = (ROOT / 'static' / 'report.css').read_text(encoding='utf-8')
    assert '/static/report.css' not in application_source
    assert "REPORT_STYLESHEET = '/static/report.css'" in report_renderer_source
    assert 'render_snapshot_report_html(r,d)' in application_source
    assert 'render_work_order_report_html(w,labor,mats)' in application_source
    assert '<style' not in application_source.casefold()
    assert '<style' not in report_renderer_source.casefold()
    assert '.report{' in report_css
