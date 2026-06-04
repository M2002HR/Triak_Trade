"""Dashboard service layer."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from triak_trade.admin_bot.runtime import get_admin_bot_status, tail_admin_bot_logs
from triak_trade.backtesting import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestRequest
from triak_trade.backtesting.report import report_to_json
from triak_trade.config.settings import Settings
from triak_trade.core.time import parse_user_datetime_to_utc
from triak_trade.dashboard.backtest_runtime import (
    DashboardBacktestCoordinator,
    normalize_channel_reference,
    parse_telegram_message_link,
)
from triak_trade.dashboard.schemas import AutoModeState, KillSwitchState, utc_now
from triak_trade.domain.enums import BacktestFillPolicy
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient


class DashboardStateService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = Path("runtime/state")
        self.auto_mode_file = self.state_dir / "auto_mode.json"
        self.kill_switch_file = self.state_dir / "kill_switch.json"

    def get_auto_mode(self) -> AutoModeState:
        if self.auto_mode_file.exists():
            return AutoModeState.model_validate_json(
                self.auto_mode_file.read_text(encoding="utf-8")
            )
        return AutoModeState(
            enabled=self.settings.AUTO_MODE_ENABLED,
            scope=self.settings.AUTO_MODE_SCOPE,
            updated_at=utc_now(),
        )

    def set_auto_mode(self, *, enabled: bool, updated_by: str, reason: str) -> AutoModeState:
        state = AutoModeState(
            enabled=enabled,
            scope=self.settings.AUTO_MODE_SCOPE,
            updated_at=utc_now(),
            updated_by=updated_by,
            reason=reason
            or "Auto Mode is stored but does not execute live orders in this phase.",
        )
        self._write(self.auto_mode_file, state.model_dump(mode="json"))
        return state

    def get_kill_switch(self) -> KillSwitchState:
        if self.kill_switch_file.exists():
            return KillSwitchState.model_validate_json(
                self.kill_switch_file.read_text(encoding="utf-8")
            )
        return KillSwitchState(
            enabled=self.settings.KILL_SWITCH_ENABLED,
            reason=self.settings.KILL_SWITCH_REASON,
            updated_at=utc_now(),
        )

    def set_kill_switch(
        self,
        *,
        enabled: bool,
        updated_by: str,
        reason: str,
    ) -> KillSwitchState:
        state = KillSwitchState(
            enabled=enabled,
            reason=reason,
            updated_at=utc_now(),
            updated_by=updated_by,
        )
        self._write(self.kill_switch_file, state.model_dump(mode="json"))
        return state

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


class DashboardService:
    def __init__(
        self,
        settings: Settings,
        *,
        realtime_notifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.state = DashboardStateService(settings)
        self.real_runner = RealBacktestRunner(settings=settings)
        self.backtests = DashboardBacktestCoordinator(
            settings=settings,
            runner_factory=lambda: RealBacktestRunner(settings=settings),
            notifier=realtime_notifier,
        )

    def overview(self) -> dict[str, Any]:
        admin_status = get_admin_bot_status(self.settings)
        log_status = TelegramLogChannelClient(settings=self.settings).safe_status()
        latest_report = self.real_runner.report_store.latest()
        auto_mode = self.state.get_auto_mode()
        kill_switch = self.state.get_kill_switch()
        return {
            "cards": [
                {
                    "label": "Admin Bot",
                    "value": "running" if admin_status["running"] else "stopped",
                },
                {
                    "label": "Telegram Log Channel",
                    "value": "enabled" if log_status["enabled"] else "disabled",
                },
                {
                    "label": "Verification",
                    "value": "report available" if latest_report else "no report",
                },
                {
                    "label": "Toobit",
                    "value": "configured" if self._toobit_configured() else "unconfigured",
                },
                {
                    "label": "AI Gateway",
                    "value": "enabled" if self.settings.AI_GATEWAY_ENABLED else "disabled",
                },
                {"label": "Kill Switch", "value": "active" if kill_switch.enabled else "inactive"},
                {"label": "Auto Mode", "value": "active" if auto_mode.enabled else "inactive"},
            ],
            "admin_status": admin_status,
            "log_status": log_status,
            "auto_mode": auto_mode,
            "kill_switch": kill_switch,
            "recent_audit_events": [],
            "recent_proposed_actions": [],
        }

    def backtest_bootstrap(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        recent_runs = [run.model_dump(mode="json") for run in self.backtests.list_runs(limit=8)]
        return {
            "default_channel": self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            "default_interval": self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
            "default_max_messages": self.settings.REAL_BACKTEST_MAX_MESSAGES,
            "default_use_ai": (
                self.settings.REAL_BACKTEST_USE_AI
                and self.settings.AI_GATEWAY_ENABLED
                and self.settings.AI_CLASSIFIER_ENABLED
            ),
            "default_send_log_channel": self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL,
            "default_log_per_message": True,
            "default_from_date": (now - timedelta(hours=24)).isoformat(),
            "default_to_date": now.isoformat(),
            "readiness": self.real_backtest_readiness(),
            "recent_runs": recent_runs,
        }

    def start_live_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel_input = str(payload.get("channel") or "").strip()
        start_message_link = str(payload.get("start_message_link") or "").strip()
        channel_resolved: str | None = (
            normalize_channel_reference(channel_input) if channel_input else None
        )
        start_message_id: int | None = None
        normalized_start_message_link: str | None = None
        if start_message_link:
            link_channel, start_message_id = parse_telegram_message_link(start_message_link)
            normalized_start_message_link = (
                f"{link_channel}/{start_message_id}"
            )
            if channel_resolved is None:
                channel_resolved = link_channel
                channel_input = start_message_link
            elif channel_resolved != link_channel:
                raise ValueError("start_message_link must belong to the selected channel")
        if channel_resolved is None:
            raise ValueError("channel is required")
        readiness = self.backtests.readiness()
        if not readiness.get("ready", False):
            return {
                "started": False,
                "blocked": True,
                "reason": "Real backtest is not ready.",
                "issues": list(readiness.get("issues", [])),
                "readiness": readiness,
            }

        from_date = self._parse_datetime(payload.get("from_date"))
        to_date = self._parse_datetime(payload.get("to_date"))
        if from_date is None or to_date is None:
            raise ValueError("from_date and to_date are required")

        request = RealBacktestRunRequest(
            channel=channel_resolved,
            from_date=from_date,
            to_date=to_date,
            hours=None,
            start_message_link=normalized_start_message_link,
            start_message_id=start_message_id,
            interval=str(payload.get("interval") or self.settings.REAL_BACKTEST_DEFAULT_INTERVAL),
            max_messages=int(
                payload.get("max_messages") or self.settings.REAL_BACKTEST_MAX_MESSAGES
            ),
            use_ai=bool(payload.get("use_ai", self.settings.REAL_BACKTEST_USE_AI)),
            send_telegram_summary=False,
            send_log_channel=bool(
                payload.get("send_log_channel", self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL)
            ),
            log_per_message=bool(payload.get("log_per_message", True)),
        )
        run = self.backtests.start_run(request, channel_input=channel_input)
        return {
            "started": True,
            "blocked": False,
            "run": run.model_dump(mode="json"),
        }

    def get_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.backtests.get_run(run_id)
        if run is None:
            return None
        return run.model_dump(mode="json")

    def list_backtest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [run.model_dump(mode="json") for run in self.backtests.list_runs(limit=limit)]

    def run_fixture_backtest_from_form(self, form: dict[str, str]) -> dict[str, Any]:
        if form.get("real_mode") == "on":
            readiness = self.real_runner.readiness()
            if not readiness.ready:
                return {
                    "blocked": True,
                    "reason": "Real backtest is not ready.",
                    "issues": readiness.issues,
                    "readiness": readiness.model_dump(mode="json"),
                }
            channel_input = (form.get("channel") or "").strip()
            start_message_link = (form.get("start_message_link") or "").strip()
            channel_resolved: str | None = (
                normalize_channel_reference(channel_input) if channel_input else None
            )
            start_message_id: int | None = None
            normalized_start_message_link: str | None = None
            if start_message_link:
                try:
                    link_channel, start_message_id = parse_telegram_message_link(
                        start_message_link
                    )
                except ValueError as exc:
                    return {
                        "blocked": True,
                        "reason": str(exc),
                        "issues": [str(exc)],
                        "readiness": readiness.model_dump(mode="json"),
                    }
                normalized_start_message_link = f"{link_channel}/{start_message_id}"
                if channel_resolved is None:
                    channel_resolved = link_channel
                elif channel_resolved != link_channel:
                    message = "start_message_link must belong to the selected channel"
                    return {
                        "blocked": True,
                        "reason": message,
                        "issues": [message],
                        "readiness": readiness.model_dump(mode="json"),
                    }
            if channel_resolved is None:
                return {
                    "blocked": True,
                    "reason": "channel is required",
                    "issues": ["channel is required"],
                    "readiness": readiness.model_dump(mode="json"),
                }
            hours = int(
                form.get("lookback_hours")
                or self.settings.REAL_BACKTEST_DEFAULT_LOOKBACK_HOURS
            )
            real_request = RealBacktestRunRequest(
                channel=channel_resolved,
                from_date=(
                    parse_user_datetime_to_utc(form["from_date"])
                    if form.get("from_date")
                    else None
                ),
                to_date=(
                    parse_user_datetime_to_utc(form["to_date"])
                    if form.get("to_date")
                    else None
                ),
                hours=hours if not form.get("from_date") and not form.get("to_date") else None,
                start_message_link=normalized_start_message_link,
                start_message_id=start_message_id,
                interval=form.get("interval") or self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
                max_messages=int(
                    form.get("max_messages") or self.settings.REAL_BACKTEST_MAX_MESSAGES
                ),
                use_ai=form.get("use_ai") == "on",
                send_telegram_summary=form.get("send_telegram_summary") == "on",
                send_log_channel=form.get("send_log_channel") == "on",
                log_per_message=form.get("log_per_message") == "on",
            )
            result = self.real_runner.run_sync(real_request)
            return {
                "blocked": False,
                "real_mode": True,
                "success": result.success,
                "summary": {
                    "real_telegram_used": result.real_telegram_used,
                    "real_market_data_used": result.real_market_data_used,
                    "ai_used": result.ai_used,
                    "regex_fallback_used": result.regex_fallback_used,
                    "total_messages": result.total_messages,
                    "parsed_signals": result.parsed_signals,
                    "valid_signals": result.valid_signals,
                    "invalid_signals": result.invalid_signals,
                    "trades_simulated": result.trades_simulated,
                    "trades_filled": result.trades_filled,
                    "total_pnl": str(result.total_pnl),
                    "channel_score": str(result.channel_score),
                    "warnings": result.warnings,
                    "errors": result.errors,
                    "report_path": result.report_path,
                    "markdown_report_path": result.markdown_report_path,
                },
            }
        channel = form.get("channel") or self.settings.BACKTEST_DEFAULT_CHANNEL
        interval = form.get("interval") or self.settings.BACKTEST_DEFAULT_INTERVAL
        initial_balance = Decimal(form.get("initial_balance") or "1000")
        risk_pct = Decimal(form.get("risk_per_trade_pct") or "1")
        fill_policy = BacktestFillPolicy(form.get("fill_policy") or "conservative")
        now = datetime.now(timezone.utc)
        fixture_request = BacktestRequest(
            channel=channel,
            from_date=now - timedelta(days=7),
            to_date=now,
            initial_balance=initial_balance,
            interval=interval,
            fill_policy=fill_policy,
            risk_per_trade_pct=risk_pct,
            use_ai_classifier=False,
            use_regex_fallback=True,
            max_messages=self.settings.BACKTEST_MAX_MESSAGES,
            symbols=None,
        )
        report = BacktestEngine().run(fixture_request)
        score = Decimal(report.warnings[0].split("=")[1]) if report.warnings else Decimal("0")
        payload = report_to_json(report, score)
        return {
            "blocked": False,
            "real_mode": False,
            "summary": {
                "total_messages": payload["metrics"]["total_messages"],
                "parsed_signals": payload["metrics"]["parsed_signals"],
                "valid_signals": payload["metrics"]["valid_signals"],
                "trades_filled": sum(
                    1 for trade in payload["trades"] if trade["status"] != "not_filled"
                ),
                "total_pnl": payload["metrics"]["total_pnl"],
                "win_rate": payload["metrics"]["win_rate"],
                "profit_factor": payload["metrics"]["profit_factor"],
                "max_drawdown": payload["metrics"]["max_drawdown"],
                "channel_score": payload["channel_score"],
                "conservative_pnl": payload["metrics"]["conservative_pnl"],
                "optimistic_pnl": payload["metrics"]["optimistic_pnl"],
            },
        }

    def reports(self) -> dict[str, Any]:
        files = self.real_runner.report_store.list_reports()
        latest = self.real_runner.report_store.latest()
        preview = ""
        if latest is not None:
            try:
                payload = json.loads(latest.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                preview = "Latest backtest report is not valid JSON."
            else:
                preview = json.dumps(payload, indent=2, sort_keys=True)[:4000]
        return {
            "files": [str(item) for item in files],
            "latest": str(latest) if latest else None,
            "preview": preview,
        }

    def real_backtest_readiness(self) -> dict[str, Any]:
        return self.real_runner.readiness().model_dump(mode="json")

    def logs(self) -> dict[str, Any]:
        return {
            "log_channel": TelegramLogChannelClient(settings=self.settings).safe_status(),
            "runtime_logs": tail_admin_bot_logs(self.settings, lines=100),
            "audit_events": [],
        }

    def approvals(self) -> dict[str, Any]:
        return {"pending_actions": [], "empty_state": "No pending proposed actions."}

    def safe_settings(self) -> dict[str, Any]:
        token = self.settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
        session_secret = self.settings.DASHBOARD_SESSION_SECRET.get_secret_value()
        auto_mode = self.state.get_auto_mode().model_dump(mode="json")
        kill_switch = self.state.get_kill_switch().model_dump(mode="json")
        return {
            "app_name": self.settings.APP_NAME,
            "app_env": self.settings.APP_ENV,
            "log_level": self.settings.LOG_LEVEL,
            "live_trading_blocked": True,
            "execution_mode": self.settings.EXECUTION_MODE,
            "dashboard_enabled": self.settings.DASHBOARD_ENABLED,
            "dashboard_auth_enabled": self.settings.DASHBOARD_AUTH_ENABLED,
            "dashboard_token_present": bool(token),
            "dashboard_session_secret_present": bool(session_secret),
            "real_backtest_enabled": self.settings.REAL_BACKTEST_ENABLED,
            "telegram_real_test_channel": self.settings.TELEGRAM_REAL_TEST_CHANNEL,
            "ai_gateway_enabled": self.settings.AI_GATEWAY_ENABLED,
            "toobit_base_url": self.settings.TOOBIT_BASE_URL,
            "auto_mode": auto_mode,
            "kill_switch": kill_switch,
        }

    def _toobit_configured(self) -> bool:
        key = self.settings.TOOBIT_API_KEY.get_secret_value()
        secret = self.settings.TOOBIT_API_SECRET.get_secret_value()
        return bool(key and key != "replace_me" and secret and secret != "replace_me")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        return parse_user_datetime_to_utc(value)
