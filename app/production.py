"""Production ASGI entrypoint for deployment-only HTTP security policy.

The historical FastAPI application still emits a compatibility CSP that permits
inline scripts. The browser shell no longer needs inline JavaScript, so the
production entrypoint replaces that response header after the inner application
stack has run. The production stack also rejects untrusted Host headers before
requests reach application routing. HTTPS responses receive HSTS; plain HTTP
responses do not, so local/reference deployments are not incorrectly marked as
transport-secure.

Forwarded transport scheme is resolved inside EUAS instead of by Uvicorn. Only
an immediate socket peer covered by ``EUAS_TRUSTED_PROXY_CIDRS`` may supply one
``X-Forwarded-Proto: http|https`` value. This keeps forwarded-scheme trust aligned
with the spoof-resistant client-identity boundary and preserves the raw socket
peer for application-level X-Forwarded-For processing.

All ``/api`` responses are marked private and non-cacheable at the outer
production boundary. That prevents browsers and intermediary caches from
retaining authenticated JSON, reports, metrics or other API payloads even if an
inner route accidentally emits a weaker cache directive. Static/PWA responses
are intentionally left alone so their existing cache strategy remains usable.

The legacy application still mounts ``UPLOAD_DIR`` at ``/uploads`` for reference
compatibility. Production blocks that mount before routing so randomized stored
attachment names can never become an alternate unauthenticated download path;
documents remain available only through the authenticated API download route.

FastAPI's Swagger/OpenAPI introspection routes remain useful in development, but
production blocks them before application routing. This avoids publishing the
full API schema and interactive documentation to unauthenticated clients while
leaving normal API routes and local/reference developer workflows unchanged.

The production CSP already restricts executable and renderable subresources to
the same origin. COOP, CORP, COEP and Origin-Agent-Cluster therefore complete a
strict cross-origin isolation boundary without introducing an external-resource
dependency into the browser shell.
"""
from __future__ import annotations

from ipaddress import ip_address

from starlette.middleware.trustedhost import TrustedHostMiddleware

from .client_identity import TRUSTED_PROXY_NETWORKS
from .config import ALLOWED_HOSTS
from .main import app as _application


STRICT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "media-src 'self'; "
    "style-src 'self'; "
    "style-src-elem 'self'; "
    "style-src-attr 'none'; "
    "script-src 'self'; "
    "script-src-elem 'self'; "
    "script-src-attr 'none'; "
    "worker-src 'self'; "
    "manifest-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
STRICT_TRANSPORT_SECURITY = 'max-age=31536000'
PRODUCTION_BROWSER_HEADERS = {
    b'x-content-type-options': b'nosniff',
    b'x-frame-options': b'DENY',
    b'referrer-policy': b'strict-origin-when-cross-origin',
    b'permissions-policy': b'camera=(), geolocation=(), microphone=()',
}
PRODUCTION_ISOLATION_HEADERS = {
    b'cross-origin-opener-policy': b'same-origin',
    b'cross-origin-resource-policy': b'same-origin',
    b'cross-origin-embedder-policy': b'require-corp',
    b'origin-agent-cluster': b'?1',
    b'x-permitted-cross-domain-policies': b'none',
}
PRODUCTION_API_CACHE_HEADERS = {
    b'cache-control': b'no-store, private, max-age=0',
    b'pragma': b'no-cache',
    b'expires': b'0',
}
PRODUCTION_PRIVATE_INTROSPECTION_PATHS = frozenset({
    '/api/docs',
    '/openapi.json',
    '/docs/oauth2-redirect',
})


class ProductionSecurityHeaders:
    """Apply deployment-only browser, transport and API cache policy."""

    def __init__(self, application):
        self.application = application
        self._csp = STRICT_CONTENT_SECURITY_POLICY.encode('ascii')
        self._hsts = STRICT_TRANSPORT_SECURITY.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.application(scope, receive, send)
            return

        is_https = str(scope.get('scheme', '')).casefold() == 'https'
        path = str(scope.get('path') or '')
        is_api = path == '/api' or path.startswith('/api/')

        async def send_with_policy(message):
            if message.get('type') == 'http.response.start':
                managed_headers = {
                    b'content-security-policy',
                    b'strict-transport-security',
                    *PRODUCTION_BROWSER_HEADERS.keys(),
                    *PRODUCTION_ISOLATION_HEADERS.keys(),
                }
                if is_api:
                    managed_headers.update(PRODUCTION_API_CACHE_HEADERS.keys())
                headers = [
                    (name, value)
                    for name, value in message.get('headers', [])
                    if name.lower() not in managed_headers
                ]
                headers.append((b'content-security-policy', self._csp))
                headers.extend(PRODUCTION_BROWSER_HEADERS.items())
                headers.extend(PRODUCTION_ISOLATION_HEADERS.items())
                if is_api:
                    headers.extend(PRODUCTION_API_CACHE_HEADERS.items())
                if is_https:
                    headers.append((b'strict-transport-security', self._hsts))
                message = {**message, 'headers': headers}
            await send(message)

        await self.application(scope, receive, send_with_policy)


