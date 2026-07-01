"""Dashboard service layer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from triak_trade.backtesting import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestRequest
from triak_trade.backtesting.report import extract_channel_score, report_to_json
from triak_trade.backtesting.strategies.registry import (
    build_strategy_from_key,
    list_available_strategies,
)
from triak_trade.config.settings import Settings
from triak_trade.core.time import parse_user_datetime_to_utc
from triak_trade.dashboard.backtest_runtime import (
    DashboardBacktestCoordinator,
    normalize_channel_reference,
    parse_telegram_message_link,
)
from triak_trade.dashboard.env_config import RootEnvConfigEditor
from triak_trade.dashboard.schemas import (
    AIKeywordFilterConfig,
    AutoModeState,
    BacktestLifecycleConfig,
    KillSwitchState,
    SavedChannelEntry,
    SavedChannelsState,
    StrategyCatalogEntry,
    TelegramNotificationConfig,
    utc_now,
)
from triak_trade.domain.enums import BacktestFillPolicy
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient


class DashboardStateService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = Path(settings.DASHBOARD_RUNTIME_DIR) / "state"
        configured_root_env = Path(settings.ROOT_ENV_FILE)
        if configured_root_env.is_absolute():
            self.root_env_file = configured_root_env
        else:
            self.root_env_file = Path(__file__).resolve().parents[3] / configured_root_env
        self.auto_mode_file = self.state_dir / "auto_mode.json"
        self.kill_switch_file = self.state_dir / "kill_switch.json"
        self.saved_channels_file = self.state_dir / "saved_channels.json"
        self.telegram_notification_file = self.state_dir / "telegram_notifications.json"
        self.root_env_editor = RootEnvConfigEditor(self.root_env_file)

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

    def get_saved_channels(self) -> SavedChannelsState:
        if self.saved_channels_file.exists():
            return SavedChannelsState.model_validate_json(
                self.saved_channels_file.read_text(encoding="utf-8")
            )
        default_channel = normalize_channel_reference(self.settings.REAL_BACKTEST_DEFAULT_CHANNEL)
        default_label = self._channel_label(default_channel)
        state = SavedChannelsState(
            channels=[
                SavedChannelEntry(
                    channel_input=self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
                    channel_resolved=default_channel,
                    label=default_label,
                    created_at=utc_now(),
                )
            ]
        )
        self._write(self.saved_channels_file, state.model_dump(mode="json"))
        return state

    def add_saved_channel(self, channel_input: str) -> SavedChannelsState:
        normalized_input = channel_input.strip()
        if not normalized_input:
            raise ValueError("channel is required")
        resolved = normalize_channel_reference(normalized_input)
        label = self._channel_label(resolved)
        state = self.get_saved_channels()
        existing = [
            item for item in state.channels if item.channel_resolved != resolved
        ]
        existing.insert(
            0,
            SavedChannelEntry(
                channel_input=normalized_input,
                channel_resolved=resolved,
                label=label,
                created_at=utc_now(),
            ),
        )
        updated = SavedChannelsState(channels=existing[:50])
        self._write(self.saved_channels_file, updated.model_dump(mode="json"))
        return updated

    def remove_saved_channel(self, channel_reference: str) -> SavedChannelsState:
        resolved = normalize_channel_reference(channel_reference)
        state = self.get_saved_channels()
        remaining = [
            item for item in state.channels if item.channel_resolved != resolved
        ]
        updated = SavedChannelsState(channels=remaining)
        self._write(self.saved_channels_file, updated.model_dump(mode="json"))
        return updated

    def get_ai_keyword_filters(self) -> AIKeywordFilterConfig:
        return AIKeywordFilterConfig(
            force_include_keywords=self._normalize_keywords(
                self.settings.AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS
            ),
            skip_keywords=self._normalize_keywords(
                self.settings.AI_CLASSIFIER_SKIP_KEYWORDS
            ),
            config_path=str(self.root_env_file),
        )

    def set_ai_keyword_filters(
        self,
        *,
        force_include_keywords: list[str],
        skip_keywords: list[str],
    ) -> AIKeywordFilterConfig:
        normalized_force = self._normalize_keywords(force_include_keywords)
        normalized_skip = self._normalize_keywords(skip_keywords)
        self.root_env_editor.update_values(
            {
                "AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS": ",".join(normalized_force),
                "AI_CLASSIFIER_SKIP_KEYWORDS": ",".join(normalized_skip),
            }
        )
        self.settings.AI_CLASSIFIER_FORCE_INCLUDE_KEYWORDS = normalized_force
        self.settings.AI_CLASSIFIER_SKIP_KEYWORDS = normalized_skip
        return self.get_ai_keyword_filters()

    def get_backtest_lifecycle_config(self) -> BacktestLifecycleConfig:
        return BacktestLifecycleConfig(
            refresh_interval=self.settings.BACKTEST_LIFECYCLE_REFRESH_INTERVAL,
            config_path=str(self.root_env_file),
        )

    def set_backtest_lifecycle_refresh_interval(
        self,
        refresh_interval: str,
    ) -> BacktestLifecycleConfig:
        interval = refresh_interval.strip().lower()
        self.root_env_editor.update_values(
            {"BACKTEST_LIFECYCLE_REFRESH_INTERVAL": interval}
        )
        self.settings.BACKTEST_LIFECYCLE_REFRESH_INTERVAL = interval
        return self.get_backtest_lifecycle_config()

    def get_telegram_notification_config(self) -> TelegramNotificationConfig:
        if self.telegram_notification_file.exists():
            try:
                return TelegramNotificationConfig.model_validate_json(
                    self.telegram_notification_file.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        return TelegramNotificationConfig()

    def set_telegram_notification_config(
        self,
        *,
        updated_by: str = "dashboard",
        **flags: bool,
    ) -> TelegramNotificationConfig:
        current = self.get_telegram_notification_config()
        data = current.model_dump()
        for key, value in flags.items():
            if key in data:
                data[key] = bool(value)
        data["updated_at"] = utc_now().isoformat()
        data["updated_by"] = updated_by
        config = TelegramNotificationConfig.model_validate(data)
        self._write(self.telegram_notification_file, config.model_dump(mode="json"))
        return config

    @staticmethod
    def _channel_label(channel_reference: str) -> str:
        if channel_reference.startswith("https://t.me/"):
            return f"@{channel_reference.rsplit('/', 1)[-1]}"
        return channel_reference

    @staticmethod
    def parse_keyword_text(value: str) -> list[str]:
        if not value.strip():
            return []
        pieces = [
            item.strip()
            for chunk in value.splitlines()
            for item in chunk.split(",")
        ]
        return [item for item in pieces if item]

    @staticmethod
    def _normalize_keywords(values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            stripped = item.strip()
            if not stripped:
                continue
            folded = stripped.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            normalized.append(stripped)
        return normalized

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
        log_status = TelegramLogChannelClient(settings=self.settings).safe_status()
        latest_report = self.real_runner.report_store.latest()
        auto_mode = self.state.get_auto_mode()
        kill_switch = self.state.get_kill_switch()
        return {
            "cards": [
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
            "log_status": log_status,
            "auto_mode": auto_mode,
            "kill_switch": kill_switch,
            "recent_audit_events": [],
        }

    def backtest_bootstrap(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        readiness = self.real_backtest_readiness()
        recent_runs = [
            run.model_dump(mode="json")
            for run in self.backtests.list_run_summaries(limit=8, offset=0)
        ]
        saved_channels = self.state.get_saved_channels()
        strategies = [
            StrategyCatalogEntry.model_validate(item).model_dump(mode="json")
            for item in list_available_strategies()
        ]
        default_strategy_key = next(
            (item["key"] for item in strategies if item.get("active")),
            "default_risk_managed",
        )
        return {
            "default_channel": self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            "default_interval": self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
            "default_lifecycle_refresh_interval": self.settings.BACKTEST_LIFECYCLE_REFRESH_INTERVAL,
            "default_max_messages": self.settings.REAL_BACKTEST_MAX_MESSAGES,
            "default_initial_balance": str(self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE),
            "default_risk_per_trade_pct": str(
                self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT
            ),
            "default_use_ai": (
                self.settings.REAL_BACKTEST_USE_AI
                and self.settings.AI_GATEWAY_ENABLED
                and self.settings.AI_CLASSIFIER_ENABLED
            ),
            "default_send_log_channel": self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL,
            "default_log_per_message": True,
            "default_strategy_key": default_strategy_key,
            "available_strategies": strategies,
            "default_from_date": (now - timedelta(hours=24)).isoformat(),
            "default_to_date": now.isoformat(),
            "readiness": readiness,
            "recent_runs": recent_runs,
            "saved_channels": [item.model_dump(mode="json") for item in saved_channels.channels],
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
        readiness = self.real_backtest_readiness()
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
            initial_balance=Decimal(
                str(
                    payload.get("initial_balance")
                    or self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE
                )
            ),
            risk_per_trade_pct=Decimal(
                str(
                    payload.get("risk_per_trade_pct")
                    or self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT
                )
            ),
            use_ai=bool(payload.get("use_ai", self.settings.REAL_BACKTEST_USE_AI)),
            send_telegram_summary=False,
            send_log_channel=bool(
                payload.get("send_log_channel", self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL)
            ),
            log_per_message=bool(payload.get("log_per_message", True)),
        )
        strategy_key = str(
            payload.get("strategy_key") or self.backtest_bootstrap()["default_strategy_key"]
        )
        build_strategy_from_key(strategy_key)
        run = self.backtests.start_run(
            request,
            channel_input=channel_input,
            strategy_key=strategy_key,
        )
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

    def list_backtest_runs(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        return [
            run.model_dump(mode="json")
            for run in self.backtests.list_run_summaries(limit=limit, offset=offset)
        ]

    def count_backtest_runs(self) -> int:
        return self.backtests.count_runs()

    def stop_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        run, stopped, reason = self.backtests.stop_run(run_id)
        if run is None:
            return None
        return {
            "stopped": stopped,
            "reason": reason,
            "run": run.model_dump(mode="json"),
        }

    def rerun_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.backtests.rerun_run(run_id)
        if run is None:
            return None
        return {
            "started": True,
            "rerun_of": run_id,
            "run": run.model_dump(mode="json"),
        }

    def list_saved_channels(self) -> list[dict[str, Any]]:
        state = self.state.get_saved_channels()
        return [item.model_dump(mode="json") for item in state.channels]

    def save_backtest_channel(self, channel_input: str) -> list[dict[str, Any]]:
        state = self.state.add_saved_channel(channel_input)
        return [item.model_dump(mode="json") for item in state.channels]

    def remove_backtest_channel(self, channel_reference: str) -> list[dict[str, Any]]:
        state = self.state.remove_saved_channel(channel_reference)
        return [item.model_dump(mode="json") for item in state.channels]

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
                initial_balance=Decimal(
                    form.get("initial_balance")
                    or str(self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE)
                ),
                risk_per_trade_pct=Decimal(
                    form.get("risk_per_trade_pct")
                    or str(self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT)
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
        initial_balance = Decimal(
            form.get("initial_balance") or str(self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE)
        )
        risk_pct = Decimal(
            form.get("risk_per_trade_pct")
            or str(self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT)
        )
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
        score = extract_channel_score(report.warnings)
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
        reports: list[dict[str, Any]] = []
        invalid_files: list[dict[str, str]] = []
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                invalid_files.append(
                    {"path": str(path), "reason": "Invalid JSON payload."}
                )
                continue
            reports.append(self._build_report_catalog_entry(payload, path))
        latest = reports[0] if reports else None
        return {
            "reports_bootstrap": {
                "reports": reports,
                "summary": self._build_report_library_summary(reports),
                "invalid_files": invalid_files,
                "latest_report_id": latest["report_id"] if latest else None,
                "sort_options": [
                    {"value": "generated_at", "label": "Newest"},
                    {"value": "score_value", "label": "Score"},
                    {"value": "total_pnl_value", "label": "PnL"},
                    {"value": "win_rate_pct", "label": "Win Rate"},
                    {"value": "profit_factor_value", "label": "Profit Factor"},
                    {"value": "fill_rate_pct", "label": "Fill Rate"},
                    {"value": "max_drawdown_value", "label": "Lowest Drawdown"},
                    {"value": "trades_filled", "label": "Filled Trades"},
                ],
            },
            "has_reports": bool(reports),
        }

    def real_backtest_readiness(self) -> dict[str, Any]:
        return self.real_runner.readiness().model_dump(mode="json")

    def logs(self) -> dict[str, Any]:
        raw_lines = self._tail_dashboard_logs(lines=500)
        parsed = self._parse_log_entries(raw_lines)
        stats = self._log_stats(parsed)
        return {
            "log_channel": TelegramLogChannelClient(settings=self.settings).safe_status(),
            "runtime_logs": raw_lines[-100:],
            "parsed_log_entries": parsed[-200:],
            "log_stats": stats,
            "audit_events": [],
        }

    def logs_tail_json(self, *, lines: int = 200, level: str = "ALL") -> dict[str, Any]:
        raw_lines = self._tail_dashboard_logs(lines=max(lines * 3, 600))
        parsed = self._parse_log_entries(raw_lines)
        level_upper = level.upper()
        if level_upper != "ALL":
            parsed = [e for e in parsed if e["level"] == level_upper]
        return {
            "entries": parsed[-lines:],
            "stats": self._log_stats(self._parse_log_entries(self._tail_dashboard_logs(lines=600))),
        }

    def live_reports(self) -> dict[str, Any]:
        """Build a per-channel live trading performance report from stored session history."""
        from triak_trade.live_trading.store import LiveTradingStore  # lazy import
        store = LiveTradingStore(self.settings.LIVE_TRADING_RUNTIME_DIR)
        sessions = store.list_sessions(limit=200)

        channel_map: dict[str, dict[str, Any]] = {}
        for session in sessions:
            session_data = session.model_dump(mode="json")
            channels = session_data.get("channels") or []
            if isinstance(channels, str):
                channels = [channels]
            for ch in channels:
                if ch not in channel_map:
                    channel_map[ch] = {
                        "channel": ch,
                        "channel_label": ch.rsplit("/", 1)[-1] if "/" in ch else ch,
                        "sessions": [],
                        "total_trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "total_pnl": 0.0,
                        "open_trades": 0,
                        "total_messages": 0,
                        "total_signals": 0,
                        "last_session_at": None,
                    }
                entry = channel_map[ch]
                entry["sessions"].append(session_data)

                metrics = session_data.get("metrics") or {}
                entry["total_trades"] += int(metrics.get("total_trades") or 0)
                entry["wins"] += int(metrics.get("wins") or 0)
                entry["losses"] += int(metrics.get("losses") or 0)
                entry["open_trades"] += int(metrics.get("open_positions") or 0)
                entry["total_messages"] += int(metrics.get("messages_processed") or 0)
                entry["total_signals"] += int(metrics.get("signals_detected") or 0)
                raw_pnl = metrics.get("total_pnl") or "0"
                try:
                    entry["total_pnl"] += float(Decimal(str(raw_pnl)))
                except Exception:
                    pass

                started = session_data.get("started_at") or session_data.get("created_at")
                if started and (
                    entry["last_session_at"] is None or started > entry["last_session_at"]
                ):
                    entry["last_session_at"] = started

        channel_reports = []
        for ch_data in channel_map.values():
            t = ch_data["total_trades"]
            w = ch_data["wins"]
            win_rate = (w / t * 100) if t > 0 else 0.0
            pnl = ch_data["total_pnl"]
            ch_data["win_rate_pct"] = round(win_rate, 1)
            ch_data["win_rate_label"] = f"{win_rate:.1f}%"
            ch_data["total_pnl_label"] = f"{pnl:+.2f}"
            ch_data["pnl_positive"] = pnl > 0
            ch_data["session_count"] = len(ch_data["sessions"])
            ch_data["sessions"] = ch_data["sessions"][:10]
            channel_reports.append(ch_data)

        channel_reports.sort(key=lambda x: x["last_session_at"] or "", reverse=True)

        total_trades = sum(r["total_trades"] for r in channel_reports)
        total_wins = sum(r["wins"] for r in channel_reports)
        total_pnl = sum(r["total_pnl"] for r in channel_reports)
        overall_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

        return {
            "channel_reports": channel_reports,
            "summary": {
                "total_channels": len(channel_reports),
                "total_sessions": len(sessions),
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_pnl": total_pnl,
                "total_pnl_label": f"{total_pnl:+.2f}",
                "overall_win_rate_pct": round(overall_win_rate, 1),
                "overall_win_rate_label": f"{overall_win_rate:.1f}%",
                "active_channels": sum(
                    1 for r in channel_reports if r["open_trades"] > 0
                ),
            },
            "has_data": bool(channel_reports),
        }

    def safe_settings(self) -> dict[str, Any]:
        token = self.settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
        session_secret = self.settings.DASHBOARD_SESSION_SECRET.get_secret_value()
        auto_mode = self.state.get_auto_mode().model_dump(mode="json")
        kill_switch = self.state.get_kill_switch().model_dump(mode="json")
        ai_keyword_filters = self.state.get_ai_keyword_filters().model_dump(mode="json")
        return {
            "app_name": self.settings.APP_NAME,
            "app_env": self.settings.APP_ENV,
            "log_level": self.settings.LOG_LEVEL,
            "live_trading_blocked": not self.settings.LIVE_TRADING_LIVE_MODE_ENABLED,
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
            "ai_keyword_filters": ai_keyword_filters,
            "backtest_lifecycle": self.state.get_backtest_lifecycle_config().model_dump(
                mode="json"
            ),
            "telegram_notifications": self.state.get_telegram_notification_config().model_dump(
                mode="json"
            ),
            "telegram_log_channel": TelegramLogChannelClient(settings=self.settings).safe_status(),
        }

    def _toobit_configured(self) -> bool:
        key = self.settings.TOOBIT_API_KEY.get_secret_value()
        secret = self.settings.TOOBIT_API_SECRET.get_secret_value()
        return bool(key and key != "replace_me" and secret and secret != "replace_me")

    def _tail_dashboard_logs(self, *, lines: int) -> list[str]:
        path = Path(self.settings.DASHBOARD_LOG_FILE)
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]

    # ── Log parsing helpers ────────────────────────────────────────────────

    _LOG_PYTHON_RE = re.compile(
        r'^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)\s+'
        r'(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+'
        r'(?P<rest>.*)$',
        re.IGNORECASE,
    )
    _LOG_UVICORN_RE = re.compile(
        r'^(?P<level>INFO|WARNING|ERROR|CRITICAL|DEBUG):\s+(?P<rest>.*)$',
        re.IGNORECASE,
    )

    _LEVEL_ORDER: ClassVar[dict[str, int]] = {
        "DEBUG": 0,
        "INFO": 1,
        "WARNING": 2,
        "ERROR": 3,
        "CRITICAL": 4,
    }

    def _parse_log_entries(self, raw_lines: list[str]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for i, line in enumerate(raw_lines):
            line = line.strip()
            if not line:
                continue
            entry = self._parse_log_line(i + 1, line)
            entries.append(entry)
        return entries

    def _parse_log_line(self, lineno: int, raw: str) -> dict[str, Any]:
        from triak_trade.verification.redaction import redact_text  # local import

        # Try JSON (from append_log)
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                ts = str(data.get("timestamp") or "")[:19].replace("T", " ")
                event = str(data.get("event") or "")
                payload = data.get("payload") or {}
                raw_level = str(data.get("level") or "").upper()
                level = raw_level if raw_level in self._LEVEL_ORDER else (
                    "ERROR" if "error" in event.lower() else "INFO"
                )
                module = str(data.get("module") or "").strip()
                if not module:
                    module = event.split(".")[0] if "." in event else "runtime"
                msg = event
                if payload:
                    short = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:4])
                    msg = f"{event}  [{short}]"
                return {
                    "lineno": lineno,
                    "ts": ts,
                    "level": level,
                    "module": module,
                    "message": redact_text(msg),
                    "raw": redact_text(raw),
                    "is_json": True,
                }
            except (ValueError, KeyError):
                pass

        # Try standard Python log format
        m = self._LOG_PYTHON_RE.match(raw)
        if m:
            ts = m.group("ts")[:19].replace("T", " ").replace(",", ".")
            level = m.group("level").upper()
            rest = m.group("rest")
            parts = rest.split(" - ", 1)
            module = parts[0].strip() if len(parts) > 1 else ""
            message = parts[1].strip() if len(parts) > 1 else rest.strip()
            return {
                "lineno": lineno,
                "ts": ts,
                "level": level,
                "module": module,
                "message": redact_text(message),
                "raw": redact_text(raw),
                "is_json": False,
            }

        # Try uvicorn format: "INFO:     GET /path HTTP/1.1 200 OK"
        m2 = self._LOG_UVICORN_RE.match(raw)
        if m2:
            return {
                "lineno": lineno,
                "ts": "",
                "level": m2.group("level").upper(),
                "module": "uvicorn",
                "message": redact_text(m2.group("rest")),
                "raw": redact_text(raw),
                "is_json": False,
            }

        # Fallback — raw line
        return {
            "lineno": lineno,
            "ts": "",
            "level": "DEBUG",
            "module": "",
            "message": redact_text(raw),
            "raw": redact_text(raw),
            "is_json": False,
        }

    @staticmethod
    def _log_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        for e in entries:
            lvl = e.get("level", "DEBUG")
            if lvl in counts:
                counts[lvl] += 1
        return {
            "total": len(entries),
            "by_level": counts,
            "has_errors": counts["ERROR"] + counts["CRITICAL"] > 0,
            "has_warnings": counts["WARNING"] > 0,
        }

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        return parse_user_datetime_to_utc(value)

    def _build_report_catalog_entry(
        self,
        payload: dict[str, Any],
        path: Path,
    ) -> dict[str, Any]:
        nested_raw = payload.get("report")
        nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else {}
        metrics_raw = nested.get("metrics")
        metrics: dict[str, Any] = metrics_raw if isinstance(metrics_raw, dict) else {}
        trades_raw = nested.get("trades")
        trades: list[dict[str, Any]] = trades_raw if isinstance(trades_raw, list) else []
        score_breakdown = (
            nested.get("score_breakdown")
            if isinstance(nested.get("score_breakdown"), dict)
            else {}
        )
        score_breakdown_dict: dict[str, Any] = (
            score_breakdown if isinstance(score_breakdown, dict) else {}
        )
        trade_status_counts = (
            nested.get("trade_status_counts")
            if isinstance(nested.get("trade_status_counts"), dict)
            else {}
        )
        trade_status_counts_dict: dict[str, Any] = (
            trade_status_counts if isinstance(trade_status_counts, dict) else {}
        )
        symbol_summary = (
            nested.get("symbol_summary")
            if isinstance(nested.get("symbol_summary"), list)
            else []
        )
        symbol_summary_rows: list[Any] = symbol_summary if isinstance(symbol_summary, list) else []
        equity_curve = (
            nested.get("equity_curve")
            if isinstance(nested.get("equity_curve"), list)
            else []
        )
        equity_curve_rows: list[Any] = equity_curve if isinstance(equity_curve, list) else []
        report_id = str(path)
        channel = str(payload.get("channel") or nested.get("channel_id") or "unknown")
        score_value = self._decimal_to_float(
            payload.get("channel_score") or nested.get("channel_score") or "0"
        )
        win_rate = self._decimal_to_float(
            payload.get("win_rate") or metrics.get("win_rate") or "0"
        )
        total_pnl = self._decimal_to_float(
            payload.get("total_pnl") or metrics.get("total_pnl") or "0"
        )
        profit_factor = payload.get("profit_factor") or metrics.get("profit_factor")
        max_drawdown = self._decimal_to_float(
            payload.get("max_drawdown") or metrics.get("max_drawdown") or "0"
        )
        simulated = int(payload.get("trades_simulated") or len(trades) or 0)
        filled = int(payload.get("trades_filled") or 0)
        fill_rate = (filled / simulated * 100) if simulated else 0.0
        initial_balance = self._decimal_to_float(
            nested.get("initial_balance") or payload.get("initial_balance") or "0"
        )
        final_balance = self._decimal_to_float(
            nested.get("final_balance") or payload.get("final_balance") or "0"
        )
        generated_at = str(payload.get("generated_at") or "")
        from_date = str(payload.get("from_date") or nested.get("from_date") or "")
        to_date = str(payload.get("to_date") or nested.get("to_date") or "")
        pnl_positive = total_pnl > 0
        return {
            "report_id": report_id,
            "file_name": path.name,
            "report_path": str(payload.get("report_path") or path),
            "markdown_report_path": str(payload.get("markdown_report_path") or ""),
            "channel": channel,
            "channel_label": channel.rsplit("/", 1)[-1] if "/" in channel else channel,
            "generated_at": generated_at,
            "from_date": from_date,
            "to_date": to_date,
            "success": bool(payload.get("success", False)),
            "success_label": "Complete" if payload.get("success", False) else "Failed",
            "ai_used": bool(payload.get("ai_used", False)),
            "real_telegram_used": bool(payload.get("real_telegram_used", False)),
            "real_market_data_used": bool(payload.get("real_market_data_used", False)),
            "regex_fallback_used": bool(payload.get("regex_fallback_used", False)),
            "total_messages": int(payload.get("total_messages") or 0),
            "classified_messages": int(payload.get("classified_messages") or 0),
            "parsed_signals": int(
                payload.get("parsed_signals") or metrics.get("parsed_signals") or 0
            ),
            "valid_signals": int(
                payload.get("valid_signals") or metrics.get("valid_signals") or 0
            ),
            "ignored_messages": int(
                payload.get("ignored_messages") or metrics.get("ignored_messages") or 0
            ),
            "ambiguous_messages": int(payload.get("ambiguous_messages") or 0),
            "invalid_signals": int(
                payload.get("invalid_signals") or metrics.get("invalid_signals") or 0
            ),
            "trades_simulated": simulated,
            "trades_filled": filled,
            "wins": int(payload.get("wins") or 0),
            "losses": int(payload.get("losses") or 0),
            "score_value": score_value,
            "score_label": f"{score_value:.2f}",
            "win_rate_pct": win_rate * 100,
            "win_rate_label": f"{win_rate * 100:.1f}%",
            "profit_factor_value": self._decimal_to_float(profit_factor or "0"),
            "profit_factor_label": (
                f"{self._decimal_to_float(profit_factor):.2f}"
                if profit_factor not in {None, "None"}
                else "n/a"
            ),
            "max_drawdown_value": max_drawdown,
            "max_drawdown_label": f"{max_drawdown:.2f}",
            "total_pnl_value": total_pnl,
            "total_pnl_label": f"{total_pnl:.2f}",
            "fill_rate_pct": fill_rate,
            "fill_rate_label": f"{fill_rate:.1f}%",
            "initial_balance": initial_balance,
            "final_balance": final_balance,
            "pnl_positive": pnl_positive,
            "score_breakdown": self._normalize_score_breakdown(score_breakdown_dict),
            "trade_status_counts": [
                {"status": status, "count": count}
                for status, count in trade_status_counts_dict.items()
            ],
            "symbol_summary": [
                {
                    "symbol": item.get("symbol"),
                    "trades": int(item.get("trades") or 0),
                    "wins": int(item.get("wins") or 0),
                    "losses": int(item.get("losses") or 0),
                    "not_filled": int(item.get("not_filled") or 0),
                    "pnl_value": self._decimal_to_float(item.get("pnl") or "0"),
                    "pnl_label": (
                        f"{self._decimal_to_float(item.get('pnl') or '0'):.2f}"
                    ),
                }
                for item in symbol_summary_rows
                if isinstance(item, dict)
            ],
            "equity_curve": [
                {
                    "index": int(point.get("index") or 0),
                    "signal_id": point.get("signal_id"),
                    "symbol": point.get("symbol"),
                    "status": point.get("status"),
                    "pnl_value": self._decimal_to_float(point.get("pnl") or "0"),
                    "equity_value": self._decimal_to_float(point.get("equity") or "0"),
                    "exit_time": point.get("exit_time"),
                }
                for point in equity_curve_rows
                if isinstance(point, dict)
            ],
            "trades": [
                self._normalize_trade_row(trade)
                for trade in trades
                if isinstance(trade, dict)
            ],
            "warnings": list(payload.get("warnings") or []),
            "errors": list(payload.get("errors") or []),
            "skipped_reasons": list(payload.get("skipped_reasons") or []),
        }

    @staticmethod
    def _build_report_library_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
        if not reports:
            return {
                "total_reports": 0,
                "successful_reports": 0,
                "avg_score": 0.0,
                "best_score": 0.0,
                "avg_win_rate_pct": 0.0,
                "positive_pnl_reports": 0,
                "channels": 0,
            }
        total = len(reports)
        successful = sum(1 for report in reports if report["success"])
        avg_score = sum(report["score_value"] for report in reports) / total
        best_score = max(report["score_value"] for report in reports)
        avg_win_rate = sum(report["win_rate_pct"] for report in reports) / total
        positive = sum(1 for report in reports if report["total_pnl_value"] > 0)
        channels = len({report["channel"] for report in reports})
        return {
            "total_reports": total,
            "successful_reports": successful,
            "avg_score": round(avg_score, 2),
            "best_score": round(best_score, 2),
            "avg_win_rate_pct": round(avg_win_rate, 2),
            "positive_pnl_reports": positive,
            "channels": channels,
        }

    @staticmethod
    def _normalize_trade_row(trade: dict[str, Any]) -> dict[str, Any]:
        pnl_value = DashboardService._decimal_to_float(trade.get("pnl") or "0")
        quantity_value = DashboardService._decimal_to_float(trade.get("quantity") or "0")
        return {
            "trade_id": trade.get("trade_id"),
            "signal_id": trade.get("signal_id"),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "status": trade.get("status"),
            "entry_time": trade.get("entry_time"),
            "exit_time": trade.get("exit_time"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "quantity_value": quantity_value,
            "quantity_label": f"{quantity_value:.4f}",
            "pnl_value": pnl_value,
            "pnl_label": f"{pnl_value:.2f}",
            "notes": list(trade.get("notes") or []),
        }

    @staticmethod
    def _normalize_score_breakdown(breakdown: dict[str, Any]) -> list[dict[str, Any]]:
        if not breakdown:
            return []
        labels = {
            "profitability_score": "Profitability",
            "win_rate_score": "Win Rate",
            "profit_factor_score": "Profit Factor",
            "drawdown_control_score": "Drawdown Control",
            "fill_rate_score": "Fill Rate",
            "consistency_score": "Consistency",
            "sample_confidence_score": "Sample Confidence",
        }
        ordered = [
            "profitability_score",
            "win_rate_score",
            "profit_factor_score",
            "drawdown_control_score",
            "fill_rate_score",
            "consistency_score",
            "sample_confidence_score",
        ]
        rows: list[dict[str, Any]] = []
        for key in ordered:
            value = DashboardService._decimal_to_float(breakdown.get(key) or "0")
            rows.append(
                {
                    "key": key,
                    "label": labels[key],
                    "value": value,
                    "label_value": f"{value:.2f}",
                }
            )
        return rows

    @staticmethod
    def _decimal_to_float(value: Any) -> float:
        try:
            return float(Decimal(str(value)))
        except Exception:
            return 0.0
