from __future__ import annotations


class ConditionRuleError(ValueError):
    pass


def condition_matches(rule: dict, value: float) -> bool:
    operator = str(rule.get('operator') or '').strip()
    low = rule.get('threshold_low')
    high = rule.get('threshold_high')
    low = float(low) if low is not None else None
    high = float(high) if high is not None else None
    if operator == '>=':
        return low is not None and value >= low
    if operator == '>':
        return low is not None and value > low
    if operator == '<=':
        return low is not None and value <= low
    if operator == '<':
        return low is not None and value < low
    if operator == 'between':
        return low is not None and high is not None and low <= value <= high
    if operator == 'outside':
        return low is not None and high is not None and (value < low or value > high)
    return False


def threshold_text(rule: dict) -> str:
    operator = rule.get('operator')
    low = rule.get('threshold_low')
    high = rule.get('threshold_high')
    if operator in ('>=', '>', '<=', '<'):
        return f"{operator} {low:g}" if low is not None else str(operator)
    if operator == 'between':
        return f"between {low:g} and {high:g}"
    if operator == 'outside':
        return f"outside {low:g} to {high:g}"
    return str(operator or '')


def validate_rule(operator, threshold_low, threshold_high, severity, action_type, work_priority) -> None:
    if operator not in ('>=', '>', '<=', '<', 'between', 'outside'):
        raise ConditionRuleError('CBM operator must be one of >=, >, <=, <, between, outside')
    if operator in ('>=', '>', '<=', '<') and threshold_low is None:
        raise ConditionRuleError('threshold_low is required for this CBM operator')
    if operator in ('between', 'outside'):
        if threshold_low is None or threshold_high is None:
            raise ConditionRuleError('Both threshold_low and threshold_high are required for range CBM rules')
        if float(threshold_high) <= float(threshold_low):
            raise ConditionRuleError('threshold_high must be greater than threshold_low')
    if severity not in ('Info', 'Warning', 'Critical'):
        raise ConditionRuleError('CBM severity must be Info, Warning or Critical')
    if action_type not in ('Recommendation', 'WorkOrder'):
        raise ConditionRuleError('CBM action_type must be Recommendation or WorkOrder')
    if work_priority not in ('Low', 'Medium', 'High', 'Critical', 'Emergency'):
        raise ConditionRuleError('Invalid CBM work priority')
