import asyncio
from html.parser import HTMLParser
from pathlib import Path

from app.production import ProductionSecurityHeaders, STRICT_CONTENT_SECURITY_POLICY


ROOT = Path(__file__).resolve().parents[1]


class _ShellMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.script_attributes: list[dict[str, str | None]] = []
        self.inline_script_bodies: list[str] = []
        self.event_attributes: list[str] = []
        self._inside_script = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.event_attributes.extend(
            name for name, _value in attrs if name.casefold().startswith('on')
        )
        if tag.casefold() == 'script':
            self.script_attributes.append(attributes)
            self._inside_script = True

    def handle_endtag(self, tag):
        if tag.casefold() == 'script':
            self._inside_script = False

    def handle_data(self, data):
        if self._inside_script and data.strip():
            self.inline_script_bodies.append(data)


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
    assert "script-src-elem 'self'" in csp
    assert "script-src-attr 'unsafe-inline'" in csp
    assert "script-src-attr 'none'" not in csp
    assert "form-action 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_production_policy_preserves_legacy_generated_event_handlers_until_refactor():
    app_js = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')

    # Legacy renderers still emit HTML event-handler attributes inside template
    # strings. Keep this regression tied to the real source so the compatibility
    # allowance can be removed as soon as the final handler is migrated.
    generated_handler_markers = sum(
        app_js.count(f' {event}=')
        for event in ('onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown')
    )
    assert generated_handler_markers > 0
    assert "script-src-elem 'self'" in STRICT_CONTENT_SECURITY_POLICY
    assert "script-src-attr 'unsafe-inline'" in STRICT_CONTENT_SECURITY_POLICY
    assert "script-src 'self' 'unsafe-inline'" not in STRICT_CONTENT_SECURITY_POLICY


def test_production_entrypoint_matches_external_script_shell_contract():
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

    assert '"app.production:app"' in dockerfile
    assert '"app.main:app"' not in dockerfile

    parser = _ShellMarkupParser()
    parser.feed(html)
    assert parser.script_attributes
    assert all(
        str(attributes.get('src', '')).startswith('/static/')
        for attributes in parser.script_attributes
    )
    assert not parser.inline_script_bodies
    assert not parser.event_attributes
