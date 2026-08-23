from datetime import date

from apps.workforce import forecast_bucket_start, parse_days_of_week


def test_weekday_parser_rejects_noise_and_preserves_valid_days():
    assert parse_days_of_week('0,2,6,9,bad') == {0, 2, 6}


def test_weekday_parser_defaults_to_business_week():
    assert parse_days_of_week('bad,9') == {0, 1, 2, 3, 4}
    assert parse_days_of_week(None) == {0, 1, 2, 3, 4}


def test_forecast_bucket_starts_on_monday():
    assert forecast_bucket_start(date(2026, 8, 23)) == date(2026, 8, 17)
    assert forecast_bucket_start(date(2026, 8, 17)) == date(2026, 8, 17)
