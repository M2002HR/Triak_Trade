"""Dashboard service layer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from triak_trade.admin_bot.runtime import get_admin_bot_status, tail_admin_bot_logs
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestRequest
from triak_trade.backtesting.report import report_to_json
from triak_trade.config.settings import Settings
from triak_trade.dashboard.schemas import AutoModeState, KillSwitchState, utc_now
from triak_trade.domain.enums import BacktestFillPolicy
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.verification.report import find_latest_report


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

    def overview(self) -> dict[str, Any]:
        admin_status = get_admin_bot_status(self.settings)
        log_status = TelegramLogChannelClient(settings=self.settings).safe_status()
        latest_report = find_latest_report(self.settings.VERIFICATION_REPORT_DIR)
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
        if form.get("real_mode") == "on" and self.settings.RUN_BACKTEST_INTEGRATION_TESTS != 1:
            return {"blocked": True, "reason": "Real backtest guard is disabled."}
        channel = form.get("channel") or self.settings.BACKTEST_DEFAULT_CHANNEL
        interval = form.get("interval") or self.settings.BACKTEST_DEFAULT_INTERVAL
        initial_balance = Decimal(form.get("initial_balance") or "1000")
        risk_pct = Decimal(form.get("risk_per_trade_pct") or "1")
        fill_policy = BacktestFillPolicy(form.get("fill_policy") or "conservative")
        now = datetime.now(timezone.utc)
        request = BacktestRequest(
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
        report = BacktestEngine().run(request)
        score = Decimal(report.warnings[0].split("=")[1]) if report.warnings else Decimal("0")
        payload = report_to_json(report, score)
        return {
            "blocked": False,
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
        report_dir = Path(self.settings.VERIFICATION_REPORT_DIR)
        files = sorted(report_dir.glob("*")) if report_dir.exists() else []
        latest = find_latest_report(self.settings.VERIFICATION_REPORT_DIR)
        preview = ""
        if latest is not None:
            preview = "\n".join(
                latest.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
            )
        return {
            "files": [str(item) for item in files],
            "latest": str(latest) if latest else None,
            "preview": preview,
        }

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
