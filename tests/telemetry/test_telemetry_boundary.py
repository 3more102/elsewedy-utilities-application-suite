from datetime import datetime

import pytest

from apps.telemetry import TelemetryValidationError, normalize_channel_code, normalize_measurement, normalize_quality, normalize_timestamp


def test_telemetry_validation_contract():
    assert normalize_channel_code(' tel-abc_01 ') == 'TEL-ABC_01'
    assert normalize_quality(' uncertain ') == 'Uncertain'
    assert normalize_measurement(1.25) == 1.25
    assert datetime.fromisoformat(normalize_timestamp('2026-08-23T10:00:00', fallback='x'))


@pytest.mark.parametrize('value', ['', 'bad channel!', 'x'])
def test_invalid_channel_code_is_rejected(value):
    with pytest.raises(TelemetryValidationError):
        normalize_channel_code(value)


def test_non_finite_measurement_is_rejected():
    with pytest.raises(TelemetryValidationError):
        normalize_measurement(float('nan'))
