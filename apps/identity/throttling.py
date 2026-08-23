from __future__ import annotations

import time

_LOGIN_FAILURES: dict[str, list[float]] = {}
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5


def login_key(request, username: str) -> str:
    host = request.client.host if request.client else 'unknown'
    return f'{host}:{username.lower()}'


def login_is_blocked(key: str) -> bool:
    now_ts = time.time()
    recent = [t for t in _LOGIN_FAILURES.get(key, []) if now_ts - t < LOGIN_WINDOW_SECONDS]
    _LOGIN_FAILURES[key] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


def login_failure(key: str) -> None:
    _LOGIN_FAILURES.setdefault(key, []).append(time.time())


def login_success(key: str) -> None:
    _LOGIN_FAILURES.pop(key, None)
