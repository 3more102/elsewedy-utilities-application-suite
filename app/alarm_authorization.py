from __future__ import annotations

from .authorization import ROUTE_PERMISSION_OVERLAY


def install_alarm_work_order_authorization() -> None:
    """Apply the existing work.create capability to alarm-generated work.

    Historical route roles are exactly the work.create default role set, so this
    remains an additional narrowing control and cannot promote a legacy-forbidden
    role.
    """
    ROUTE_PERMISSION_OVERLAY[
        ('POST', '/api/alarms/{alarm_id}/work-order')
    ] = 'work.create'
