from __future__ import annotations

from .authorization import (
    CAPABILITY_ENFORCED_MUTATION_PREFIXES,
    ROUTE_PERMISSION_OVERLAY,
)


def install_reservation_authorization_contract() -> None:
    """Extend the existing Work Management contract to indirect reservation URLs.

    These endpoints historically live under ``/api/reservations`` instead of
    ``/api/work-orders`` even though they mutate work-order material state. The
    reused capabilities have default role sets exactly equal to the legacy
    route whitelists, so this is an additive narrowing control only.
    """
    ROUTE_PERMISSION_OVERLAY.update(
        {
            ('POST', '/api/reservations/{reservation_id}/release'): (
                'work.material.reserve'
            ),
            ('POST', '/api/reservations/{reservation_id}/issue'): (
                'work.material.issue'
            ),
        }
    )

    prefixes = CAPABILITY_ENFORCED_MUTATION_PREFIXES.get('work_management', ())
    reservation_prefix = '/api/reservations'
    if reservation_prefix not in prefixes:
        CAPABILITY_ENFORCED_MUTATION_PREFIXES['work_management'] = (
            *prefixes,
            reservation_prefix,
        )
