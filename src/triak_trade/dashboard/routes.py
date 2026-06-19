"""Dashboard routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from triak_trade.config.settings import Settings
from triak_trade.dashboard.auth import DashboardAuth
from triak_trade.dashboard.realtime import DashboardRealtimeHub
from triak_trade.dashboard.services import DashboardService


def build_router(
    *,
    settings: Settings,
    templates: Jinja2Templates,
    service: DashboardService,
    realtime_hub: DashboardRealtimeHub,
) -> APIRouter:
    router = APIRouter()
    auth = DashboardAuth(settings)

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
        result = await asyncio.to_thread(service.run_fixture_backtest_from_form, form)
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

    @router.get("/api/backtests/channels")
    async def list_backtest_channels(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse({"channels": service.list_saved_channels()})

    @router.post("/api/backtests/channels")
    async def save_backtest_channel(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            channels = service.save_backtest_channel(str(payload.get("channel") or ""))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"saved": True, "channels": channels}, status_code=201)

    @router.delete("/api/backtests/channels")
    async def delete_backtest_channel(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            channels = service.remove_backtest_channel(str(payload.get("channel") or ""))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"deleted": True, "channels": channels})

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

    @router.post("/api/backtests/runs/{run_id}/stop")
    async def stop_backtest_run(request: Request, run_id: str) -> JSONResponse:
        auth.require_api(request)
        result = service.stop_backtest_run(run_id)
        if result is None:
            return JSONResponse({"detail": "run_not_found"}, status_code=404)
        status_code = 202 if result.get("stopped") else 409
        return JSONResponse(result, status_code=status_code)

    @router.post("/api/backtests/runs/{run_id}/rerun")
    async def rerun_backtest_run(request: Request, run_id: str) -> JSONResponse:
        auth.require_api(request)
        result = service.rerun_backtest_run(run_id)
        if result is None:
            return JSONResponse({"detail": "run_not_found"}, status_code=404)
        return JSONResponse(result, status_code=202)

    @router.websocket("/ws/backtests")
    async def backtest_websocket(websocket: WebSocket) -> None:
        if not auth.is_authenticated_websocket(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return
        await realtime_hub.connect(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "bootstrap",
                    "readiness": service.real_backtest_readiness(),
                    "runs": service.list_backtest_runs(limit=8),
                }
            )
            while True:
                message = await websocket.receive_text()
                if message.strip().lower() == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            await realtime_hub.disconnect(websocket)
        except Exception:
            await realtime_hub.disconnect(websocket)

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
    async def settings_page(
        request: Request,
        tab: str = "controls",
        saved: str = "",
    ) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(
                request,
                {
                    "settings_view": service.safe_settings(),
                    "active_tab": tab,
                    "settings_saved": saved == "1",
                },
            ),
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

    @router.post("/settings/ai-keyword-filters")
    async def update_ai_keyword_filters(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        form = {key: str(value) for key, value in (await request.form()).items()}
        force_include_keywords = service.state.parse_keyword_text(
            form.get("force_include_keywords", "")
        )
        skip_keywords = service.state.parse_keyword_text(
            form.get("skip_keywords", "")
        )
        service.state.set_ai_keyword_filters(
            force_include_keywords=force_include_keywords,
            skip_keywords=skip_keywords,
        )
        return RedirectResponse(url="/settings?tab=ai-keywords&saved=1", status_code=303)

    @router.post("/settings/backtest-lifecycle")
    async def update_backtest_lifecycle_settings(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        form = {key: str(value) for key, value in (await request.form()).items()}
        service.state.set_backtest_lifecycle_refresh_interval(
            form.get("refresh_interval", "")
        )
        return RedirectResponse(url="/settings?tab=controls&saved=1", status_code=303)

    @router.get("/status")
    async def status(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse(service.safe_settings() | {"overview": service.overview()["cards"]})

    return router
