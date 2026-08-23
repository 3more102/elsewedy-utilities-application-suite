from apps.hse import is_high_risk, risk_score, validate_hse_status

def test_hse_risk_policy():
    assert risk_score(3,4) == 12
    assert is_high_risk(12)
    assert not is_high_risk(11)
    assert validate_hse_status('Action Required')
    assert not validate_hse_status('Unknown')
