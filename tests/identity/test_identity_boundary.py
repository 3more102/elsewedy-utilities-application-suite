from types import SimpleNamespace

from app.auth import hash_password as legacy_hash_password
from apps.identity import (
    hash_password,
    login_failure,
    login_is_blocked,
    login_key,
    login_success,
    verify_password,
)


def test_identity_password_compatibility_and_login_throttle():
    stored = hash_password('Identity@Test123', salt='0123456789abcdef0123456789abcdef')
    assert verify_password('Identity@Test123', stored)
    assert not verify_password('wrong', stored)
    assert legacy_hash_password('Identity@Test123', salt='0123456789abcdef0123456789abcdef') == stored

    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.77'))
    key = login_key(request, 'OMAR')
    login_success(key)
    assert not login_is_blocked(key)
    for _ in range(5):
        login_failure(key)
    assert login_is_blocked(key)
    login_success(key)
    assert not login_is_blocked(key)
