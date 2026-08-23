import asyncio
import re
from pathlib import Path

from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.production import (
    PRODUCTION_BROWSER_HEADERS,
    PRODUCTION_ISOLATION_HEADERS,
    ProductionSecurityHeaders,
    STRICT_CONTENT_SECURITY_POLICY,
    STRICT_TRANSPORT_SECURITY,
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
    postgres_smoke = (ROOT / 'scripts' / 'postgres_smoke_test.py').read_text(encoding='utf-8')
    production_source = (ROOT / 'app' / 'production.py').read_text(encoding='utf-8')

    assert '"app.production:app"' in dockerfile
    assert '"app.main:app"' not in dockerfile
    assert "'app.production:app'" in postgres_smoke
    assert "'app.main:app'" not in postgres_smoke
    assert 'TrustedHostMiddleware' in production_source
    assert 'www_redirect=False' in production_source
    assert "must not contain the unrestricted '*' wildcard" in production_source

    script_tags = re.findall(r'<script\b[^>]*>.*?</script>', html, flags=re.I | re.S)
    assert script_tags
    assert all(re.search(r'\bsrc="/static/[^"]+"', tag, flags=re.I) for tag in script_tags)
    assert not re.search(r'\son[a-z]+\s*=', html, flags=re.I)
    application_source = (ROOT / 'app' / 'application.py').read_text(encoding='utf-8')
    report_css = (ROOT / 'static' / 'report.css').read_text(encoding='utf-8')
    assert application_source.count('/static/report.css') == 2
    assert '<style' not in application_source.casefold()
    assert '.report{' in report_css
