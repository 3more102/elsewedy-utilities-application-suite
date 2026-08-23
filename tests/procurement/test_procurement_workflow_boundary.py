import pytest

from apps.procurement import (
    InvalidProcurementTransition,
    purchase_order_receive_target,
    requisition_target,
)


def test_procurement_lifecycle_policy_preserves_existing_states():
    assert requisition_target('Draft', 'submit') == 'Submitted'
    assert requisition_target('Rejected', 'submit') == 'Submitted'
    assert requisition_target('Submitted', 'approve') == 'Approved'
    assert requisition_target('Approved', 'order') == 'Ordered'
    assert requisition_target('Ordered', 'receive') == 'Received'
    with pytest.raises(InvalidProcurementTransition):
        requisition_target('Draft', 'approve')
    assert purchase_order_receive_target('Ordered') == 'Received'
    with pytest.raises(InvalidProcurementTransition):
        purchase_order_receive_target('Received')
