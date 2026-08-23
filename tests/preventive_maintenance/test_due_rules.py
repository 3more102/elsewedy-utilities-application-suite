from datetime import date

from apps.preventive_maintenance import is_plan_due, next_calendar_due


def test_calendar_plan_due_and_next_interval_are_deterministic():
    target = date(2026, 8, 23)
    assert is_plan_due({'trigger_type': 'Calendar', 'next_due': '2026-08-23'}, target)
    assert not is_plan_due({'trigger_type': 'Calendar', 'next_due': '2026-08-24'}, target)
    assert next_calendar_due('2026-08-16', 7, target) == '2026-08-30'


def test_meter_and_condition_plan_due_rules_match_existing_contract():
    target = date(2026, 8, 23)
    assert is_plan_due({'trigger_type': 'Meter', 'meter_interval': 100, 'meter_reading': 310, 'last_meter': 200}, target)
    assert not is_plan_due({'trigger_type': 'Meter', 'meter_interval': 100, 'meter_reading': 299, 'last_meter': 200}, target)
    assert is_plan_due({'trigger_type': 'Condition', 'condition': 'Critical'}, target)
    assert not is_plan_due({'trigger_type': 'Condition', 'condition': 'Good'}, target)
