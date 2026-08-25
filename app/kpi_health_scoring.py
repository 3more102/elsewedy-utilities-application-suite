"""EUAS KPI health scoring primitives.

Deterministic helpers for converting operational KPI values into executive
health indicators. Kept independent from route/store layers so existing
canonical KPI calculations remain the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HealthSignal:
    score: float
    status: str
    reason: str


def bounded_score(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    """Clamp a score into the executive dashboard range."""
    return max(minimum, min(maximum, float(value)))


def availability_health(availability_pct: float) -> HealthSignal:
    score = bounded_score(availability_pct)
    if score >= 98:
        return HealthSignal(score, "healthy", "Availability is within target range")
    if score >= 95:
        return HealthSignal(score, "attention", "Availability requires monitoring")
    return HealthSignal(score, "critical", "Availability is below operational target")


def compliance_health(compliance_pct: float) -> HealthSignal:
    score = bounded_score(compliance_pct)
    if score >= 90:
        return HealthSignal(score, "healthy", "Compliance target achieved")
    if score >= 75:
        return HealthSignal(score, "attention", "Compliance gap detected")
    return HealthSignal(score, "critical", "Compliance requires corrective action")
