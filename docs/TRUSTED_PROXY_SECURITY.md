# Trusted proxy client identity and scheme

EUAS login throttling is keyed partly by client identity, and production HSTS depends on the effective request scheme. In a direct deployment, both are derived from the immediate socket peer/request reported by the ASGI server. Forwarded headers are ignored by default.

## Configuration

Set `EUAS_TRUSTED_PROXY_CIDRS` only when EUAS is deployed behind reverse proxies or load balancers that you operate and whose source networks are known.

Example:

```text
EUAS_TRUSTED_PROXY_CIDRS=10.20.0.0/16,fd00:20::/64
```

The setting is a comma-separated list of IPv4 or IPv6 CIDRs. Invalid CIDRs fail application import/startup instead of silently widening trust.

The production container disables Uvicorn proxy-header rewriting. EUAS therefore retains the raw socket peer and applies one application-level proxy trust boundary to both client identity and forwarded transport scheme.

## Security behavior

- If the immediate socket peer is not inside a configured trusted proxy CIDR, EUAS ignores `X-Forwarded-For` and `X-Forwarded-Proto`.
- If the immediate peer is trusted, EUAS parses the `X-Forwarded-For` chain from right to left.
- Traversal continues only while the current hop is trusted and stops at the first untrusted address. That address becomes the client identity used by login throttling.
- Attacker-prepended values farther left cannot override the first untrusted proxy/client boundary.
- Malformed, empty, or excessively long forwarded client chains fail closed to the immediate socket peer.
- Forwarded hostnames are not accepted; only IP literals are used.
- Common IPv4-with-port and bracketed IPv6-with-port forms are normalized.
- A trusted immediate proxy may supply exactly one `X-Forwarded-Proto` value, and only `http` or `https` is accepted case-insensitively.
- Repeated, comma-separated, malformed, or unsupported forwarded-proto values are ignored rather than changing the ASGI scheme.
- Production HSTS is emitted only after a direct HTTPS request or a trusted proxy has supplied the effective `https` scheme.

## Deployment requirements

Configure the edge proxy to replace or sanitize inbound `X-Forwarded-For` and `X-Forwarded-Proto` before supplying its own forwarding information. Do not trust broad private-network ranges unless every host in those ranges is controlled as part of the proxy path.

For Kubernetes, ingress-controller, service-mesh, or cloud load-balancer deployments, configure only the source CIDRs that can directly connect to the EUAS application service. Keep application network policy/firewall rules aligned with the same trust boundary.

Do not enable Uvicorn's independent proxy-header rewriting in front of `app.production:app`; doing so can replace the raw socket peer before EUAS performs its own spoof-resistant trust checks. If a non-Docker deployment launches Uvicorn manually, keep proxy-header handling disabled and use `EUAS_TRUSTED_PROXY_CIDRS` for the application trust boundary.

If `EUAS_TRUSTED_PROXY_CIDRS` is unset or empty, behavior remains fail closed: the socket peer is the throttle client identity, forwarded client/scheme headers have no effect, and HSTS is emitted only when the ASGI server itself receives an HTTPS scheme.
