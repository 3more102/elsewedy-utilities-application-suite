import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.production_readiness import evaluate_configuration


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_strict_production_rejects_reference_configuration():
    result = evaluate_configuration({}, require_postgres=True, strict_production=True)
    state = statuses(result)
    assert state['environment'] == 'FAIL'
    assert state['database_backend'] == 'FAIL'
    assert state['automation_scheduler'] == 'WARN'


def test_production_postgres_and_signed_webhook_are_ready():
    env = {
        'EUAS_ENV': 'production',
        'EUAS_DATABASE_URL': 'postgresql://euas:secret@db:5432/euas',
        'EUAS_EVENT_WEBHOOK_URL': 'https://example.invalid/euas/events',
        'EUAS_EVENT_WEBHOOK_SECRET': 'test-secret',
        'EUAS_AUTOMATION_INTERVAL_MINUTES': '5',
        'EUAS_SESSION_HOURS': '8',
        'EUAS_MAX_UPLOAD_MB': '25',
    }
    result = evaluate_configuration(env, require_postgres=True, strict_production=True)
    state = statuses(result)
    assert state['environment'] == 'PASS'
    assert state['database_backend'] == 'PASS'
    assert state['webhook_signing'] == 'PASS'
    assert state['automation_scheduler'] == 'PASS'
    assert not [c for c in result if c.status == 'FAIL']


def test_webhook_without_secret_is_a_hard_failure():
    result = evaluate_configuration(
        {'EUAS_EVENT_WEBHOOK_URL': 'https://example.invalid/euas/events'}
    )
    state = statuses(result)
    assert state['webhook_signing'] == 'FAIL'
