from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import application as _application

# Re-export the existing monolithic application surface, including private
# helpers imported by operational scripts. This keeps app.main as the stable
# compatibility entrypoint while security-sensitive routes are composed below.
for _name, _value in vars(_application).items():
    if not _name.startswith('__'):
        globals()[_name] = _value

from .audit_store import append_audit, ensure_audit_chain_lock
from .auth import (
    current_user,
    hash_password,
    require_permissions,
    require_roles,
    verify_password,
    verify_password_with_upgrade,
)
from .authorization import (
    authorization_snapshot,
    ensure_permission_catalog,
    permission_codes_for_role,
    replace_role_permissions,
)
from .auth_store import (
    CLIENT_LOGIN_MAX_FAILURES,
    active_session_count,
    clear_login_failures,
    client_login_scope_digest,
    create_session,
    initialize_auth_database,
    list_sessions as list_auth_sessions,
    login_scope_digest,
    record_login_failure,
    revoke_all_sessions,
    revoke_other_sessions,
    revoke_session,
    throttle_status,
)
from .config import SESSION_HOURS
from .database import db
from .inventory_store import (
    InventoryConcurrencyConflict,
    adjust_stock_if_unchanged,
    increment_stock,
    issue_unreserved_stock,
)

# All original application handlers resolve ``audit`` from the application
# module at call time. Replacing that one module global serializes the existing
# tamper-evident chain without rewriting every business endpoint.
audit = append_audit
_application.audit = append_audit

app = _application.app
_legacy_metrics = _application.metrics

# Use a valid current-format PBKDF2 hash for nonexistent/disabled principals so
# invalid-login timing does not trivially disclose account existence. This is a
# fixed non-secret verifier value, never a real credential.
_DUMMY_PASSWORD_HASH = (
    'pbkdf2_sha256$600000$euas-auth-dummy-salt$'
    'e1f6335ca2062b517a9280425b46e2ae2f68356c2163fb34ad92b13370b65bf6'
)


class RolePermissionsIn(BaseModel):
    permissions: list[str] = Field(default_factory=list)


# Initialize the base schema under its historical v9 marker, then run the v10
# auth migration which writes marker 10 only after its DDL/data migration has
# succeeded. The established application lifespan is then entered to preserve
# its backfill and scheduler lifecycle; its repeated base init is idempotent and
# cannot pre-claim v10 because the secure migration has already completed.
_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _security_lifespan(app_instance):
    initialize_auth_database(hash_password)
    with db() as conn:
        ensure_audit_chain_lock(conn)
    async with _original_lifespan(app_instance):
        with db() as conn:
            ensure_permission_catalog(conn)
        yield


app.router.lifespan_context = _security_lifespan


def _remove_route(path: str, methods: set[str]) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and bool(methods.intersection(set(getattr(route, 'methods', set()) or set())))
        )
    ]


# Replace only routes whose semantics depend on hardened security/session or
# transaction behavior. All other application routes remain the original
# registered handlers and receive authorization overlays through the shared
# require_roles() dependency.
for _path, _methods in (
    ('/api/auth/login', {'POST'}),
    ('/api/auth/logout', {'POST'}),
    ('/api/auth/change-password', {'POST'}),
    ('/api/auth/sessions', {'GET'}),
    ('/api/auth/sessions/revoke-others', {'POST'}),
    ('/api/admin/users/{user_id}/status', {'PATCH'}),
    ('/api/inventory/{item_id}/transaction', {'POST'}),
    ('/api/metrics', {'GET'}),
):
    _remove_route(_path, _methods)

app.openapi_schema = None


