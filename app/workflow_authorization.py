from __future__ import annotations

from .authorization import (
    CAPABILITY_ENFORCED_MUTATION_PREFIXES,
    PERMISSION_CATALOG,
    ROUTE_PERMISSION_OVERLAY,
)


def install_workflow_authorization_contract() -> None:
    permission = 'work.dispatch.transition'
    PERMISSION_CATALOG.setdefault(
        permission,
        (
            'Transition an assigned field dispatch through its lifecycle',
            ('admin', 'maintenance_manager', 'planner', 'supervisor', 'technician'),
        ),
    )
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/dispatch/{dispatch_id}/transition')
    ] = permission

    prefixes = CAPABILITY_ENFORCED_MUTATION_PREFIXES.get('work_management', ())
    dispatch_prefix = '/api/dispatch'
    if dispatch_prefix not in prefixes:
        CAPABILITY_ENFORCED_MUTATION_PREFIXES['work_management'] = (
            *prefixes,
            dispatch_prefix,
        )
