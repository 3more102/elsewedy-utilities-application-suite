from __future__ import annotations

from .authorization import (
    CAPABILITY_ENFORCED_MUTATION_PREFIXES,
    ROUTE_PERMISSION_OVERLAY,
)


def install_alarm_work_authorization_contract() -> None:
    """Protect alarm-driven work creation with the existing work.create capability."""
    path = '/api/alarms/{alarm_id}/work-order'
    ROUTE_PERMISSION_OVERLAY[('POST', path)] = 'work.create'

    prefixes = CAPABILITY_ENFORCED_MUTATION_PREFIXES.get('work_management', ())
    if path not in prefixes:
        CAPABILITY_ENFORCED_MUTATION_PREFIXES['work_management'] = (*prefixes, path)
