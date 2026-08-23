from __future__ import annotations

from ipaddress import ip_network

from starlette.requests import Request

from app.client_identity import MAX_FORWARDED_HOPS, _trusted_networks, resolve_client_host


def _request(peer: str, forwarded_for: str = '') -> Request:
    headers = []
    if forwarded_for:
        headers.append((b'x-forwarded-for', forwarded_for.encode('ascii')))
    return Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/',
            'headers': headers,
            'client': (peer, 43123),
            'server': ('euas.test', 443),
            'scheme': 'https',
            'query_string': b'',
        }
    )


def test_trusted_proxy_client_identity_is_fail_closed_and_spoof_resistant():
    trusted_v4 = (ip_network('10.0.0.0/8'),)

    # Forwarded headers are ignored when the socket peer is not explicitly
    # trusted, so an internet client cannot choose its own throttle identity.
    assert resolve_client_host(
        _request('203.0.113.9', '198.51.100.25'), trusted_networks=trusted_v4
    ) == '203.0.113.9'

    # A trusted reverse proxy may supply the real client address.
    assert resolve_client_host(
        _request('10.0.0.10', '198.51.100.25'), trusted_networks=trusted_v4
    ) == '198.51.100.25'

    # Walk from the trusted socket peer inward. The attacker-prepended leftmost
    # address cannot override the first untrusted hop at the proxy boundary.
    assert resolve_client_host(
        _request('10.0.0.10', '6.6.6.6, 198.51.100.25, 10.0.0.20'),
        trusted_networks=trusted_v4,
    ) == '198.51.100.25'

    # Malformed or unreasonably long chains fail closed to the socket peer.
    assert resolve_client_host(
        _request('10.0.0.10', 'not-an-ip'), trusted_networks=trusted_v4
    ) == '10.0.0.10'
    long_chain = ','.join('10.0.0.20' for _ in range(MAX_FORWARDED_HOPS + 1))
    assert resolve_client_host(
        _request('10.0.0.10', long_chain), trusted_networks=trusted_v4
    ) == '10.0.0.10'

    # Common bracketed IPv6-with-port proxy formatting is normalized.
    trusted_v6 = (ip_network('fd00::/8'),)
    assert resolve_client_host(
        _request('fd00::10', '[2001:db8::25]:443'), trusted_networks=trusted_v6
    ) == '2001:db8::25'

    # Bad deployment CIDRs are rejected instead of silently widening trust.
    try:
        _trusted_networks(('not-a-cidr',))
    except ValueError:
        pass
    else:
        raise AssertionError('invalid trusted proxy CIDR must fail closed')
