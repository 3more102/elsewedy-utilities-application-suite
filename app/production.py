"""Production ASGI entrypoint for deployment-only HTTP security policy.

The historical FastAPI application still emits a compatibility CSP that permits
inline scripts. The browser shell no longer needs inline JavaScript, so the
production entrypoint replaces that response header after the inner application
stack has run. HTTPS responses also receive HSTS; plain HTTP responses do not,
so local/reference deployments are not incorrectly marked as transport-secure.
Development entrypoints remain unchanged.
"""
from __future__ import annotations

from .main import app as _application


STRICT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "script-src-attr 'none'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
STRICT_TRANSPORT_SECURITY = 'max-age=31536000'
PRODUCTION_ISOLATION_HEADERS = {
    b'cross-origin-opener-policy': b'same-origin',
    b'cross-origin-resource-policy': b'same-origin',
    b'x-permitted-cross-domain-policies': b'none',
}


class ProductionSecurityHeaders:
    """Apply deployment-only browser and transport policy to HTTP responses."""

    def __init__(self, application):
        self.application = application
        self._csp = STRICT_CONTENT_SECURITY_POLICY.encode('ascii')
        self._hsts = STRICT_TRANSPORT_SECURITY.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.application(scope, receive, send)
            return

        is_https = str(scope.get('scheme', '')).casefold() == 'https'

        async def send_with_policy(message):
            if message.get('type') == 'http.response.start':
                managed_headers = {
                    b'content-security-policy',
                    b'strict-transport-security',
                    *PRODUCTION_ISOLATION_HEADERS.keys(),
                }
                headers = [
                    (name, value)
                    for name, value in message.get('headers', [])
                    if name.lower() not in managed_headers
                ]
                headers.append((b'content-security-policy', self._csp))
                headers.extend(PRODUCTION_ISOLATION_HEADERS.items())
                if is_https:
                    headers.append((b'strict-transport-security', self._hsts))
                message = {**message, 'headers': headers}
            await send(message)

        await self.application(scope, receive, send_with_policy)


app = ProductionSecurityHeaders(_application)