def _client_host(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


@app.post('/api/auth/login')
def login(body: _application.LoginIn, request: Request):
    username = body.username.strip()
    host = _client_host(request)
    scope = login_scope_digest(username, host)
    client_scope = client_login_scope_digest(host)

    with db() as conn:
        account_throttle = throttle_status(conn, scope)
        client_throttle = throttle_status(conn, client_scope)
    if account_throttle['blocked'] or client_throttle['blocked']:
        retry_after = max(
            int(account_throttle['retry_after']),
            int(client_throttle['retry_after']),
        )
        raise HTTPException(
            429,
            'Too many failed login attempts. Try again later.',
            headers={'Retry-After': str(max(1, retry_after))},
        )

    failed = False
    response = None
    with db() as conn:
        row = conn.execute(
            '''SELECT u.*,r.code role,r.name role_name
               FROM users u JOIN roles r ON r.id=u.role_id
               WHERE u.username=?''',
            (username,),
        ).fetchone()

        valid = False
        replacement_hash = None
        if row and row['active']:
            valid, replacement_hash = verify_password_with_upgrade(
                body.password, row['password_hash']
            )
        else:
            # Always execute one current-work-factor password verification for a
            # missing/disabled principal to reduce account-enumeration timing.
            verify_password(body.password, _DUMMY_PASSWORD_HASH)

        if not valid:
            state = record_login_failure(conn, scope)
            record_login_failure(
                conn,
                client_scope,
                max_failures=CLIENT_LOGIN_MAX_FAILURES,
            )
            # Audit failures for known accounts without logging the password,
            # bearer material, client IP, or throttle scope digest.
            if row:
                action = 'LOGIN_THROTTLED' if state['blocked_until'] else 'LOGIN_FAILED'
                audit(
                    conn,
                    row['id'],
                    action,
                    'Authentication',
                    row['username'],
                    '',
                    'Invalid credentials',
                )
            failed = True
        else:
            # A successful authentication clears the account/client pair only;
            # it deliberately does not erase the wider client abuse history.
            clear_login_failures(conn, scope)
            if replacement_hash:
                conn.execute(
                    'UPDATE users SET password_hash=? WHERE id=?',
                    (replacement_hash, row['id']),
                )
                audit(
                    conn,
                    row['id'],
                    'PASSWORD_HASH_UPGRADE',
                    'Authentication',
                    row['username'],
                    '',
                    'Credential hash upgraded after successful authentication',
                )

            session = create_session(
                conn,
                row['id'],
                SESSION_HOURS,
                request.headers.get('user-agent', ''),
            )
            audit(
                conn,
                row['id'],
                'LOGIN',
                'Authentication',
                row['username'],
                '',
                {'session_id': session['session_id']},
            )
            response = {
                'token': session['token'],
                'user': {
                    'id': row['id'],
                    'username': row['username'],
                    'full_name': row['full_name'],
                    'email': row['email'],
                    'department': row['department'],
                    'phone': row['phone'],
                    'role': row['role'],
                    'role_name': row['role_name'],
                },
            }

    # Raise only after leaving the DB context so the persistent failure counter
    # and audit event commit instead of being rolled back with HTTPException.
    if failed:
        raise HTTPException(401, 'Invalid username or password')
    return response


@app.post('/api/auth/logout')
def logout(user=Depends(current_user)):
    with db() as conn:
        revoked = revoke_session(conn, user['id'], user['session_id'])
        audit(
            conn,
            user['id'],
            'LOGOUT',
            'Authentication',
            user['username'],
            '',
            {'session_id': user['session_id'], 'revoked': bool(revoked)},
        )
    return {'ok': True}


@app.post('/api/auth/change-password')
def change_password(body: _application.PasswordChange, user=Depends(current_user)):
    if (
        not any(c.isupper() for c in body.new_password)
        or not any(c.islower() for c in body.new_password)
        or not any(c.isdigit() for c in body.new_password)
        or body.new_password.isalnum()
    ):
        raise HTTPException(400, 'Password must include upper, lower, number and special character')

    with db() as conn:
        stored = get_or_404(
            conn,
            'SELECT password_hash FROM users WHERE id=?',
            (user['id'],),
            'User not found',
        )
        if not verify_password(body.current_password, stored['password_hash']):
            raise HTTPException(400, 'Current password is incorrect')
        if verify_password(body.new_password, stored['password_hash']):
            raise HTTPException(400, 'New password must be different')

        conn.execute(
            'UPDATE users SET password_hash=? WHERE id=?',
            (hash_password(body.new_password), user['id']),
        )
        revoked = revoke_other_sessions(conn, user['id'], user['session_id'])
        audit(
            conn,
            user['id'],
            'PASSWORD_CHANGE',
            'Authentication',
            user['username'],
            '',
            {
                'current_session_id': user['session_id'],
                'other_sessions_revoked': revoked,
            },
        )
    return {'ok': True, 'other_sessions_revoked': True, 'revoked': revoked}


@app.get('/api/auth/sessions')
def list_sessions(user=Depends(current_user)):
    with db() as conn:
        data = list_auth_sessions(conn, user['id'])
    return [
        {
            **entry,
            'current': int(entry['session_id']) == int(user['session_id']),
        }
        for entry in data
    ]


@app.get('/api/auth/permissions')
def my_permissions(user=Depends(current_user)):
    return {
        'role': user['role'],
        'role_name': user['role_name'],
        'permissions': list(user.get('permissions', [])),
    }


@app.post('/api/auth/sessions/{session_id}/revoke')
def revoke_one_session(session_id: int, user=Depends(current_user)):
    with db() as conn:
        revoked = revoke_session(conn, user['id'], session_id)
        if not revoked:
            raise HTTPException(404, 'Session not found')
        audit(
            conn,
            user['id'],
            'SESSION_REVOKE',
            'Authentication',
            user['username'],
            '',
            {'session_id': session_id},
        )
    return {
        'ok': True,
        'revoked': 1,
        'current_session_revoked': int(session_id) == int(user['session_id']),
    }


@app.post('/api/auth/sessions/revoke-others')
def revoke_other_session_route(user=Depends(current_user)):
    with db() as conn:
        revoked = revoke_other_sessions(conn, user['id'], user['session_id'])
        audit(
            conn,
            user['id'],
            'SESSION_REVOKE_OTHERS',
            'Authentication',
            user['username'],
            '',
            {'current_session_id': user['session_id'], 'revoked': revoked},
        )
    return {'ok': True, 'revoked': revoked}


@app.post('/api/auth/sessions/revoke-all')
def revoke_all_session_route(user=Depends(current_user)):
    with db() as conn:
        revoked = revoke_all_sessions(conn, user['id'])
        audit(
            conn,
            user['id'],
            'SESSION_REVOKE_ALL',
            'Authentication',
            user['username'],
            '',
            {'revoked': revoked},
        )
    return {'ok': True, 'revoked': revoked, 'current_session_revoked': True}


@app.get('/api/admin/permissions')
def admin_permissions(user=Depends(require_permissions('admin.permissions.manage'))):
    with db() as conn:
        return authorization_snapshot(conn)


@app.put('/api/admin/roles/{role_code}/permissions')
def update_role_permissions(
    role_code: str,
    body: RolePermissionsIn,
    user=Depends(require_permissions('admin.permissions.manage')),
):
    with db() as conn:
        old = permission_codes_for_role(conn, role_code)
        try:
            updated = replace_role_permissions(conn, role_code, body.permissions)
        except KeyError:
            raise HTTPException(404, 'Role not found')
        except ValueError as exc:
            message = str(exc)
            if message == 'protected_admin_permission':
                raise HTTPException(
                    409,
                    'The admin role must retain admin.permissions.manage to prevent authorization lockout',
                )
            if message.startswith('unknown_permissions:'):
                unknown = message.split(':', 1)[1]
                raise HTTPException(400, f'Unknown permissions: {unknown}')
            raise
        audit(
            conn,
            user['id'],
            'ROLE_PERMISSIONS_UPDATE',
            'Administration',
            role_code,
            old,
            updated,
        )
    return {'role': role_code, 'permissions': updated}


@app.patch('/api/admin/users/{user_id}/status')
def set_user_status(
    user_id: int,
    body: _application.UserStatusIn,
    user=Depends(require_roles('admin')),
):
    if user_id == user['id'] and not body.active:
        raise HTTPException(400, 'You cannot deactivate your own account')

    with db() as conn:
        target = get_or_404(
            conn,
            '''SELECT u.*,r.code role
               FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?''',
            (user_id,),
            'User not found',
        )
        if target['role'] == 'admin' and not body.active:
            admins = conn.execute(
                '''SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE r.code='admin' AND u.active=1'''
            ).fetchone()[0]
            if admins <= 1:
                raise HTTPException(409, 'At least one active administrator is required')

        conn.execute(
            'UPDATE users SET active=? WHERE id=?',
            (1 if body.active else 0, user_id),
        )
        revoked = 0
        if not body.active:
            revoked = revoke_all_sessions(conn, user_id)
        audit(
            conn,
            user['id'],
            'ACTIVATE' if body.active else 'DEACTIVATE',
            'Administration',
            target['username'],
            target['active'],
            {'active': 1 if body.active else 0, 'sessions_revoked': revoked},
        )
    return {'ok': True, 'active': body.active, 'sessions_revoked': revoked}


@app.post('/api/inventory/{item_id}/transaction')
def inventory_tx(
    item_id: int,
    body: _application.InventoryTxIn,
    user=Depends(require_roles(*INV_ROLES)),
):
    """Apply stock mutations without read-modify-write lost updates."""
    with db() as conn:
        item = get_or_404(
            conn,
            'SELECT * FROM inventory_items WHERE id=?',
            (item_id,),
            'Item not found',
        )
        tx = body.tx_type.upper()
        quantity = float(body.quantity)

        try:
            if tx == 'ISSUE':
                amount = abs(quantity)
                old_stock, new_stock = issue_unreserved_stock(conn, item_id, amount)
                quantity = -amount
            elif tx in ('RETURN', 'RECEIPT'):
                quantity = abs(quantity)
                old_stock, new_stock = increment_stock(conn, item_id, quantity)
            elif tx == 'ADJUSTMENT':
                target_stock = quantity
                old_stock, new_stock = adjust_stock_if_unchanged(
                    conn,
                    item_id,
                    float(item['current_stock']),
                    target_stock,
                )
                quantity = new_stock - old_stock
            elif tx == 'TRANSFER':
                if not body.to_warehouse_id:
                    raise HTTPException(400, 'Destination warehouse required')
                if body.to_warehouse_id == item['warehouse_id']:
                    raise HTTPException(400, 'Destination warehouse must be different')

                amount = abs(quantity)
                old_stock, new_stock = issue_unreserved_stock(conn, item_id, amount)
                quantity = -amount

                destination = conn.execute(
                    '''SELECT * FROM inventory_items
                       WHERE warehouse_id=? AND name=? AND category=?''',
                    (body.to_warehouse_id, item['name'], item['category']),
                ).fetchone()
                if destination:
                    increment_stock(conn, destination['id'], amount)
                    destination_id = destination['id']
                else:
                    destination_no = next_no(
                        conn, 'inventory_items', 'item_no', 'ITM-', 1000
                    )
                    created = conn.execute(
                        '''INSERT INTO inventory_items(
                             item_no,name,description,category,warehouse_id,
                             current_stock,reserved_stock,min_level,max_level,
                             reorder_point,unit_price,unit,vendor_id,bin
                           ) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)''',
                        (
                            destination_no,
                            item['name'],
                            item['description'],
                            item['category'],
                            body.to_warehouse_id,
                            amount,
                            item['min_level'],
                            item['max_level'],
                            item['reorder_point'],
                            item['unit_price'],
                            item['unit'],
                            item['vendor_id'],
                            item['bin'],
                        ),
                    )
                    destination_id = created.lastrowid

                conn.execute(
                    '''INSERT INTO inventory_transactions(
                         item_id,tx_type,quantity,from_warehouse_id,
                         to_warehouse_id,reference,user_id,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)''',
                    (
                        destination_id,
                        'TRANSFER',
                        amount,
                        item['warehouse_id'],
                        body.to_warehouse_id,
                        body.reference or item['item_no'],
                        user['id'],
                        now(),
                    ),
                )
            else:
                raise HTTPException(400, 'Invalid transaction type')
        except KeyError:
            raise HTTPException(404, 'Item not found')
        except InventoryConcurrencyConflict as exc:
            if str(exc) == 'insufficient_unreserved_stock':
                message = (
                    'Insufficient unreserved stock; reserved material cannot be '
                    'issued or transferred'
                )
            else:
                message = 'Inventory stock changed concurrently; retry the adjustment'
            raise HTTPException(409, message)

        current = conn.execute(
            '''SELECT current_stock,reserved_stock,reorder_point
               FROM inventory_items WHERE id=?''',
            (item_id,),
        ).fetchone()
        new_stock = float(current['current_stock'])

        conn.execute(
            '''INSERT INTO inventory_transactions(
                 item_id,tx_type,quantity,from_warehouse_id,to_warehouse_id,
                 work_order_id,reference,user_id,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                item_id,
                tx,
                quantity,
                item['warehouse_id'],
                body.to_warehouse_id,
                body.work_order_id,
                body.reference,
                user['id'],
                now(),
            ),
        )
        audit(
            conn,
            user['id'],
            tx,
            'Inventory',
            item['item_no'],
            old_stock,
            new_stock,
        )
        if new_stock - float(current['reserved_stock']) <= float(current['reorder_point']):
            notify(
                conn,
                'Inventory below reorder point',
                f"{item['item_no']} — {item['name']} is below reorder point",
                'Warning',
                None,
                'storekeeper',
                'inventory',
                item['item_no'],
            )
        return {'ok': True, 'current_stock': new_stock}


@app.get('/api/metrics', response_class=PlainTextResponse)
def metrics(user=Depends(require_roles('admin', 'maintenance_manager', 'executive'))):
    text = _legacy_metrics(user)
    with db() as conn:
        count = active_session_count(conn)
    lines = []
    replaced = False
    for line in str(text).splitlines():
        if line.startswith('euas_active_sessions '):
            lines.append(f'euas_active_sessions {count}')
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f'euas_active_sessions {count}')
    return '\n'.join(lines) + '\n'


# Preserve the historical ``app.main`` module identity for callers that mutate
# module-level deployment settings in tests or embedding code. The application
# module owns the original functions, so aliasing the public module name back to
# it ensures monkeypatches such as EVENT_WEBHOOK_URL continue to affect those
# functions while the FastAPI router retains the hardened handlers registered
# above.
for _export in (
    'audit',
    'login',
    'logout',
    'change_password',
    'list_sessions',
    'my_permissions',
    'revoke_one_session',
    'revoke_other_session_route',
    'revoke_all_session_route',
    'admin_permissions',
    'update_role_permissions',
    'set_user_status',
    'inventory_tx',
    'metrics',
):
    setattr(_application, _export, globals()[_export])
_application.app = app
sys.modules[__name__] = _application
