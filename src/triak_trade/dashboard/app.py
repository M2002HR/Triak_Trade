"""FastAPI dashboard app factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from triak_trade.config.settings import Settings
from triak_trade.dashboard.realtime import DashboardRealtimeHub
from triak_trade.dashboard.routes import build_router
from triak_trade.dashboard.services import DashboardService
from triak_trade.dashboard.templates import STATIC_DIR, TEMPLATE_DIR


def create_dashboard_app(settings: Settings) -> FastAPI:
    realtime_hub = DashboardRealtimeHub()
    service = DashboardService(
        settings,
        realtime_notifier=realtime_hub.broadcast_threadsafe,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        realtime_hub.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="Triak Trade Dashboard", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.state.dashboard_service = service
    app.state.dashboard_realtime_hub = realtime_hub
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(
        build_router(
            settings=settings,
            templates=templates,
            service=service,
            realtime_hub=realtime_hub,
        )
    )
    return app
