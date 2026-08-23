import pytest

from apps.maintenance import (
    InvalidWorkTransition,
    WorkTransitionForbidden,
    transition_target,
    validate_transition_actor,
)


def test_maintenance_state_machine_and_execution_authorization():
    assert transition_target('Draft', 'submit') == 'Submitted'
    assert transition_target('Assigned', 'start') == 'In Progress'
    assert transition_target('In Progress', 'complete') == 'Completed'
    with pytest.raises(InvalidWorkTransition):
        transition_target('Draft', 'complete')

    work = {'assigned_to': 7}
    validate_transition_actor(work, 'start', {'id': 7, 'role': 'technician'})
    with pytest.raises(WorkTransitionForbidden):
        validate_transition_actor(work, 'start', {'id': 8, 'role': 'technician'})
    with pytest.raises(WorkTransitionForbidden):
        validate_transition_actor(work, 'approve', {'id': 7, 'role': 'technician'})
