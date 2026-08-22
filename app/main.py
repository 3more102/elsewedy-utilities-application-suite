from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import application as _application

# Re-export the existing monolithic application surface, including private
# helpers imported by operational scripts. This keeps app.main as the stable
# compatibility entrypoint while security-sensitive routes are composed below.
for _name, _value in vars(_application).items():
    if not _name.startswith('__'):
        globals()[_name] = _value

from .auth import (
    current_user,
    hash_password,
    require_roles,
    verify_password,
    verify_password_with_upgrade,
)
from .auth_store import (
    active_session_count,
    clear_login_failures,
    create_session,
    ensure_auth_schema,
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

app = _application.app


# Run the v10 authentication migration after the established application
# initializer has created/upgraded the base schema, but before the service is
# exposed to requests. Failure aborts application startup rather than serving a
# partially migrated authentication model.
_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _security_lifespan(app_instance):
    async with _original_lifespan(app_instance):
        with db() as conn:
            ensure_auth_schema(conn)
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


# Replace only routes whose semantics depend on raw session tokens, in-memory
# throttling, or session revocation. All other application routes remain the
# original registered handlers.
for _path, _methods in (
    ('/api/auth/login', {'POST'}),
    ('/api/auth/logout', {'POST'}),
    ('/api/auth/change-password', {'POST'}),
    ('/api/auth/sessions', {'GET'}),
    ('/api/auth/sessions/revoke-others', {'POST'}),
    ('/api/admin/users/{user_id}/status', {'PATCH'}),
    ('/api/metrics', {'GET'}),
):
    _remove_route(_path, _methods)

app.openapi_schema = None


def _client_host(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


@app.post('/api/auth/login')
def login(body: _application.LoginIn, request: Request):
    username = body.username.strip()
    scope = login_scope_digest(username, _client_host(request))

    with db() as conn:
        throttle = throttle_status(conn, scope)
    if throttle['blocked']:
        raise HTTPException(
            429,
            'Too many failed login attempts. Try again later.',
            headers={'Retry-After': str(throttle['retry_after'])},
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

        if not valid:
            state = record_login_failure(conn, scope)
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


@app.get('/api/metrics', response_class=PlainTextResponse)
def metrics(user=Depends(require_roles('admin', 'maintenance_manager', 'executive'))):
    text = _application.metrics(user)
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
    'login',
    'logout',
    'change_password',
    'list_sessions',
    'revoke_one_session',
    'revoke_other_session_route',
    'revoke_all_session_route',
    'set_user_status',
    'metrics',
):
    setattr(_application, _export, globals()[_export])
_application.app = app
sys.modules[__name__] = _application
