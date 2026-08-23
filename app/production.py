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
    b'permissions-policy': b'camera=(self), geolocation=(self), microphone=()',
}
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
                    *PRODUCTION_BROWSER_HEADERS.keys(),
                    *PRODUCTION_ISOLATION_HEADERS.keys(),
                }
                headers = [
                    (name, value)
                    for name, value in message.get('headers', [])
                    if name.lower() not in managed_headers
                ]
                headers.append((b'content-security-policy', self._csp))
                headers.extend(PRODUCTION_BROWSER_HEADERS.items())
                headers.extend(PRODUCTION_ISOLATION_HEADERS.items())
                if is_https:
                    headers.append((b'strict-transport-security', self._hsts))
                message = {**message, 'headers': headers}
            await send(message)

        await self.application(scope, receive, send_with_policy)


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
# production browser-security header set.
app = TrustedProxyScheme(
    ProductionSecurityHeaders(_trusted_host_application(_application))
)
