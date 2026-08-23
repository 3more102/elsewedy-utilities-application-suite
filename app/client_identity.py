from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Iterable

from fastapi import Request

from .config import TRUSTED_PROXY_CIDRS

IPAddress = IPv4Address | IPv6Address
MAX_FORWARDED_HOPS = 32


def _trusted_networks(values: Iterable[str]):
    """Parse deployment-owned trusted proxy CIDRs and fail closed on mistakes."""
    return tuple(ip_network(value, strict=False) for value in values)


TRUSTED_PROXY_NETWORKS = _trusted_networks(TRUSTED_PROXY_CIDRS)


def _parse_ip_literal(value: str) -> IPAddress | None:
    raw = (value or '').strip().strip('"')
    if not raw or raw.casefold() == 'unknown':
        return None

    # X-Forwarded-For normally contains bare addresses, but tolerate the common
    # IPv4:port and [IPv6]:port forms without accepting arbitrary hostnames.
    if raw.startswith('['):
        end = raw.find(']')
        if end <= 1:
            return None
        host = raw[1:end]
        suffix = raw[end + 1 :]
        if suffix and (not suffix.startswith(':') or not suffix[1:].isdigit()):
            return None
        raw = host
    else:
        try:
            return ip_address(raw)
        except ValueError:
            if raw.count(':') == 1:
                host, port = raw.rsplit(':', 1)
                if port.isdigit():
                    raw = host

    try:
        return ip_address(raw)
    except ValueError:
        return None


def _is_trusted(address: IPAddress, trusted_networks) -> bool:
    return any(address.version == network.version and address in network for network in trusted_networks)


def _forwarded_chain(header_value: str) -> list[IPAddress] | None:
    if not header_value:
        return []
    parts = [part.strip() for part in header_value.split(',')]
    if not parts or len(parts) > MAX_FORWARDED_HOPS or any(not part for part in parts):
        return None

    chain: list[IPAddress] = []
    for part in parts:
        address = _parse_ip_literal(part)
        if address is None:
            return None
        chain.append(address)
    return chain


def resolve_client_host(request: Request, *, trusted_networks=None) -> str:
    """Return the spoof-resistant client identity used by login throttling.

    Forwarded headers are ignored unless the immediate socket peer belongs to
    an explicitly configured trusted proxy CIDR. For a trusted peer, walk the
    X-Forwarded-For chain from right to left and stop at the first untrusted
    hop. This prevents an attacker-controlled value prepended to the header
    from overriding the actual client/proxy boundary.
    """
    peer = request.client.host if request.client else 'unknown'
    peer_address = _parse_ip_literal(peer)
    networks = TRUSTED_PROXY_NETWORKS if trusted_networks is None else tuple(trusted_networks)

    if peer_address is None or not networks or not _is_trusted(peer_address, networks):
        return peer

    chain = _forwarded_chain(request.headers.get('x-forwarded-for', ''))
    if chain is None or not chain:
        return str(peer_address)

    candidate = peer_address
    for forwarded in reversed(chain):
        if not _is_trusted(candidate, networks):
            break
        candidate = forwarded
    return str(candidate)
