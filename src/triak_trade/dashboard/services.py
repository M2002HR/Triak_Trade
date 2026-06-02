"""Dashboard service layer."""

from __future__ import annotations

import json
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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = DashboardStateService(settings)
        self.real_runner = RealBacktestRunner(settings=settings)

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
            hours = int(
                form.get("lookback_hours")
                or self.settings.REAL_BACKTEST_DEFAULT_LOOKBACK_HOURS
            )
            real_request = RealBacktestRunRequest(
                channel=form.get("channel") or self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
                from_date=(
                    datetime.fromisoformat(form["from_date"]).replace(tzinfo=timezone.utc)
                    if form.get("from_date")
                    else None
                ),
                to_date=(
                    datetime.fromisoformat(form["to_date"]).replace(tzinfo=timezone.utc)
                    if form.get("to_date")
                    else None
                ),
                hours=hours if not form.get("from_date") and not form.get("to_date") else None,
                interval=form.get("interval") or self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
                max_messages=int(
                    form.get("max_messages") or self.settings.REAL_BACKTEST_MAX_MESSAGES
                ),
                use_ai=form.get("use_ai") == "on",
                send_telegram_summary=form.get("send_telegram_summary") == "on",
                send_log_channel=form.get("send_log_channel") == "on",
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
        auto_mode = self.state.get_auto_mode()
        kill_switch = self.state.get_kill_switch()
        return {
            "dashboard_enabled": self.settings.DASHBOARD_ENABLED,
            "dashboard_host": self.settings.DASHBOARD_HOST,
            "dashboard_port": self.settings.DASHBOARD_PORT,
            "dashboard_auth_enabled": self.settings.DASHBOARD_AUTH_ENABLED,
            "log_channel_enabled": self.settings.TELEGRAM_LOG_CHANNEL_ENABLED,
            "auto_mode": auto_mode.model_dump(mode="json"),
            "kill_switch": kill_switch.model_dump(mode="json"),
            "live_trading_blocked": True,
        }

    def _toobit_configured(self) -> bool:
        key = self.settings.TOOBIT_API_KEY.get_secret_value()
        secret = self.settings.TOOBIT_API_SECRET.get_secret_value()
        return bool(key and key != "replace_me" and secret and secret != "replace_me")
