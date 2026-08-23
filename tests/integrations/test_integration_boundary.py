from apps.integrations import integration_key_digest


def test_integration_key_digest_is_stable_and_not_plaintext():
    raw = 'euas_test_key'
    digest = integration_key_digest(raw)

    assert len(digest) == 64
    assert digest == integration_key_digest(raw)
    assert raw not in digest
