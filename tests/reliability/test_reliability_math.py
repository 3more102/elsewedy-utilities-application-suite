from datetime import datetime, timedelta
from apps.reliability import outage_overlap_hours


def test_outage_overlap_is_clipped_to_reporting_window():
    start=datetime(2026,8,23,8); end=start+timedelta(hours=10)
    assert outage_overlap_hours(start-timedelta(hours=2), start+timedelta(hours=3), start, end) == 3.0
    assert outage_overlap_hours(end+timedelta(hours=1), end+timedelta(hours=2), start, end) == 0.0
