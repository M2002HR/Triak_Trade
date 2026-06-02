"""FastAPI dashboard app factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from triak_trade.config.settings import Settings
from triak_trade.dashboard.routes import build_router
from triak_trade.dashboard.templates import STATIC_DIR, TEMPLATE_DIR


def create_dashboard_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Triak Trade Dashboard")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(build_router(settings=settings, templates=templates))
    return app
