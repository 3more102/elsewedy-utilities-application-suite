"""EUAS Asset Performance Management analytics kernel.

Pure, deterministic condition-monitoring mathematics shared by the health
engine, the deterioration watchlist, alarm correlation, CBM recommendations,
bad-actor ranking and post-maintenance effectiveness reporting.

Design rules (enforced by tests):

* No database access and no FastAPI imports live in this module; every
  function takes plain data and returns plain data so results are fully
  reproducible and unit-testable.
* No machine-learning predictions. Every verdict states its evidence and an
  explicit evidence level. Where data is insufficient the functions say
  ``insufficient_data`` instead of guessing.
* Normalized scores use documented integer/one-decimal formulas (see
  ``compute_asset_health`` and ``compute_asset_risk``); no fabricated decimal
  precision is ever emitted for risk outputs.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Health scoring (documented formula)
# ---------------------------------------------------------------------------
# The asset health score starts at 100 and subtracts capped evidence penalties:
#
#   condition            mapped from the asset condition field
#                        Good 0 / Fair 10 / Warning 25 / Poor 40 / Critical 55
#   criticality          Low 0 / Medium 3 / High 7 / Critical 12
#   status               Operating or Standby 0 / Under Maintenance or
#                        Restricted 10 / anything else 25
#   priority_work        min(25, high-priority open work orders x 7)
#   overdue_work         min(20, overdue work orders x 5)
#   failed_inspections   min(16, failed inspections x 8)
#   sla_breaches         min(10, breached SLA rows x 5)
#   operational_alarms   min(18, active alarms x 5 + active critical x 5)
#   repeat_failures      min(15, completed corrective/failure work orders in
#                        the evaluation window x 5)
#   deterioration        0 / 6 / 12 based on the adverse-trend level reported
#                        by the deterioration engine (none/adverse/severe)
#   downtime_90d         min(10, forced outage hours in window / 4)
#
# score = max(0, min(100, 100 - sum(penalties)))
# Bands: >=85 Healthy, >=70 Monitor, >=50 Warning, else Critical.
HEALTH_BANDS: tuple[tuple[int, str], ...] = (
    (85, 'Healthy'),
    (70, 'Monitor'),
    (50, 'Warning'),
    (0, 'Critical'),
)

_CONDITION_PENALTIES = {'Good': 0, 'Fair': 10, 'Warning': 25, 'Poor': 40, 'Critical': 55}
_CRITICALITY_PENALTIES = {'Low': 0, 'Medium': 3, 'High': 7, 'Critical': 12}
_CALM_STATUSES = ('Operating', 'Standby')
_BUSY_STATUSES = ('Under Maintenance', 'Restricted')


def health_band(score: float) -> str:
    for floor, label in HEALTH_BANDS:
        if score >= floor:
            return label
    return 'Critical'


def compute_asset_health(evidence: dict) -> dict:
    """Turn gathered asset evidence into a one-decimal explainable score.

    ``evidence`` keys (all optional, treated as neutral when absent):
    condition, criticality, status, open_work (list of {priority,target_finish}),
    failed_inspections (int), sla_breaches (int), active_alarms (int),
    active_critical_alarms (int), failures_window (int), trend_level
    ('none'|'adverse'|'severe'), downtime_hours (float).
    """
    condition_penalty = _CONDITION_PENALTIES.get(str(evidence.get('condition')), 15)
    criticality_penalty = _CRITICALITY_PENALTIES.get(str(evidence.get('criticality')), 3)
    status = str(evidence.get('status'))
    if status in _CALM_STATUSES:
        status_penalty = 0
    elif status in _BUSY_STATUSES:
        status_penalty = 10
    elif not status:
        status_penalty = 0
    else:
        status_penalty = 25

    open_work = list(evidence.get('open_work') or ())
    high = sum(1 for w in open_work if w.get('priority') in ('Emergency', 'Critical', 'High'))
    today = str(evidence.get('today') or '')
    overdue = sum(
        1 for w in open_work
        if w.get('target_finish') and today and str(w['target_finish']) < today
    )
    failed = int(evidence.get('failed_inspections') or 0)
    sla = int(evidence.get('sla_breaches') or 0)
    alarms = int(evidence.get('active_alarms') or 0)
    critical_alarms = int(evidence.get('active_critical_alarms') or 0)
    failures = int(evidence.get('failures_window') or 0)
    downtime_hours = float(evidence.get('downtime_hours') or 0.0)
    trend_level = str(evidence.get('trend_level') or 'none')

    penalties = {
        'condition': condition_penalty,
        'criticality': criticality_penalty,
        'status': status_penalty,
        'priority_work': min(25, high * 7),
        'overdue_work': min(20, overdue * 5),
        'failed_inspections': min(16, failed * 8),
        'sla_breaches': min(10, sla * 5),
        'operational_alarms': min(18, alarms * 5 + critical_alarms * 5),
        'repeat_failures': min(15, failures * 5),
        'deterioration': {'none': 0, 'adverse': 6, 'severe': 12}.get(trend_level, 0),
        'downtime_90d': min(10, int(downtime_hours // 4)),
    }
    score = max(0, min(100, 100 - sum(penalties.values())))

    contributors = [
        {'factor': name, 'points': points}
        for name, points in penalties.items()
        if points
    ]
    contributors.sort(key=lambda item: (-item['points'], item['factor']))
    return {
        'score': round(float(score), 1),
        'band': health_band(score),
        'factors': penalties,
        'contributors': contributors,
    }


# ---------------------------------------------------------------------------
# Risk scoring (separate from health; integer likelihood x consequence)
# ---------------------------------------------------------------------------
# Likelihood (1-5): how likely is a functional failure in the near term?
#   base 1; +2 if health band Critical, +1 if Warning;
#   +1 if any active critical alarm; +1 if >=2 repeat failures in window;
#   +1 if the deterioration engine reports a severe adverse trend.
# Consequence (1-5): operational/safety impact if it fails.
#   starts at 2 (service impact of an in-service utility asset);
#   replaced by the asset criticality floor when higher
#   (Low 2 / Medium 3 / High 4 / Critical 5);
#   +1 if forced outage hours in the evaluation window exceed 24.
# risk_score = likelihood * consequence (integer, 1..25)
# Levels: <=4 Low, <=9 Medium, <=16 High, else Extreme.
RISK_LEVEL_FLOORS: tuple[tuple[int, str], ...] = (
    (17, 'Extreme'),
    (10, 'High'),
    (5, 'Medium'),
    (1, 'Low'),
)

_CRITICALITY_CONSEQUENCE = {'Low': 2, 'Medium': 3, 'High': 4, 'Critical': 5}


def risk_level(risk_score: int) -> str:
    for floor, label in RISK_LEVEL_FLOORS:
        if risk_score >= floor:
            return label
    return 'Low'


def compute_asset_risk(evidence: dict) -> dict:
    band = str(evidence.get('health_band'))
    critical_alarms = int(evidence.get('active_critical_alarms') or 0)
    failures = int(evidence.get('failures_window') or 0)
    severe_trend = bool(evidence.get('severe_trend'))
    downtime_hours = float(evidence.get('downtime_hours') or 0.0)
    criticality = str(evidence.get('criticality'))

    likelihood = 1
    likelihood_contributors = []
    if band == 'Critical':
        likelihood += 2
        likelihood_contributors.append('health state is Critical')
    elif band == 'Warning':
        likelihood += 1
        likelihood_contributors.append('health state is Warning')
    if critical_alarms:
        likelihood += 1
        likelihood_contributors.append(f'{critical_alarms} active critical alarm(s)')
    if failures >= 2:
        likelihood += 1
        likelihood_contributors.append(f'{failures} repeat failure(s) in window')
    if severe_trend:
        likelihood += 1
        likelihood_contributors.append('severe deterioration trend detected')
    likelihood = min(5, likelihood)

    consequence = _CRITICALITY_CONSEQUENCE.get(criticality, 3)
    consequence_contributors = [f'asset criticality {criticality or "unclassified"} (base {consequence})']
    if downtime_hours > 24:
        consequence += 1
        consequence_contributors.append(
            f'{downtime_hours:g}h forced outage in window exceeds 24h'
        )
    consequence = min(5, consequence)

    risk_score = likelihood * consequence
    return {
        'likelihood': likelihood,
        'consequence': consequence,
        'risk_score': risk_score,
        'risk_level': risk_level(risk_score),
        'contributors': {
            'likelihood': likelihood_contributors,
            'consequence': consequence_contributors,
        },
    }


# ---------------------------------------------------------------------------
# Deterministic series analysis (trend / variance / acceleration / excursions)
# ---------------------------------------------------------------------------
def linear_slope(values: Sequence[float]) -> float:
    """Ordinary least squares slope of ``values`` against sample index."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den


