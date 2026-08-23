from app.auth import require_permission as legacy_require_permission
from apps.authorization import require_permission, require_roles


def test_authorization_compatibility_exports_are_stable():
    assert legacy_require_permission is require_permission
    assert callable(require_roles('admin'))
    assert callable(require_permission('automation.run', 'admin'))
