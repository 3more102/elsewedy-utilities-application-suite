from __future__ import annotations

from contextlib import asynccontextmanager

from . import application as _application
from .config import DB_BACKEND
from .database import db
from .pm_store import ensure_pm_generation_lock, install_pm_generator


PM_BOOTSTRAP_LOCK_KEY = 1_169_982_297


def initialize_pm_generation_support(conn) -> None:
    """Create the coordinator safely during simultaneous PostgreSQL startup."""
    if DB_BACKEND == 'postgresql':
        conn.execute(
            'SELECT pg_advisory_xact_lock(?)',
            (PM_BOOTSTRAP_LOCK_KEY,),
        )
    ensure_pm_generation_lock(conn)


def install_pm_generation_startup() -> None:
    """Initialize and install the PM generator before automation can execute."""
    app = _application.app
    marker = '_euas_pm_generation_startup'
    if getattr(app.state, marker, False):
        return

    # Replace the shared application global immediately. Both the scheduler and
    # manual generation endpoint resolve this name at call time.
    install_pm_generator()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def pm_generation_lifespan(app_instance):
        # This coordinator has no foreign keys, so it can be created before the
        # historical lifespan starts its automation task. PostgreSQL first-start
        # DDL is serialized across replicas; SQLite serializes schema writers.
        with db() as conn:
            initialize_pm_generation_support(conn)
        async with original_lifespan(app_instance):
            yield

    app.router.lifespan_context = pm_generation_lifespan
    setattr(app.state, marker, True)