def _std(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def classify_series_trend(
    values: Sequence[float],
    *,
    min_points: int = 4,
    relative_change_threshold: float = 0.15,
) -> dict:
    """Classify a numeric reading series as rising/falling/stable.

    A series needs at least ``min_points`` samples and a non-trivial scale to
    be classified; otherwise the result is ``insufficient_data``. The slope is
    judged relative to the magnitude of the series so that noisy flat lines
    around zero do not produce phantom trends.
    """
    clean = [float(v) for v in values]
    if len(clean) < max(2, min_points) or any(math.isnan(v) for v in clean):
        return {
            'state': 'insufficient_data',
            'points': len(clean),
            'slope_per_sample': None,
            'relative_change': None,
        }
    slope = linear_slope(clean)
    scale = max(abs(sum(clean) / len(clean)), 1e-9)
    relative = (slope * (len(clean) - 1)) / scale
    if abs(relative) < relative_change_threshold:
        state = 'stable'
    elif slope > 0:
        state = 'rising'
    else:
        state = 'falling'
    return {
        'state': state,
        'points': len(clean),
        'slope_per_sample': round(slope, 6),
        'relative_change': round(relative, 4),
    }


def classify_variance_trend(values: Sequence[float], *, min_points: int = 6) -> dict:
    """Detect increasing/decreasing dispersion between two halves."""
    clean = [float(v) for v in values]
    half = len(clean) // 2
    if len(clean) < max(4, min_points) or half < 2:
        return {'state': 'insufficient_data', 'first_half_std': None, 'second_half_std': None}
    first = _std(clean[:half])
    second = _std(clean[half:])
    scale = max(abs(sum(clean) / len(clean)), 1e-9)
    change = (second - first) / scale
    if abs(change) < 0.1:
        state = 'stable'
    elif second > first:
        state = 'increasing'
    else:
        state = 'decreasing'
    return {
        'state': state,
        'first_half_std': round(first, 6),
        'second_half_std': round(second, 6),
    }


def classify_acceleration(values: Sequence[float], *, min_points: int = 6) -> dict:
    """Compare slopes of the first and second half of a series."""
    clean = [float(v) for v in values]
    half = len(clean) // 2
    if len(clean) < max(4, min_points) or half < 2:
        return {'state': 'insufficient_data'}
    first_slope = linear_slope(clean[:half])
    second_slope = linear_slope(clean[half:])
    delta = second_slope - first_slope
    scale = max(abs(sum(clean) / len(clean)), 1e-9)
    if abs(delta) / scale < 0.05:
        state = 'steady'
    elif delta > 0:
        state = 'accelerating_deterioration' if second_slope > 0 else 'accelerating_change'
    else:
        state = 'decelerating'
    return {'state': state, 'first_half_slope': round(first_slope, 6), 'second_half_slope': round(second_slope, 6)}


def threshold_level(value: float, thresholds: dict) -> tuple[Optional[str], Optional[float]]:
    """Mirror the operational telemetry severity semantics.

    Critical bounds win over warning bounds; ``*_high`` triggers at or above,
    ``*_low`` at or below. Returns ``(severity, threshold_value)`` or ``(None, None)``.
    """
    checks = (
        ('Critical', 'critical_high', 'high'),
        ('Critical', 'critical_low', 'low'),
        ('Warning', 'warning_high', 'high'),
        ('Warning', 'warning_low', 'low'),
    )
    for severity, key, direction in checks:
        bound = thresholds.get(key)
        if bound is None:
            continue
        bound = float(bound)
        if direction == 'high' and value >= bound:
            return severity, bound
        if direction == 'low' and value <= bound:
            return severity, bound
    return None, None


def excursion_summary(values: Sequence[float], thresholds: dict) -> dict:
    """Count threshold excursions and describe the current abnormal state."""
    clean = [float(v) for v in values]
    warning = critical = 0
    consecutive = 0
    max_consecutive = 0
    current_level: Optional[str] = None
    last_severity: Optional[str] = None
    for value in clean:
        severity, _bound = threshold_level(value, thresholds)
        if severity == 'Warning':
            warning += 1
        elif severity == 'Critical':
            critical += 1
        if severity:
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
            last_severity = severity
        else:
            consecutive = 0
    if clean:
        current_level, _bound = threshold_level(clean[-1], thresholds)
    total = len(clean)
    return {
        'readings': total,
        'warning_excursions': warning,
        'critical_excursions': critical,
        'longest_abnormal_run': max_consecutive,
        'currently_abnormal': bool(current_level),
        'current_level': current_level,
        'last_severity': last_severity,
        'excursion_rate': round((warning + critical) / total, 4) if total else None,
    }


def persistent_abnormal_state(excursions: dict, *, min_consecutive: int = 3) -> bool:
    return excursions['longest_abnormal_run'] >= min_consecutive


def repeated_abnormal_readings(excursions: dict, *, min_repeats: int = 3) -> bool:
    return (excursions['warning_excursions'] + excursions['critical_excursions']) >= min_repeats


# ---------------------------------------------------------------------------
# Deterioration verdicts per channel
# ---------------------------------------------------------------------------
def evaluate_channel_condition(
    values: Sequence[float],
    thresholds: dict,
    *,
    min_points: int = 4,
) -> dict:
    """Deterministic deterioration verdict for one telemetry channel.

    Combines trend direction, excursion history and persistence into a single
    adverse level used by the health engine:
      none    - no adverse evidence
      adverse - rising/falling trend towards a bound, repeated abnormal
                readings or an active abnormal state
      severe  - abnormal readings persist (long run) or a critical excursion
                occurred, or the trend is accelerating while adverse
    """
    values = [float(v) for v in values]
    trend = classify_series_trend(values, min_points=min_points)
    excursions = excursion_summary(values, thresholds)
    acceleration = classify_acceleration(values)
    variance = classify_variance_trend(values)

    adverse_signals: list[str] = []
    severe_signals: list[str] = []
    if trend['state'] in ('rising', 'falling'):
        adverse_signals.append(f"{trend['state']} trend over {trend['points']} readings")
    if excursions['critical_excursions']:
        severe_signals.append(
            f"{excursions['critical_excursions']} critical threshold excursion(s)"
        )
    elif excursions['warning_excursions']:
        adverse_signals.append(
            f"{excursions['warning_excursions']} warning excursion(s)"
        )
    if persistent_abnormal_state(excursions):
        severe_signals.append(
            f"abnormal state persisted across {excursions['longest_abnormal_run']} consecutive readings"
        )
    elif repeated_abnormal_readings(excursions):
        adverse_signals.append('repeated abnormal readings')
    if excursions['currently_abnormal']:
        adverse_signals.append('latest reading outside normal band')

    if (
        trend['state'] in ('rising', 'falling')
        and acceleration['state'].startswith('accelerating')
    ):
        severe_signals.append('deterioration accelerating between halves of the window')

    if severe_signals:
        level = 'severe'
    elif adverse_signals:
        level = 'adverse'
    else:
        level = 'none'

    rationale = severe_signals + adverse_signals
    return {
        'level': level,
        'signals': rationale,
        'trend': trend,
        'excursions': excursions,
        'acceleration': acceleration,
        'variance': variance,
    }


# ---------------------------------------------------------------------------
# Post-maintenance effectiveness
# ---------------------------------------------------------------------------
def maintenance_effectiveness(pre: dict, post: dict) -> dict:
    """Compare like-for-like windows before/after completed maintenance.

    Each side supplies: alarms, critical_alarms, failures, downtime_hours,
    excursions, avg_health. A dimension improves when post is strictly lower
    than pre for burden metrics and strictly higher for average health.
    """
    dimensions = (
        ('alarms', 'lower'),
        ('critical_alarms', 'lower'),
        ('failures', 'lower'),
        ('downtime_hours', 'lower'),
        ('excursions', 'lower'),
        ('avg_health', 'higher'),
    )
    have_any = False
    improved: list[str] = []
    regressed: list[str] = []
    unchanged: list[str] = []
    comparison: dict[str, dict] = {}
    for name, direction in dimensions:
        before = pre.get(name)
        after = post.get(name)
        if before is None or after is None:
            comparison[name] = {'before': before, 'after': after, 'verdict': 'no_data'}
            continue
        have_any = True
        before_f, after_f = float(before), float(after)
        if after_f < before_f and direction == 'lower':
            verdict = 'improved'
        elif after_f > before_f and direction == 'lower':
            verdict = 'regressed'
        elif after_f > before_f and direction == 'higher':
            verdict = 'improved'
        elif after_f < before_f and direction == 'higher':
            verdict = 'regressed'
        else:
            verdict = 'unchanged'
        comparison[name] = {
            'before': before,
            'after': after,
            'verdict': verdict,
        }
        if verdict == 'improved':
            improved.append(name)
        elif verdict == 'regressed':
            regressed.append(name)
        else:
            unchanged.append(name)

    if not have_any:
        overall = 'insufficient_data'
    elif regressed and not improved:
        overall = 'regressed'
    elif improved and not regressed:
        overall = 'improved'
    elif improved and regressed:
        overall = 'mixed'
    else:
        overall = 'unchanged'

    return {
        'verdict': overall,
        'improved': improved,
        'regressed': regressed,
        'comparison': comparison,
        'recurring_issues': list(post.get('recurring_issues') or ()),
    }


# ---------------------------------------------------------------------------
# Bad-actor ranking (documented weighted points)
# ---------------------------------------------------------------------------
# Per-asset bad-actor points (all integers, capped):
#   failures        min(30, corrective completions x 6)
#   emergency_work  min(15, emergency/critical priority WOs x 5)
#   downtime        min(25, forced outage hours / 4)
#   alarms          min(15, alarms in window x 3)
#   cost            min(15, maintenance cost / 2000 currency units)
# MTBF/MTTR are reported as context, not scored directly: with sparse failure
# counts they would double-count the same evidence already covered by
# ``failures`` and ``downtime``.
BAD_ACTOR_WEIGHTS: dict[str, int] = {
    'failures_points': 30,
    'emergency_points': 15,
    'downtime_points': 25,
    'alarm_points': 15,
    'cost_points': 15,
}


def bad_actor_metrics(
    *,
    corrective_completions: int,
    emergency_count: int,
    downtime_hours: float,
    alarm_count: int,
    maintenance_cost: float,
    operating_hours: float,
) -> dict:
    corrective = int(corrective_completions)
    mtbf = round(operating_hours / corrective, 1) if corrective >= 2 and operating_hours > 0 else None
    repair_events = max(corrective, 1)
    mttr = round(downtime_hours / repair_events, 1) if downtime_hours > 0 else None
    return {
        'corrective_completions': corrective,
        'emergency_count': int(emergency_count),
        'downtime_hours': round(float(downtime_hours), 1),
        'alarm_count': int(alarm_count),
        'maintenance_cost': round(float(maintenance_cost), 2),
        'mtbf_hours': mtbf,
        'mttr_hours': mttr,
    }


def rank_bad_actors(metrics_by_asset: dict[str, dict]) -> list[dict]:
    ranked = []
    for asset_no, m in metrics_by_asset.items():
        points = {
            'failures': min(BAD_ACTOR_WEIGHTS['failures_points'], m['corrective_completions'] * 6),
            'emergency_work': min(BAD_ACTOR_WEIGHTS['emergency_points'], m['emergency_count'] * 5),
            'downtime': min(BAD_ACTOR_WEIGHTS['downtime_points'], int(m['downtime_hours'] // 4)),
            'alarms': min(BAD_ACTOR_WEIGHTS['alarm_points'], m['alarm_count'] * 3),
            'cost': min(BAD_ACTOR_WEIGHTS['cost_points'], int(m['maintenance_cost'] // 2000)),
        }
        total = sum(points.values())
        drivers = [f'{name}: {value} points' for name, value in sorted(points.items(), key=lambda kv: -kv[1]) if value]
        ranked.append({
            'asset_no': asset_no,
            'bad_actor_points': total,
            'points': points,
            'drivers': drivers,
            **m,
        })
    ranked.sort(key=lambda item: (-item['bad_actor_points'], item['asset_no']))
    return ranked
