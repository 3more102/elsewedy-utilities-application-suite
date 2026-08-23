from __future__ import annotations

import math
import re
from datetime import datetime

_CHANNEL_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{1,79}$")


class TelemetryValidationError(ValueError):
    pass


class TelemetryChannelNotFound(LookupError):
    pass


def normalize_channel_code(value: str) -> str:
    code = str(value or '').strip().upper()
    if not _CHANNEL_CODE.fullmatch(code):
        raise TelemetryValidationError(
            'Telemetry channel code must be 2-80 characters using letters, numbers, dot, underscore, colon or hyphen'
        )
    return code


def normalize_quality(value: str) -> str:
    quality = str(value or 'Good').strip().title()
    if quality not in ('Good', 'Uncertain', 'Bad'):
        raise TelemetryValidationError('Telemetry quality must be Good, Uncertain or Bad')
    return quality


def normalize_timestamp(value: str | None, *, fallback: str) -> str:
    if not value:
        return fallback
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise TelemetryValidationError('Telemetry captured_at must be a valid ISO-8601 timestamp') from exc
    return parsed.isoformat(timespec='seconds')


def normalize_measurement(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise TelemetryValidationError('Telemetry values must be finite numbers')
    return result


def normalize_source(value: str | None, *, fallback: str) -> str:
    source = str(value or fallback or 'Manual').strip()
    if not source:
        source = 'Manual'
    if len(source) > 160:
        raise TelemetryValidationError('Telemetry source must be 160 characters or fewer')
    return source
