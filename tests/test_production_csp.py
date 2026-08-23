import asyncio
import re
from pathlib import Path

from app.production import ProductionSecurityHeaders, STRICT_CONTENT_SECURITY_POLICY


ROOT = Path(__file__).resolve().parents[1]


def test_production_wrapper_replaces_legacy_inline_script_policy():
    async def legacy_app(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [
                (b'content-security-policy', b"default-src 'self'; script-src 'self' 'unsafe-inline'"),
                (b'x-content-type-options', b'nosniff'),
            ],
        })
        await send({'type': 'http.response.body', 'body': b'ok'})

    sent = []

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    asyncio.run(
        ProductionSecurityHeaders(legacy_app)(
            {'type': 'http', 'method': 'GET', 'path': '/'}, receive, send
        )
    )

    start = sent[0]
    csp_values = [
        value.decode('ascii')
        for name, value in start['headers']
        if name.lower() == b'content-security-policy'
    ]
    assert csp_values == [STRICT_CONTENT_SECURITY_POLICY]
    csp = csp_values[0]
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "script-src-attr 'none'" in csp
    assert "form-action 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_production_entrypoint_matches_external_script_shell_contract():
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

    assert '"app.production:app"' in dockerfile
    assert '"app.main:app"' not in dockerfile

    script_tags = re.findall(r'<script\b[^>]*>.*?</script>', html, flags=re.I | re.S)
    assert script_tags
    assert all(re.search(r'\bsrc="/static/[^"]+"', tag, flags=re.I) for tag in script_tags)
    assert not re.search(r'\son[a-z]+\s*=', html, flags=re.I)
