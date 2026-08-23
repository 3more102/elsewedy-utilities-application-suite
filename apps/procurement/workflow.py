from __future__ import annotations


REQUISITION_TRANSITIONS = {
    'Draft': {'submit': 'Submitted'},
    'Rejected': {'submit': 'Submitted'},
    'Submitted': {'approve': 'Approved'},
    'Approved': {'order': 'Ordered'},
    'Ordered': {'receive': 'Received'},
}


class InvalidProcurementTransition(ValueError):
    pass


def requisition_target(current_status: str, action: str) -> str:
    normalized = action.lower().strip()
    target = REQUISITION_TRANSITIONS.get(current_status, {}).get(normalized)
    if not target:
        raise InvalidProcurementTransition(
            f"Procurement action '{action}' is not valid from {current_status}"
        )
    return target


def purchase_order_receive_target(current_status: str) -> str:
    # Preserve the historical contract: any not-yet-received PO can be received.
    if current_status == 'Received':
        raise InvalidProcurementTransition('PO already received')
    return 'Received'
