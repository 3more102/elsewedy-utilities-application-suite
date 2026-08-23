"""Backward-compatible identity and authorization imports during modular migration."""

from apps.identity import PBKDF2_ROUNDS, current_user, hash_password, verify_password
from apps.authorization import (
    _permission_allowed,
    effective_permissions,
    has_permission,
    require_permission,
    require_roles,
)

__all__ = [
    'PBKDF2_ROUNDS',
    'hash_password',
    'verify_password',
    'current_user',
    'require_roles',
    'require_permission',
    'effective_permissions',
    'has_permission',
    '_permission_allowed',
]
