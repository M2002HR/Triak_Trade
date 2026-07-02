"""Dashboard routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from triak_trade.config.settings import Settings
from triak_trade.dashboard.auth import DashboardAuth
from triak_trade.dashboard.live_runtime import DashboardLiveCoordinator
from triak_trade.dashboard.realtime import DashboardRealtimeHub
from triak_trade.dashboard.services import DashboardService
from triak_trade.live_trading.models import LiveSessionConfig, build_live_session_label


def build_router(
    *,
    settings: Settings,
    templates: Jinja2Templates,
    service: DashboardService,
    realtime_hub: DashboardRealtimeHub,
    live_coordinator: DashboardLiveCoordinator,
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
            context(
                request,
                {
                    "next_path": next,
                    "login_error": None,
                    "hide_shell": True,
                },
            ),
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
                        "hide_shell": True,
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
        result = await run_in_threadpool(service.run_fixture_backtest_from_form, form)
        return templates.TemplateResponse(
            request,
            "backtests.html",
            context(
                request,
                {
                    "result": result,
                    "default_channel": settings.REAL_BACKTEST_DEFAULT_CHANNEL,
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
    async def list_backtest_runs(
        request: Request,
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        auth.require_api(request)
        safe_limit = min(max(limit, 1), 100)
        safe_offset = max(offset, 0)
        runs = service.list_backtest_runs(limit=safe_limit, offset=safe_offset)
        total_runs = service.count_backtest_runs()
        return JSONResponse(
            {
                "runs": runs,
                "limit": safe_limit,
                "offset": safe_offset,
                "total_runs": total_runs,
                "has_more": safe_offset + len(runs) < total_runs,
            }
        )

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
                    "runs": service.list_backtest_runs(limit=8, offset=0),
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

    @router.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request, level: str = "ALL") -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        data = service.logs()
        data["active_level"] = level.upper()
        if level.upper() != "ALL":
            data["parsed_log_entries"] = [
                e for e in data["parsed_log_entries"] if e["level"] == level.upper()
            ]
        return templates.TemplateResponse(request, "logs.html", context(request, data))

    @router.get("/api/logs/tail")
    async def logs_tail(request: Request, level: str = "ALL", lines: int = 200) -> JSONResponse:
        auth.require_api(request)
        safe_lines = min(max(lines, 20), 500)
        return JSONResponse(service.logs_tail_json(lines=safe_lines, level=level))

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

    @router.post("/settings/telegram-notifications")
    async def update_telegram_notifications(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        form = {key: str(value) for key, value in (await request.form()).items()}
        bool_fields = [
            "enabled",
            "send_signal_detected", "send_signal_invalid", "send_signal_ignored",
            "send_trade_opened", "send_trade_closed", "send_trade_updated",
            "send_session_started", "send_session_stopped", "send_session_error",
            "send_session_summary", "send_daily_digest", "send_error_alerts",
        ]
        flags = {field: form.get(field) == "on" for field in bool_fields}
        service.state.set_telegram_notification_config(updated_by="dashboard", **flags)
        return RedirectResponse(url="/settings?tab=telegram&saved=1", status_code=303)

    @router.get("/status")
    async def status(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse(service.safe_settings() | {"overview": service.overview()["cards"]})

    @router.get("/reports/live", response_class=HTMLResponse)
    async def live_reports_page(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "reports_live.html",
            context(request, service.live_reports()),
        )

    # ── Live Trading Routes ────────────────────────────────────────────────

    @router.get("/live-trading", response_class=HTMLResponse)
    async def live_trading_page(request: Request) -> Response:
        redirect = auth.redirect_if_needed(request)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "live_trading.html",
            context(
                request,
                {
                    "bootstrap": live_coordinator.bootstrap(),
                    "live_mode_enabled": live_coordinator.live_mode_enabled(),
                },
            ),
        )

    @router.get("/api/live/readiness")
    async def live_readiness(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse(live_coordinator.readiness().model_dump(mode="json"))

    @router.get("/api/live/overview")
    async def get_live_overview(request: Request) -> JSONResponse:
        auth.require_api(request)
        overview = live_coordinator.get_overview()
        return JSONResponse({"overview": overview.model_dump(mode="json")})

    @router.post("/api/live/sessions/start")
    async def start_live_session(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            channels_raw = payload.get("channels") or []
            if isinstance(channels_raw, str):
                channels_raw = [c.strip() for c in channels_raw.split(",") if c.strip()]
            channels = [str(c).strip() for c in channels_raw if str(c).strip()]
            if not channels:
                return JSONResponse({"detail": "at least one channel is required"}, status_code=400)
            trading_mode = str(
                payload.get("trading_mode") or settings.LIVE_TRADING_MODE
            ).strip().lower()
            risk_per_trade_pct = Decimal(
                str(
                    payload.get("risk_per_trade_pct")
                    or settings.LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT
                )
            )
            config = LiveSessionConfig(
                channels=channels,
                trading_mode=trading_mode,
                initial_balance=Decimal("0"),
                risk_per_trade_pct=risk_per_trade_pct,
                strategy_key=str(
                    payload.get("strategy_key")
                    or settings.LIVE_TRADING_DEFAULT_STRATEGY_KEY
                ),
                use_ai=bool(payload.get("use_ai", settings.LIVE_TRADING_USE_AI)),
                interval=str(payload.get("interval") or "1m"),
                label=(
                    str(payload.get("label") or "").strip()
                    or build_live_session_label(channels[0], trading_mode)
                ),
            )
            session = live_coordinator.start_session(config)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(
            {"started": True, "session": session.model_dump(mode="json")},
            status_code=202,
        )

    @router.post("/api/live/sessions/stop")
    async def stop_live_session(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = (
            await request.json()
            if request.headers.get("content-type") == "application/json"
            else {}
        )
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        session = live_coordinator.stop_session(str(session_id) if session_id else None)
        if session is None:
            return JSONResponse({"detail": "no_active_session"}, status_code=404)
        return JSONResponse({"stopped": True, "session": session.model_dump(mode="json")})

    @router.post("/api/live/sessions/{session_id}/stop")
    async def stop_live_session_by_id(request: Request, session_id: str) -> JSONResponse:
        auth.require_api(request)
        session = live_coordinator.stop_session(session_id)
        if session is None:
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        return JSONResponse({"stopped": True, "session": session.model_dump(mode="json")})

    @router.get("/api/live/sessions/current")
    async def get_current_live_session(request: Request) -> JSONResponse:
        auth.require_api(request)
        session = live_coordinator.get_current_session()
        if session is None:
            return JSONResponse({"session": None, "is_running": False})
        return JSONResponse(
            {
                "session": session.model_dump(mode="json"),
                "is_running": live_coordinator.is_running(),
            }
        )

    @router.get("/api/live/sessions/{session_id}")
    async def get_live_session_detail(request: Request, session_id: str) -> JSONResponse:
        auth.require_api(request)
        detail = live_coordinator.get_session_detail(session_id)
        if detail is None:
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        return JSONResponse({"detail": detail.model_dump(mode="json")})

    @router.delete("/api/live/sessions/{session_id}")
    async def delete_live_session_history(request: Request, session_id: str) -> JSONResponse:
        auth.require_api(request)
        try:
            deleted = live_coordinator.delete_session_history(session_id)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        if not deleted:
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        return JSONResponse({"deleted": True, "session_id": session_id})

    @router.post("/api/live/telegram/forward-message")
    async def forward_live_telegram_message(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        message_link = str(payload.get("message_link") or "").strip()
        destination_channel = str(payload.get("destination_channel") or "").strip()
        if not message_link or not destination_channel:
            return JSONResponse(
                {"detail": "message_link and destination_channel are required"},
                status_code=400,
            )
        try:
            trace = await live_coordinator.forward_test_message(
                message_link=message_link,
                destination_channel=destination_channel,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse(
                {"detail": f"telegram_forward_failed: {type(exc).__name__}: {exc}"},
                status_code=502,
            )
        return JSONResponse({"forwarded": True, "message": trace.model_dump(mode="json")})

    @router.get("/api/live/snapshot")
    async def get_live_snapshot(request: Request, session_id: str | None = None) -> JSONResponse:
        auth.require_api(request)
        snap = live_coordinator.get_snapshot(session_id=session_id)
        if snap is None:
            return JSONResponse({"snapshot": None})
        return JSONResponse({"snapshot": snap.model_dump(mode="json")})

    @router.get("/api/live/sessions")
    async def list_live_sessions(request: Request, limit: int = 10) -> JSONResponse:
        auth.require_api(request)
        sessions = live_coordinator.list_sessions(limit=limit)
        return JSONResponse({"sessions": [s.model_dump(mode="json") for s in sessions]})

    @router.get("/api/live/sessions/{session_id}/trades")
    async def list_session_trades(
        request: Request, session_id: str, open_only: bool = False
    ) -> JSONResponse:
        auth.require_api(request)
        trades = live_coordinator.list_trades(session_id, open_only=open_only)
        return JSONResponse({"trades": [t.model_dump(mode="json") for t in trades]})

    @router.get("/api/live/sessions/{session_id}/messages")
    async def list_session_messages(
        request: Request,
        session_id: str,
        limit: int = 100,
    ) -> JSONResponse:
        auth.require_api(request)
        detail = live_coordinator.get_session_detail(session_id, message_limit=limit)
        if detail is None:
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        return JSONResponse(
            {"messages": [item.model_dump(mode="json") for item in detail.messages]}
        )

    @router.delete("/api/live/sessions/{session_id}/trades/{trade_id}")
    async def delete_live_trade_record(
        request: Request,
        session_id: str,
        trade_id: str,
    ) -> JSONResponse:
        auth.require_api(request)
        deleted = live_coordinator.delete_trade_record(session_id, trade_id)
        if not deleted:
            return JSONResponse({"detail": "trade_not_found"}, status_code=404)
        return JSONResponse({"deleted": True, "trade_id": trade_id})

    @router.delete("/api/live/sessions/{session_id}/messages/{message_id}")
    async def delete_live_message_record(
        request: Request,
        session_id: str,
        message_id: int,
        channel_id: str,
    ) -> JSONResponse:
        auth.require_api(request)
        deleted = live_coordinator.delete_message_record(session_id, message_id, channel_id)
        if not deleted:
            return JSONResponse({"detail": "message_not_found"}, status_code=404)
        return JSONResponse({"deleted": True, "message_id": message_id, "channel_id": channel_id})

    @router.get("/api/live/account")
    async def get_live_account(request: Request) -> JSONResponse:
        """Fetch live Toobit account info directly from the exchange API."""
        auth.require_api(request)
        data = await live_coordinator.fetch_account_info_direct()
        return JSONResponse(data)

    @router.get("/api/live/messages")
    async def get_live_messages(request: Request, limit: int = 50) -> JSONResponse:
        auth.require_api(request)
        traces = live_coordinator.get_recent_messages(limit=limit)
        return JSONResponse({"messages": [t.model_dump(mode="json") for t in traces]})

    # ── Saved Channel Management ──────────────────────────────────────────

    @router.get("/api/live/channels")
    async def list_live_channels(request: Request) -> JSONResponse:
        auth.require_api(request)
        return JSONResponse({"channels": live_coordinator.get_saved_channels()})

    @router.post("/api/live/channels")
    async def save_live_channel(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            channels = live_coordinator.save_channel(str(payload.get("channel") or ""))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"saved": True, "channels": channels}, status_code=201)

    @router.delete("/api/live/channels")
    async def delete_live_channel(request: Request) -> JSONResponse:
        auth.require_api(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid_payload"}, status_code=400)
        try:
            channels = live_coordinator.remove_channel(str(payload.get("channel") or ""))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"deleted": True, "channels": channels})

    @router.websocket("/ws/live")
    async def live_websocket(websocket: WebSocket) -> None:
        if not auth.is_authenticated_websocket(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return
        await realtime_hub.connect(websocket)
        try:
            overview = live_coordinator.get_overview()
            await websocket.send_json(
                {
                    "type": "live_bootstrap",
                    "bootstrap": live_coordinator.bootstrap(),
                    "overview": overview.model_dump(mode="json"),
                }
            )
            while True:
                msg = await websocket.receive_text()
                if msg.strip().lower() == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            await realtime_hub.disconnect(websocket)
        except Exception:
            await realtime_hub.disconnect(websocket)

    return router
