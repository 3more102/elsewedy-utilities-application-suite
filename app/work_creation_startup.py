from __future__ import annotations

from contextlib import asynccontextmanager

from . import application as _application
from .config import DB_BACKEND
from .database import db
from .work_creation_store import (
    ensure_work_order_creation_lock,
    install_alarm_work_order_route,
    install_work_order_number_allocator,
)


WORK_CREATION_BOOTSTRAP_LOCK_KEY = 1_169_982_301


def initialize_work_creation_support(conn) -> None:
    if DB_BACKEND == 'postgresql':
        conn.execute(
            'SELECT pg_advisory_xact_lock(?)',
            (WORK_CREATION_BOOTSTRAP_LOCK_KEY,),
        )
    ensure_work_order_creation_lock(conn)


def install_work_creation_startup() -> None:
    """Install shared WO numbering before any scheduler or request can create work."""
    app = _application.app
    marker = '_euas_work_creation_startup'
    if getattr(app.state, marker, False):
        return

    install_work_order_number_allocator()
    install_alarm_work_order_route()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def work_creation_lifespan(app_instance):
        with db() as conn:
            initialize_work_creation_support(conn)
        async with original_lifespan(app_instance):
            yield

    app.router.lifespan_context = work_creation_lifespan
    setattr(app.state, marker, True)
