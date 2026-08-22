from __future__ import annotations

import inspect

from fastapi.routing import APIRoute

from app.authorization import (
    PERMISSION_CATALOG,
    PROTECTED_ADMIN_PERMISSION,
    ROUTE_PERMISSION_OVERLAY,
)
from app.main import app


def _api_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _route(method: str, path: str) -> APIRoute:
    matches = [
        route
        for route in _api_routes()
        if route.path == path and method.upper() in (route.methods or set())
    ]
    assert len(matches) == 1, (
        f'authorization contract expects exactly one {method} {path} route; '
        f'found {len(matches)}'
    )
    return matches[0]


def _dependency_nonlocals(route: APIRoute) -> list[dict]:
    result = []
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if not callable(call):
            continue
        try:
            result.append(inspect.getclosurevars(call).nonlocals)
        except (TypeError, ValueError):
            continue
    return result


def _declared_roles(route: APIRoute) -> tuple[str, ...]:
    role_sets = [
        tuple(values['roles'])
        for values in _dependency_nonlocals(route)
        if 'roles' in values
    ]
    assert len(role_sets) == 1, (
        f'{sorted(route.methods or set())} {route.path} must expose exactly one '
        f'legacy require_roles dependency; found {role_sets}'
    )
    return role_sets[0]


def _declared_permissions(route: APIRoute) -> tuple[str, ...]:
    permission_sets = [
        tuple(values['required'])
        for values in _dependency_nonlocals(route)
        if 'required' in values
    ]
    assert len(permission_sets) == 1, (
        f'{sorted(route.methods or set())} {route.path} must expose exactly one '
        f'require_permissions dependency; found {permission_sets}'
    )
    return permission_sets[0]


def test_every_overlay_targets_a_real_route_and_known_permission():
    assert ROUTE_PERMISSION_OVERLAY, 'permission overlay must not be empty'
    for (method, path), permission in ROUTE_PERMISSION_OVERLAY.items():
        _route(method, path)
        assert permission in PERMISSION_CATALOG, (
            f'{method} {path} references unknown permission {permission}'
        )


def test_overlay_defaults_exactly_match_legacy_route_roles():
    """Capabilities may narrow old access, but default grants must not change it."""
    for (method, path), permission in ROUTE_PERMISSION_OVERLAY.items():
        route = _route(method, path)
        legacy_roles = set(_declared_roles(route))
        default_roles = set(PERMISSION_CATALOG[permission][1])
        assert default_roles == legacy_roles, (
            f'{method} {path}: {permission} defaults {sorted(default_roles)} '
            f'do not match legacy route roles {sorted(legacy_roles)}'
        )


def test_permission_management_endpoints_require_protected_recovery_capability():
    for method, path in (
        ('GET', '/api/admin/permissions'),
        ('PUT', '/api/admin/roles/{role_code}/permissions'),
    ):
        route = _route(method, path)
        assert _declared_permissions(route) == (PROTECTED_ADMIN_PERMISSION,)


def test_overlay_does_not_use_permission_native_management_capability():
    """The recovery capability is reserved for its explicit management APIs."""
    assert PROTECTED_ADMIN_PERMISSION not in set(ROUTE_PERMISSION_OVERLAY.values())
