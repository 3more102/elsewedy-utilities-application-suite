# Trusted proxy client identity

EUAS login throttling is keyed partly by client identity. In a direct deployment, the client identity is the immediate socket peer reported by the ASGI server. Forwarded headers are ignored by default.

## Configuration

Set `EUAS_TRUSTED_PROXY_CIDRS` only when EUAS is deployed behind reverse proxies or load balancers that you operate and whose source networks are known.

Example:

```text
EUAS_TRUSTED_PROXY_CIDRS=10.20.0.0/16,fd00:20::/64
```

The setting is a comma-separated list of IPv4 or IPv6 CIDRs. Invalid CIDRs fail application import/startup instead of silently widening trust.

## Security behavior

- If the immediate socket peer is not inside a configured trusted proxy CIDR, EUAS ignores `X-Forwarded-For` completely.
- If the immediate peer is trusted, EUAS parses the `X-Forwarded-For` chain from right to left.
- Traversal continues only while the current hop is trusted and stops at the first untrusted address. That address becomes the client identity used by login throttling.
- Attacker-prepended values farther left cannot override the first untrusted proxy/client boundary.
- Malformed, empty, or excessively long forwarded chains fail closed to the immediate socket peer.
- Forwarded hostnames are not accepted; only IP literals are used.
- Common IPv4-with-port and bracketed IPv6-with-port forms are normalized.

## Deployment requirements

Configure the edge proxy to replace or sanitize inbound `X-Forwarded-For` before appending its own forwarding information. Do not trust broad private-network ranges unless every host in those ranges is controlled as part of the proxy path.

For Kubernetes, ingress-controller, service-mesh, or cloud load-balancer deployments, configure only the source CIDRs that can directly connect to the EUAS application service. Keep application network policy/firewall rules aligned with the same trust boundary.

If `EUAS_TRUSTED_PROXY_CIDRS` is unset or empty, behavior remains backward compatible: the socket peer is the throttle client identity and forwarded headers have no effect.
