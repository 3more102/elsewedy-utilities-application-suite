from __future__ import annotations

VALID_HSE_STATUSES = ('Open', 'Investigating', 'Action Required', 'Closed', 'Cancelled')
HIGH_RISK_THRESHOLD = 12


def risk_score(severity: int, probability: int) -> int:
    return int(severity) * int(probability)


def validate_hse_status(status: str) -> bool:
    return status in VALID_HSE_STATUSES


def is_high_risk(score: int | float) -> bool:
    return float(score) >= HIGH_RISK_THRESHOLD
