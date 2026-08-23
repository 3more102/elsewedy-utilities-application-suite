from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Header, HTTPException

from core.database import db


def current_user(authorization: Optional[str] = Header(default=None)):
    """Resolve the active user for an unexpired bearer session."""
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(401, 'Authentication required')
    token = authorization.split(' ', 1)[1]
    with db() as conn:
        row = conn.execute(
            '''SELECT u.id,u.username,u.full_name,u.email,r.code role,r.name role_name,u.active
               FROM sessions s JOIN users u ON u.id=s.user_id JOIN roles r ON r.id=u.role_id
               WHERE s.token=? AND s.expires_at>?''',
            (token, datetime.now().isoformat()),
        ).fetchone()
        if not row or not row['active']:
            raise HTTPException(401, 'Invalid or expired session')
        return dict(row)
