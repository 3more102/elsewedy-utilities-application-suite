from apps.alarms import ACTIVE_ALARM_STATES, TERMINAL_ALARM_STATES, telemetry_alarm_level


def test_alarm_threshold_precedence_and_lifecycle_contract():
    channel = {'critical_high': 90, 'warning_high': 70, 'critical_low': 10, 'warning_low': 20}
    assert telemetry_alarm_level(channel, 95) == ('Critical', 90.0)
    assert telemetry_alarm_level(channel, 75) == ('Warning', 70.0)
    assert telemetry_alarm_level(channel, 5) == ('Critical', 10.0)
    assert telemetry_alarm_level(channel, 50) == (None, None)
    assert ACTIVE_ALARM_STATES == ('Open', 'Acknowledged')
    assert TERMINAL_ALARM_STATES == ('Cleared', 'Closed')
