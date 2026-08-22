from __future__ import annotations

from contextlib import asynccontextmanager

from . import application as _application
from .config import DB_BACKEND
from .database import db
from .work_order_number_store import (
    ensure_work_order_number_lock,
    install_work_order_number_allocator,
)


WORK_ORDER_NUMBER_BOOTSTRAP_LOCK_KEY = 1_169_982_301


def initialize_work_order_number_support(conn) -> None:
    """Create the WO-number coordinator safely across simultaneous replicas."""
    if DB_BACKEND == 'postgresql':
        conn.execute(
            'SELECT pg_advisory_xact_lock(?)',
            (WORK_ORDER_NUMBER_BOOTSTRAP_LOCK_KEY,),
        )
    ensure_work_order_number_lock(conn)


def install_work_order_number_startup() -> None:
    """Install the allocator immediately and initialize its lock before serving."""
    app = _application.app
    marker = '_euas_work_order_number_startup'
    if getattr(app.state, marker, False):
        return

    # Existing application functions resolve ``next_no`` from the application
    # module at call time. Replacing that one global protects manual, PM, alarm,
    # inspection, and other future work-order creators without rewriting them.
    install_work_order_number_allocator()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def work_order_number_lifespan(app_instance):
        with db() as conn:
            initialize_work_order_number_support(conn)
        async with original_lifespan(app_instance):
            yield

    app.router.lifespan_context = work_order_number_lifespan
    setattr(app.state, marker, True)
