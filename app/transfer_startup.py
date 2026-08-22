from __future__ import annotations

from contextlib import asynccontextmanager

from . import application as _application
from .database import db
from .transfer_store import ensure_transfer_support, install_inventory_transfer_routes


def install_inventory_transfer_startup() -> None:
    """Install transfer support only after the historical base schema exists."""
    app = _application.app
    marker = '_euas_inventory_transfer_startup'
    if getattr(app.state, marker, False):
        return

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def inventory_transfer_lifespan(app_instance):
        async with original_lifespan(app_instance):
            with db() as conn:
                ensure_transfer_support(conn)
            # app.main has completed all of its route replacement by the time a
            # lifespan starts, so this captures/delegates to the final hardened
            # non-transfer inventory endpoint rather than the historical monolith.
            install_inventory_transfer_routes()
            yield

    app.router.lifespan_context = inventory_transfer_lifespan
    setattr(app.state, marker, True)
