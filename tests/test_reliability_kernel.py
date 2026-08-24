from __future__ import annotations

import math

from app.reliability import (
    bad_actor_metrics,
    classify_acceleration,
    classify_series_trend,
    classify_variance_trend,
    compute_asset_health,
    compute_asset_risk,
    excursion_summary,
    health_band,
    maintenance_effectiveness,
    rank_bad_actors,
    risk_level,
    threshold_level,
)


def test_trend_classification_states():
    rising = classify_series_trend([10, 11, 12.2, 13.5, 15, 16.5])
    falling = classify_series_trend([16.5, 15, 13.5, 12.2, 11, 10])
    stable = classify_series_trend([10, 10.1, 9.9, 10.05, 10.0, 9.95])

    assert rising['state'] == 'rising'
    assert falling['state'] == 'falling'
    assert stable['state'] == 'stable'


def test_trend_insufficient_and_degenerate_data():
    assert classify_series_trend([])['state'] == 'insufficient_data'
    assert classify_series_trend([1.0, 2.0])['state'] == 'insufficient_data'
    flat_zero = [0.0, 0.0, 0.0, 0.0]
    result = classify_series_trend(flat_zero)
    assert result['state'] in ('stable', 'insufficient_data')

    noisy_nan = [1.0, float('nan'), 3.0, 4.0, 5.0]
    assert classify_series_trend(noisy_nan)['state'] == 'insufficient_data'

    # A huge jump between two samples is still not enough evidence.
    two_points = classify_series_trend([10.0, 90.0], min_points=4)
    assert two_points['state'] == 'insufficient_data'


def test_variance_and_acceleration():
    increasing_dispersion = [10, 10, 10, 10, 4, 16, 2, 18]
    variance = classify_variance_trend(increasing_dispersion)
    assert variance['state'] == 'increasing'
    assert variance['second_half_std'] > variance['first_half_std']

    assert classify_variance_trend([5, 5, 5])['state'] == 'insufficient_data'

    accelerating = [0, 0.1, 0.2, 0.3, 2, 6, 12, 20]
    accel = classify_acceleration(accelerating)
    assert accel['second_half_slope'] > accel['first_half_slope']
    assert accel['state'].startswith('accelerating')

    assert classify_acceleration([1, 2, 3])['state'] == 'insufficient_data'


def test_threshold_boundaries_match_operational_semantics():
    thresholds = {'warning_high': 80, 'critical_high': 90, 'warning_low': 20}

    # At or above a bound triggers (>= semantics like live telemetry).
    assert threshold_level(80, thresholds) == ('Warning', 80)
    assert threshold_level(89.999, thresholds) == ('Warning', 80)
    assert threshold_level(90, thresholds) == ('Critical', 90)
    assert threshold_level(19.999, thresholds) == ('Warning', 20)
    assert threshold_level(50, thresholds) == (None, None)

    no_bounds = threshold_level(1000, {})
    assert no_bounds == (None, None)


def test_excursion_summary_counts_and_persistence():
    values = [50, 85, 92, 95, 40, 82, 30, 33]
    summary = excursion_summary(values, {'warning_high': 80, 'critical_high': 90})
    assert summary['warning_excursions'] == 2  # 85 and 82
    assert summary['critical_excursions'] == 2  # 92 and 95
    assert summary['longest_abnormal_run'] == 3  # 85, 92, 95
    assert summary['currently_abnormal'] is False  # last value (33) is normal
    assert summary['excursion_rate'] is not None

    still_open = excursion_summary([50, 91], {'warning_high': 80, 'critical_high': 90})
    assert still_open['currently_abnormal'] is True
    assert still_open['current_level'] == 'Critical'

    empty = excursion_summary([], {'warning_high': 80})
    assert empty['readings'] == 0 and empty['excursion_rate'] is None


def test_health_formula_exact_arithmetic():
    clean_evidence = {
        'condition': 'Good',
        'criticality': 'Low',
        'status': 'Operating',
        'open_work': [],
        'failed_inspections': 0,
        'sla_breaches': 0,
        'active_alarms': 0,
        'active_critical_alarms': 0,
        'failures_window': 0,
        'trend_level': 'none',
        'downtime_hours': 0,
        'today': '2026-08-24',
    }
    result = compute_asset_health(clean_evidence)
    assert result['score'] == 100.0
    assert result['band'] == 'Healthy'
    assert result['contributors'] == []

    # Documented caps: 4 high-priority WOs -> min(25, 4*7=28) = 25.
    capped = compute_asset_health({**clean_evidence, 'open_work': [
        {'priority': 'High'}, {'priority': 'High'},
        {'priority': 'Critical'}, {'priority': 'Emergency'},
    ]})
    assert capped['factors']['priority_work'] == 25

    # Deterioration levels map to 0/6/12.
    adverse = compute_asset_health({**clean_evidence, 'trend_level': 'adverse'})
    severe = compute_asset_health({**clean_evidence, 'trend_level': 'severe'})
    assert adverse['factors']['deterioration'] == 6
    assert severe['factors']['deterioration'] == 12

    # Downtime: 17h -> floor(17/4)=4 points; 400h capped at 10.
    downtime = compute_asset_health({**clean_evidence, 'downtime_hours': 17})
    assert downtime['factors']['downtime_90d'] == 4
    maxed = compute_asset_health({**clean_evidence, 'downtime_hours': 400})
    assert maxed['factors']['downtime_90d'] == 10

    # Repeat failures: 3 completions x 5 = 15 (cap reached at exactly 3).
    repeats = compute_asset_health({**clean_evidence, 'failures_window': 3})
    assert repeats['factors']['repeat_failures'] == 15

    # Score floors at zero and contributors sort by descending weight.
    wrecked = compute_asset_health({
        **clean_evidence,
        'condition': 'Critical',
        'criticality': 'Critical',
        'status': 'Decommissioned',
        'active_alarms': 5,
        'failures_window': 4,
        'trend_level': 'severe',
        'downtime_hours': 200,
    })
    assert wrecked['score'] == 0.0
    points = [c['points'] for c in wrecked['contributors']]
    assert points == sorted(points, reverse=True)
    assert sum(points) == sum(wrecked['factors'].values())


