"""FastAPI dashboard app factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from triak_trade.config.settings import Settings
from triak_trade.dashboard.live_runtime import DashboardLiveCoordinator
from triak_trade.dashboard.realtime import DashboardRealtimeHub
from triak_trade.dashboard.routes import build_router
from triak_trade.dashboard.services import DashboardService
from triak_trade.dashboard.templates import STATIC_DIR, TEMPLATE_DIR
from triak_trade.db.base import Base
from triak_trade.db.engine import build_engine_from_settings, create_session_factory


def create_dashboard_app(settings: Settings) -> FastAPI:
    realtime_hub = DashboardRealtimeHub()
    db_engine = build_engine_from_settings(settings)
    Base.metadata.create_all(db_engine)
    db_session_factory = create_session_factory(db_engine)
    service = DashboardService(
        settings,
        realtime_notifier=realtime_hub.broadcast_threadsafe,
    )
    live_coordinator = DashboardLiveCoordinator(
        settings=settings,
        session_factory=db_session_factory,
        notifier=realtime_hub.broadcast_threadsafe,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        realtime_hub.bind_loop(asyncio.get_running_loop())
        try:
            yield
        finally:
            await live_coordinator.aclose()

    app = FastAPI(title="Triak Trade Dashboard", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.state.dashboard_service = service
    app.state.dashboard_realtime_hub = realtime_hub
    app.state.live_coordinator = live_coordinator
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(
        build_router(
            settings=settings,
            templates=templates,
            service=service,
            realtime_hub=realtime_hub,
            live_coordinator=live_coordinator,
        )
    )
    return app
