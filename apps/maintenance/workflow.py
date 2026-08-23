from __future__ import annotations


TRANSITIONS = {
    'Draft': {'submit': 'Submitted'},
    'Rejected': {'resubmit': 'Submitted'},
    'Submitted': {'approve': 'Approved'},
    'Approved': {'assign': 'Assigned'},
    'Assigned': {'start': 'In Progress'},
    'In Progress': {'pause': 'Assigned', 'complete': 'Completed'},
    'Completed': {'close': 'Closed'},
}

ACTION_ROLES = {
    'approve': ('admin', 'maintenance_manager', 'supervisor'),
    'assign': ('admin', 'maintenance_manager', 'planner', 'supervisor'),
    'close': ('admin', 'maintenance_manager', 'supervisor'),
    'submit': ('admin', 'maintenance_manager', 'planner', 'supervisor'),
    'resubmit': ('admin', 'maintenance_manager', 'planner', 'supervisor'),
}


class InvalidWorkTransition(ValueError):
    pass


class WorkTransitionForbidden(PermissionError):
    pass


def transition_target(current_status: str, action: str) -> str:
    normalized = action.lower().strip()
    target = TRANSITIONS.get(current_status, {}).get(normalized)
    if not target:
        raise InvalidWorkTransition(f"Action '{action}' is not valid from {current_status}")
    return target


def validate_transition_actor(work_order: dict, action: str, user: dict) -> None:
    normalized = action.lower().strip()
    allowed_roles = ACTION_ROLES.get(normalized)
    if allowed_roles and user['role'] not in allowed_roles:
        raise WorkTransitionForbidden(f"Role {user['role']} cannot perform {normalized}")
    if (
        user['role'] == 'technician'
        and normalized in ('start', 'pause', 'complete')
        and work_order.get('assigned_to') != user['id']
    ):
        raise WorkTransitionForbidden('Technicians can only execute work assigned to them')
    if normalized == 'assign' and not work_order.get('assigned_to'):
        raise InvalidWorkTransition('Assign a technician before moving to Assigned')
