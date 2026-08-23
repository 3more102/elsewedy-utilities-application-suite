"""Production ASGI entrypoint for deployment-only HTTP security policy.

The historical FastAPI application still emits a compatibility CSP that permits
inline scripts. The browser shell no longer needs inline JavaScript, so the
production entrypoint replaces only that response header after the inner
application/middleware stack has run. Development entrypoints remain unchanged.
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


class ProductionSecurityHeaders:
    """Replace the legacy CSP on HTTP responses while preserving ASGI semantics."""

    def __init__(self, application):
        self.application = application
        self._csp = STRICT_CONTENT_SECURITY_POLICY.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.application(scope, receive, send)
            return

        async def send_with_policy(message):
            if message.get('type') == 'http.response.start':
                headers = [
                    (name, value)
                    for name, value in message.get('headers', [])
                    if name.lower() != b'content-security-policy'
                ]
                headers.append((b'content-security-policy', self._csp))
                message = {**message, 'headers': headers}
            await send(message)

        await self.application(scope, receive, send_with_policy)


app = ProductionSecurityHeaders(_application)
