"""Dashboard routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from triak_trade.config.settings import Settings
from triak_trade.dashboard.auth import DashboardAuth
from triak_trade.dashboard.services import DashboardService


def build_router(
    *,
    settings: Settings,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter()
    auth = DashboardAuth(settings)
    service = DashboardService(settings)

    def context(request: Request, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request": request,
            "title": "Triak Trade Dashboard",
            "dashboard_url": f"http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}",
        }
        if extra:
            payload.update(extra)
        return payload

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(request, {"overview": service.overview()}),
        )

    @router.get("/backtests", response_class=HTMLResponse)
    async def backtests(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(
            request,
            "backtests.html",
            context(
                request,
                {"result": None, "default_channel": settings.BACKTEST_DEFAULT_CHANNEL},
            ),
        )

    @router.post("/backtests/run", response_class=HTMLResponse)
    async def run_backtest(request: Request) -> HTMLResponse:
        auth.require(request)
        form = {key: str(value) for key, value in (await request.form()).items()}
        result = service.run_fixture_backtest_from_form(form)
        return templates.TemplateResponse(
            request,
            "backtests.html",
            context(
                request,
                {"result": result, "default_channel": settings.BACKTEST_DEFAULT_CHANNEL},
            ),
        )

    @router.get("/approvals", response_class=HTMLResponse)
    async def approvals(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(
            request,
            "approvals.html",
            context(request, service.approvals()),
        )

    @router.post("/approvals/{action_id}/{decision}")
    async def approval_decision(
        request: Request,
        action_id: str,
        decision: str,
    ) -> RedirectResponse:
        auth.require(request)
        # Repository-backed pending actions will be wired in a later step.
        _ = (action_id, decision)
        return RedirectResponse(url="/approvals?decision_recorded=placeholder", status_code=303)

    @router.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(request, "logs.html", context(request, service.logs()))

    @router.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(
            request,
            "reports.html",
            context(request, service.reports()),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        auth.require(request)
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(request, {"settings_view": service.safe_settings()}),
        )

    @router.post("/settings/auto-mode")
    async def toggle_auto_mode(request: Request) -> RedirectResponse:
        auth.require(request)
        form = {key: str(value) for key, value in (await request.form()).items()}
        enabled = form.get("enabled") == "on"
        service.state.set_auto_mode(
            enabled=enabled,
            updated_by="dashboard",
            reason=form.get("reason", ""),
        )
        return RedirectResponse(url="/settings", status_code=303)

    @router.post("/settings/kill-switch")
    async def toggle_kill_switch(request: Request) -> RedirectResponse:
        auth.require(request)
        form = {key: str(value) for key, value in (await request.form()).items()}
        enabled = form.get("enabled") == "on"
        service.state.set_kill_switch(
            enabled=enabled,
            updated_by="dashboard",
            reason=form.get("reason", ""),
        )
        return RedirectResponse(url="/settings", status_code=303)

    @router.get("/status")
    async def status(request: Request) -> JSONResponse:
        auth.require(request)
        return JSONResponse(service.safe_settings() | {"overview": service.overview()["cards"]})

    return router