class ProductionPrivateIntrospectionBoundary:
    """Hide FastAPI documentation and schema routes from production clients."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            path = str(scope.get('path') or '')
            if path in PRODUCTION_PRIVATE_INTROSPECTION_PATHS or path.startswith('/api/docs/'):
                body = b'Not Found'
                await send({
                    'type': 'http.response.start',
                    'status': 404,
                    'headers': [
                        (b'content-type', b'text/plain; charset=utf-8'),
                        (b'content-length', str(len(body)).encode('ascii')),
                        (b'cache-control', b'no-store, private, max-age=0'),
                    ],
                })
                await send({'type': 'http.response.body', 'body': body})
                return
        await self.application(scope, receive, send)


class ProductionPrivateUploadBoundary:
    """Deny the legacy unauthenticated ``/uploads`` static mount in production."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            path = str(scope.get('path') or '')
            if path == '/uploads' or path.startswith('/uploads/'):
                body = b'Not Found'
                await send({
                    'type': 'http.response.start',
                    'status': 404,
                    'headers': [
                        (b'content-type', b'text/plain; charset=utf-8'),
                        (b'content-length', str(len(body)).encode('ascii')),
                        (b'cache-control', b'no-store, private, max-age=0'),
                    ],
                })
                await send({'type': 'http.response.body', 'body': body})
                return
        await self.application(scope, receive, send)


class TrustedProxyScheme:
    """Resolve forwarded HTTP scheme only across the configured proxy boundary."""

    def __init__(self, application, *, trusted_networks=None):
        self.application = application
        self.trusted_networks = tuple(
            TRUSTED_PROXY_NETWORKS if trusted_networks is None else trusted_networks
        )

    def _peer_is_trusted(self, scope) -> bool:
        client = scope.get('client')
        if not client or not self.trusted_networks:
            return False
        try:
            address = ip_address(str(client[0]))
        except ValueError:
            return False
        return any(
            address.version == network.version and address in network
            for network in self.trusted_networks
        )

    @staticmethod
    def _forwarded_scheme(scope) -> str | None:
        values = []
        for name, value in scope.get('headers', []):
            if name.lower() != b'x-forwarded-proto':
                continue
            try:
                values.append(value.decode('ascii').strip().casefold())
            except UnicodeDecodeError:
                return None
        if len(values) != 1 or values[0] not in {'http', 'https'}:
            return None
        return values[0]

    async def __call__(self, scope, receive, send):
        # X-Forwarded-Proto uses HTTP transport values. Do not write those into
        # WebSocket scopes, whose ASGI scheme contract is ws/wss rather than
        # http/https. EUAS currently has no production WebSocket proxy contract.
        if scope.get('type') != 'http':
            await self.application(scope, receive, send)
            return

        forwarded_scheme = None
        if self._peer_is_trusted(scope):
            forwarded_scheme = self._forwarded_scheme(scope)
        if forwarded_scheme:
            scope = {**scope, 'scheme': forwarded_scheme}
        await self.application(scope, receive, send)


def _trusted_host_application(application):
    if not ALLOWED_HOSTS:
        raise RuntimeError('EUAS_ALLOWED_HOSTS must contain at least one trusted hostname')
    if '*' in ALLOWED_HOSTS:
        raise RuntimeError("EUAS_ALLOWED_HOSTS must not contain the unrestricted '*' wildcard")
    return TrustedHostMiddleware(
        application,
        allowed_hosts=list(ALLOWED_HOSTS),
        www_redirect=False,
    )


# TrustedProxyScheme only normalizes the request scope. Security headers remain
# outside TrustedHostMiddleware so even a rejected Host response receives the
# production browser-security header set. Private production boundaries sit
# behind host validation but in front of the legacy application router.
app = TrustedProxyScheme(
    ProductionSecurityHeaders(
        _trusted_host_application(
            ProductionPrivateIntrospectionBoundary(
                ProductionPrivateUploadBoundary(_application)
            )
        )
    )
)
