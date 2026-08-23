from __future__ import annotations

import uuid


def correlation_id(incoming: str | None = None) -> str:
    value = (incoming or '').strip()
    return value or uuid.uuid4().hex
