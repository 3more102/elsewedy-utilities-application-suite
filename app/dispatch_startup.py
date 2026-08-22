from __future__ import annotations

from contextlib import asynccontextmanager

from . import application as _application
from .database import db
from .dispatch_store import ensure_dispatch_assignment_lock


def install_dispatch_assignment_startup() -> None:
    """Ensure the assignment coordinator exists before serving requests.

    The wrapper is installed while ``app.main`` is composing the compatibility
    surface, so the later security lifespan naturally wraps this initialization
    together with the original application lifespan.
    """
    app = _application.app
    marker = '_euas_dispatch_assignment_startup'
    if getattr(app.state, marker, False):
        return

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def dispatch_assignment_lifespan(app_instance):
        with db() as conn:
            ensure_dispatch_assignment_lock(conn)
        async with original_lifespan(app_instance):
            yield

    app.router.lifespan_context = dispatch_assignment_lifespan
    setattr(app.state, marker, True)
