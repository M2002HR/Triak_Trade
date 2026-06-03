"""Dashboard routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
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

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Response:
        return templates.TemplateResponse(
            request,
            "login.html",
            context(request, {"next_path": next, "login_error": None}),
        )

    @router.post("/login")
    async def login_submit(request: Request) -> Response:
        form = {key: str(value) for key, value in (await request.form()).items()}
        token = form.get("token", "")
        next_path = form.get("next_path", "/")
        if not auth.validate_login_token(token):
            return templates.TemplateResponse(
                request,
                "login.html",
                context(
                    request,
                    {
                        "next_path": next_path,
                        "login_error": "Invalid admin token.",
                    },
                ),
                status_code=401,
            )
        response = RedirectResponse(url=next_path or "/", status_code=303)
        auth.set_session_cookie(response)
        return response

    @router.get("/logout")
    async def logout(request: Request) -> Response:
        response = RedirectResponse(url="/login", status_code=303)
        auth.clear_session_cookie(response)
        return response

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(request, {"overview": service.overview()}),
        )

    @router.get("/backtests", response_class=HTMLResponse)
    async def backtests(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "backtests.html",
            context(
                request,
                {
                    "result": None,
                    "default_channel": settings.REAL_BACKTEST_DEFAULT_CHANNEL,
                    "readiness": service.real_backtest_readiness(),
                    "bootstrap": service.backtest_bootstrap(),
                },
            ),
        )

    @router.post("/backtests/run", response_class=HTMLResponse)
    async def run_backtest(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        form = {key: str(value) for key, value in (await request.form()).items()}
        result = service.run_fixture_backtest_from_form(form)
        return templates.TemplateResponse(
            request,
            "backtests.html",
            context(
                request,
                {
                    "result": result,
                    "default_channel": settings.REAL_BACKTEST_DEFAULT_CHANNEL,
                    "readiness": service.real_backtest_readiness(),
                    "bootstrap": service.backtest_bootstrap(),
                },
            ),
        )

    @router.get("/api/backtests/readiness")
    async def backtest_readiness(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse(service.real_backtest_readiness())

    @router.get("/api/backtests/runs")
    async def list_backtest_runs(request: Request, limit: int = 20) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse({"runs": service.list_backtest_runs(limit=limit)})

    @router.get("/api/backtests/runs/{run_id}")
    async def get_backtest_run(request: Request, run_id: str) -> JSONResponse:
        auth.require_api(request)
        run = service.get_backtest_run(run_id)
        if run is None:
            return JSONResponse({"detail": "run_not_found"}, status_code=404)
        return JSONResponse(run)

    @router.post("/api/backtests/start")
    async def start_backtest_run(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            result = service.start_live_backtest(payload)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        status_code = 202 if result.get("started") else 409
        return JSONResponse(result, status_code=status_code)

    @router.get("/approvals", response_class=HTMLResponse)
    async def approvals(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
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
    ) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        _ = (action_id, decision)
        return RedirectResponse(url="/approvals?decision_recorded=placeholder", status_code=303)

    @router.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(request, "logs.html", context(request, service.logs()))

    @router.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "reports.html",
            context(request, service.reports()),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(request, {"settings_view": service.safe_settings()}),
        )

    @router.post("/settings/auto-mode")
    async def toggle_auto_mode(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        form = {key: str(value) for key, value in (await request.form()).items()}
        enabled = form.get("enabled") == "on"
        service.state.set_auto_mode(
            enabled=enabled,
            updated_by="dashboard",
            reason=form.get("reason", ""),
        )
        return RedirectResponse(url="/settings", status_code=303)

    @router.post("/settings/kill-switch")
    async def toggle_kill_switch(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
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
        auth.require_api(request)
        return JSONResponse(service.safe_settings() | {"overview": service.overview()["cards"]})

    return router
