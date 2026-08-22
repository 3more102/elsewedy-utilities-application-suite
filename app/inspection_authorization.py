from __future__ import annotations

from .authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY


INSPECTION_SUBMIT_ROLES = (
    'admin',
    'maintenance_manager',
    'planner',
    'supervisor',
    'technician',
)


def install_inspection_authorization_contract() -> None:
    permission = 'inspections.submit'
    PERMISSION_CATALOG.setdefault(
        permission,
        ('Submit and complete field inspections', INSPECTION_SUBMIT_ROLES),
    )
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/inspections/{inspection_id}/submit')
    ] = permission
