import pytest

from apps.condition_monitoring import ConditionRuleError, condition_matches, threshold_text, validate_rule


def test_condition_rule_threshold_and_recovery_boundaries():
    rule = {'operator': '>=', 'threshold_low': 80.0, 'threshold_high': None}
    assert condition_matches(rule, 80.0) is True
    assert condition_matches(rule, 79.999) is False
    assert threshold_text(rule) == '>= 80'


def test_range_rule_and_invalid_configuration():
    rule = {'operator': 'outside', 'threshold_low': 10.0, 'threshold_high': 20.0}
    assert condition_matches(rule, 9.0) is True
    assert condition_matches(rule, 15.0) is False
    validate_rule('outside', 10, 20, 'Warning', 'Recommendation', 'High')
    with pytest.raises(ConditionRuleError):
        validate_rule('between', 20, 10, 'Warning', 'Recommendation', 'High')
