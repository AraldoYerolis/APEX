"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apex.actions.routes import router as actions_router

logger = logging.getLogger(__name__)


def create_app(settings=None, conn=None) -> FastAPI:
    """Create and configure the FastAPI application.

    settings and conn are injected at startup from main.py.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup is handled in main.py before the server starts
        yield
        # Shutdown
        from apex.db.connection import close_db
        logger.info("APEX shutting down...")
        close_db()

    app = FastAPI(
        title="APEX",
        description="Hyperliquid Perp Scalp Alert Bot",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(actions_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "apex"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        from apex.config import get_settings
        from apex.db.connection import get_connection
        from apex.db import repository as repo

        cfg = get_settings()
        try:
            db_conn = get_connection()
            markets = repo.get_scan_enabled_markets(db_conn)
            daily = repo.get_or_create_daily_risk(db_conn, cfg.max_daily_loss_usd)
            return {
                "status": "ok",
                "scan_enabled_markets": len(markets),
                "daily_loss_used_usd": daily["planned_loss_used_usd"],
                "daily_max_loss_usd": daily["max_planned_loss_usd"],
                "daily_lockout": bool(daily["lockout_active"]),
                "pushover_configured": bool(cfg.pushover_app_token and cfg.pushover_user_key),
                "action_links_enabled": bool(cfg.apex_public_base_url),
            }
        except Exception as e:
            return {"status": "degraded", "error": str(e)}

    return app
