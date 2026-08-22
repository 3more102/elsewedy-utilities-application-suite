from __future__ import annotations

from .authorization import PERMISSION_CATALOG, ROUTE_PERMISSION_OVERLAY


ALARM_ACKNOWLEDGE_ROLES = (
    'admin',
    'asset_manager',
    'maintenance_manager',
    'planner',
    'supervisor',
    'technician',
)
ALARM_CLOSE_ROLES = (
    'admin',
    'asset_manager',
    'maintenance_manager',
    'planner',
    'supervisor',
)


def install_alarm_work_order_authorization() -> None:
    """Apply the existing work.create capability to alarm-generated work."""
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/work-order')
    ] = 'work.create'


def install_alarm_lifecycle_authorization() -> None:
    """Add exact-role capabilities for acknowledge and close mutations."""
    PERMISSION_CATALOG.setdefault(
        'alarms.acknowledge',
        ('Acknowledge active operational alarms', ALARM_ACKNOWLEDGE_ROLES),
    )
    PERMISSION_CATALOG.setdefault(
        'alarms.close',
        ('Close operational alarms', ALARM_CLOSE_ROLES),
    )
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/acknowledge')
    ] = 'alarms.acknowledge'
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/close')
    ] = 'alarms.close'
