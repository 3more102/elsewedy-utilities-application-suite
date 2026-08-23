from __future__ import annotations

VALID_HSE_STATUSES = ('Open', 'Investigating', 'Action Required', 'Closed', 'Cancelled')
HIGH_RISK_THRESHOLD = 12
HSE_TRANSITIONS = {
    'Open': {'Open', 'Investigating', 'Action Required', 'Closed', 'Cancelled'},
    'Investigating': {'Investigating', 'Action Required', 'Closed', 'Cancelled'},
    'Action Required': {'Action Required', 'Investigating', 'Closed', 'Cancelled'},
    'Closed': {'Closed'},
    'Cancelled': {'Cancelled'},
}


def risk_score(severity: int, probability: int) -> int:
    return int(severity) * int(probability)


def validate_hse_status(status: str) -> bool:
    return status in VALID_HSE_STATUSES


def is_high_risk(score: int | float) -> bool:
    return float(score) >= HIGH_RISK_THRESHOLD


def validate_hse_transition(current_status: str, target_status: str) -> bool:
    return target_status in HSE_TRANSITIONS.get(current_status, set())