def test_health_bands():
    assert health_band(100) == 'Healthy'
    assert health_band(85) == 'Healthy'
    assert health_band(84.9) == 'Monitor'
    assert health_band(70) == 'Monitor'
    assert health_band(69.9) == 'Warning'
    assert health_band(50) == 'Warning'
    assert health_band(49.9) == 'Critical'
    assert health_band(0) == 'Critical'


def test_risk_model_integer_matrix_with_explanations():
    base = {
        'health_band': 'Healthy',
        'active_critical_alarms': 0,
        'failures_window': 0,
        'severe_trend': False,
        'downtime_hours': 0,
        'criticality': 'Medium',
    }
    low = compute_asset_risk(base)
    assert low['likelihood'] == 1
    assert low['consequence'] == 3
    assert low['risk_score'] == 3
    assert low['risk_level'] == 'Low'
    assert low['contributors']['likelihood'] == []

    worst = compute_asset_risk({**base,
        'health_band': 'Critical',
        'active_critical_alarms': 2,
        'failures_window': 4,
        'severe_trend': True,
        'downtime_hours': 72,
        'criticality': 'Critical',
    })
    # likelihood 1+2+1+1+1 = 5 (capped), consequence 5+1 = 5 (capped)
    assert worst['likelihood'] == 5
    assert worst['consequence'] == 5
    assert worst['risk_score'] == 25
    assert worst['risk_level'] == 'Extreme'
    assert any('critical alarm' in c for c in worst['contributors']['likelihood'])
    assert any('outage' in c for c in worst['contributors']['consequence'])

    # No fabricated precision: everything stays integral.
    for value in (
        low['likelihood'], low['consequence'], low['risk_score'],
        worst['likelihood'], worst['consequence'], worst['risk_score'],
    ):
        assert isinstance(value, int)


def test_risk_levels_and_likelihood_cap():
    assert risk_level(1) == 'Low'
    assert risk_level(4) == 'Low'
    assert risk_level(5) == 'Medium'
    assert risk_level(9) == 'Medium'
    assert risk_level(10) == 'High'
    assert risk_level(16) == 'High'
    assert risk_level(17) == 'Extreme'

    saturated = compute_asset_risk({
        'health_band': 'Critical',
        'active_critical_alarms': 9,
        'failures_window': 99,
        'severe_trend': True,
        'downtime_hours': 0,
        'criticality': 'Low',
    })
    assert saturated['likelihood'] == 5


def test_maintenance_effectiveness_verdicts():
    insufficient = maintenance_effectiveness({}, {})
    assert insufficient['verdict'] == 'insufficient_data'

    improved = maintenance_effectiveness(
        {'alarms': 6, 'critical_alarms': 2, 'avg_health': 55},
        {'alarms': 1, 'critical_alarms': 0, 'avg_health': 78},
    )
    assert improved['verdict'] == 'improved'
    assert set(improved['improved']) == {'alarms', 'critical_alarms', 'avg_health'}

    regressed = maintenance_effectiveness({'alarms': 1}, {'alarms': 5})
    assert regressed['verdict'] == 'regressed'

    mixed = maintenance_effectiveness(
        {'alarms': 5, 'excursions': 1},
        {'alarms': 1, 'excursions': 4},
    )
    assert mixed['verdict'] == 'mixed'
    assert mixed['regressed'] == ['excursions']

    unchanged = maintenance_effectiveness({'alarms': 3}, {'alarms': 3})
    assert unchanged['verdict'] == 'unchanged'

    recurring = maintenance_effectiveness(
        {'alarms': 3},
        {'alarms': 3, 'recurring_issues': [{'channel_code': 'TEL-X'}]},
    )
    assert recurring['recurring_issues'][0]['channel_code'] == 'TEL-X'


def test_bad_actor_ranking_ordering_caps_and_drivers():
    metrics = {
        'AST-QUIET': bad_actor_metrics(
            corrective_completions=0, emergency_count=0,
            downtime_hours=0, alarm_count=0,
            maintenance_cost=0, operating_hours=8760,
        ),
        'AST-BAD': bad_actor_metrics(
            corrective_completions=9, emergency_count=4,
            downtime_hours=120, alarm_count=8,
            maintenance_cost=50000, operating_hours=8760,
        ),
        'AST-MID': bad_actor_metrics(
            corrective_completions=2, emergency_count=1,
            downtime_hours=8, alarm_count=1,
            maintenance_cost=3000, operating_hours=8760,
        ),
    }
    ranked = rank_bad_actors(metrics)
    assert [r['asset_no'] for r in ranked][:1] == ['AST-BAD']

    bad = ranked[0]
    # Caps: failures min(30, 54)=30, emergency min(15,20)=15,
    # downtime min(25,30)=25, alarms min(15,24)=15, cost min(15,25)=15.
    assert bad['points'] == {
        'failures': 30, 'emergency_work': 15, 'downtime': 25,
        'alarms': 15, 'cost': 15,
    }
    assert bad['bad_actor_points'] == 100
    assert bad['drivers'][0].startswith('failures')
    assert bad['mtbf_hours'] == round(8760 / 9, 1)
    assert bad['mttr_hours'] == round(120 / 9, 1)

    quiet = metrics['AST-QUIET']
    assert quiet['mtbf_hours'] is None  # <2 failures: MTBF would be meaningless
