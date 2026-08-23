import asyncio
import re
from pathlib import Path

from app.production import (
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


def test_production_wrapper_replaces_legacy_inline_script_policy():
    start = _run_wrapper([
        (b'content-security-policy', b"default-src 'self'; script-src 'self' 'unsafe-inline'"),
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
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "script-src-attr 'none'" in csp
    assert "form-action 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


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


def test_production_entrypoint_matches_external_script_shell_contract():
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

    assert '"app.production:app"' in dockerfile
    assert '"app.main:app"' not in dockerfile

    script_tags = re.findall(r'<script\b[^>]*>.*?</script>', html, flags=re.I | re.S)
    assert script_tags
    assert all(re.search(r'\bsrc="/static/[^"]+"', tag, flags=re.I) for tag in script_tags)
    assert not re.search(r'\son[a-z]+\s*=', html, flags=re.I)
