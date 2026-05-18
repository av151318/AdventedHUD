"""Standalone aiohttp application for AdventedHUD."""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from hud.adapters import HUDAdapterHub
from hud.contracts import (
    HUD_ROUTE_BRIEF,
    HUD_ROUTE_INGEST,
    HUD_ROUTE_MCP,
    HUD_ROUTE_ONBOARDING_SOUL,
    HUD_ROUTE_PROJECT,
    HUD_ROUTE_STATUS_COMPAT,
    HUD_ROUTE_SYNC_STATUS,
)
from hud.handlers import handle_brief, handle_health, handle_sync_status
from hud.ingest_project import handle_hud_ingest, handle_hud_project
from hud.mcp import handle_mcp
from hud.onboarding import handle_onboarding_soul_read, handle_onboarding_soul_write
from hud.store import HUDStore
from hud.workers import HUDWorkers


def create_app() -> web.Application:
    db_path = os.environ.get("HUD_DB_PATH", "data/hud.db")
    app = web.Application()
    store = HUDStore(str(Path(db_path).expanduser()))
    workers = HUDWorkers()
    hub = HUDAdapterHub()
    app["hud_store"] = store
    app["hud_workers"] = workers
    app["hud_adapter_hub"] = hub

    app.router.add_get("/health", handle_health)
    app.router.add_post(HUD_ROUTE_SYNC_STATUS, handle_sync_status)
    app.router.add_post(HUD_ROUTE_STATUS_COMPAT, handle_sync_status)
    app.router.add_post(HUD_ROUTE_BRIEF, handle_brief)
    app.router.add_post(HUD_ROUTE_INGEST, handle_hud_ingest)
    app.router.add_post(HUD_ROUTE_PROJECT, handle_hud_project)
    app.router.add_post(HUD_ROUTE_MCP, handle_mcp)
    app.router.add_get(HUD_ROUTE_ONBOARDING_SOUL, handle_onboarding_soul_read)
    app.router.add_post(HUD_ROUTE_ONBOARDING_SOUL, handle_onboarding_soul_write)
    return app
