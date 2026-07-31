"""Dashboard service layer."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from sqlalchemy.orm import Session, sessionmaker

from triak_trade.backtesting.backtest_analytics import (
    BacktestAnalytics,
    BacktestAnalyticsFilters,
    BacktestAnalyticsRun,
)
from triak_trade.backtesting.backtest_runner import (
    BacktestRunner,
    BacktestRunRequest,
    default_backtest_parallel_workers,
)
from triak_trade.backtesting.strategies.registry import (
    build_strategy_from_key,
    list_available_strategies,
)
from triak_trade.config.settings import Settings
from triak_trade.core.time import TEHRAN_TZ, parse_user_datetime_to_utc
from triak_trade.dashboard.backtest_runtime import (
    DashboardBacktestCoordinator,
    normalize_channel_reference,
    parse_telegram_message_link,
)
from triak_trade.dashboard.env_config import RootEnvConfigEditor
from triak_trade.dashboard.saved_channels import SavedChannelStore
from triak_trade.dashboard.schemas import (
    AIKeywordFilterConfig,
    AutoModeState,
    BacktestLifecycleConfig,
    KillSwitchState,
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
        self.saved_channel_store = SavedChannelStore(settings)
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
        return self.saved_channel_store.get()

    def add_saved_channel(self, channel_input: str) -> SavedChannelsState:
        return self.saved_channel_store.add(channel_input)

    def remove_saved_channel(self, channel_reference: str) -> SavedChannelsState:
        return self.saved_channel_store.remove(channel_reference)

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
        live_session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings
        self.state = DashboardStateService(settings)
        self.backtest_runner = BacktestRunner(settings=settings)
        self.live_session_factory = live_session_factory
        self.backtests = DashboardBacktestCoordinator(
            settings=settings,
            runner_factory=lambda: BacktestRunner(settings=settings),
            notifier=realtime_notifier,
        )
        self.backtest_analytics = BacktestAnalytics()

    def overview(self) -> dict[str, Any]:
        log_status = TelegramLogChannelClient(settings=self.settings).safe_status()
        latest_report = self.backtest_runner.report_store.latest()
        auto_mode = self.state.get_auto_mode()
        kill_switch = self.state.get_kill_switch()
        latest_backtest = self.backtests.latest_run_summary(run_type="backtest")
        active_backtests = self.backtests.list_run_summaries(
            limit=20,
            run_type="backtest",
            statuses={"queued", "running", "cancelling"},
        )
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
            "active_backtests": [
                run.model_dump(mode="json") for run in active_backtests
            ],
            "latest_backtest": (
                latest_backtest.model_dump(mode="json") if latest_backtest else None
            ),
            "recent_audit_events": [],
        }

    def backtest_bootstrap(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        readiness = self.backtest_readiness()
        recent_runs = [
            run.model_dump(mode="json")
            for run in self.backtests.list_run_summaries(
                limit=8,
                offset=0,
                run_type="backtest",
            )
        ]
        latest_run = self.backtests.latest_run_summary(
            run_type="backtest",
            prefer_active=True,
        )
        saved_channels = self.state.get_saved_channels()
        strategies = [
            StrategyCatalogEntry.model_validate(item).model_dump(mode="json")
            for item in list_available_strategies()
        ]
        strategy_keys = {str(item["key"]) for item in strategies}
        default_strategy_key = (
            self.settings.LIVE_TRADING_DEFAULT_STRATEGY_KEY
            if self.settings.LIVE_TRADING_DEFAULT_STRATEGY_KEY in strategy_keys
            else next(
                (str(item["key"]) for item in strategies if item.get("active")),
                "default_risk_managed",
            )
        )
        default_from_date = (now - timedelta(hours=24)).isoformat()
        default_to_date = now.isoformat()
        return {
            "default_channel": self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            "default_start_message_link": "",
            "default_interval": self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
            "default_lifecycle_refresh_interval": self.settings.BACKTEST_LIFECYCLE_REFRESH_INTERVAL,
            "default_max_messages": self.settings.REAL_BACKTEST_MAX_MESSAGES,
            "default_run_type": "backtest",
            "default_initial_balance": str(self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE),
            "default_risk_per_trade_pct": str(
                self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT
            ),
            "default_fill_policy": self.settings.BACKTEST_DEFAULT_FILL_POLICY,
            "default_capital_per_signal": str(
                self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE
            ),
            "default_fee_rate_pct": str(self.settings.LIVE_TRADING_FEE_RATE_PCT),
            "default_max_effective_leverage": str(
                self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE
            ),
            "default_signal_leverage": str(
                self.settings.LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE
            ),
            "default_min_allocation_pct": str(
                self.settings.LIVE_TRADING_MIN_ALLOCATION_PCT
            ),
            "default_max_allocation_pct": str(
                self.settings.LIVE_TRADING_MAX_ALLOCATION_PCT
            ),
            "default_stop_pct": str(self.settings.LIVE_TRADING_DEFAULT_STOP_PCT),
            "default_synthetic_stop_max_loss_pct": str(
                self.settings.LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT
            ),
            "default_max_stop_loss_pct": str(
                self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE
            ),
            "default_consolidation_seconds": self.settings.SIGNAL_CONSOLIDATION_SECONDS,
            "default_leverage_source": "signal_or_default",
            "default_fixed_leverage": str(
                self.settings.LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE
            ),
            "default_max_parallel_signals": default_backtest_parallel_workers(),
            "default_include_not_filled_signals": True,
            "default_close_open_positions_at_end": False,
            "default_use_ai": (
                self.settings.REAL_BACKTEST_USE_AI
                and self.settings.AI_GATEWAY_ENABLED
                and self.settings.AI_CLASSIFIER_ENABLED
            ),
            "default_send_log_channel": False,
            "default_log_per_message": True,
            "default_strategy_key": default_strategy_key,
            "available_strategies": strategies,
            "default_from_date": default_from_date,
            "default_to_date": default_to_date,
            "default_from_input": self._format_datetime_local_input(default_from_date),
            "default_to_input": self._format_datetime_local_input(default_to_date),
            "readiness": readiness,
            "recent_runs": recent_runs,
            "active_run_id": latest_run.run_id if latest_run else None,
            "active_run_status": latest_run.status if latest_run else None,
            "saved_channels": [item.model_dump(mode="json") for item in saved_channels.channels],
        }

    @staticmethod
    def _format_datetime_local_input(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            localized = parsed.replace(tzinfo=TEHRAN_TZ)
        else:
            localized = parsed.astimezone(TEHRAN_TZ)
        return localized.strftime("%Y-%m-%dT%H:%M")

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
        readiness = self.backtest_readiness()
        if not readiness.get("ready", False):
            return {
                "started": False,
                "blocked": True,
                "reason": "Backtest is not ready.",
                "issues": list(readiness.get("issues", [])),
                "readiness": readiness,
            }

        from_date = self._parse_datetime(payload.get("from_date"))
        to_date = self._parse_datetime(payload.get("to_date"))
        if from_date is None or to_date is None:
            raise ValueError("from_date and to_date are required")

        common_request: dict[str, Any] = {
            "channel": channel_resolved,
            "from_date": from_date,
            "to_date": to_date,
            "hours": None,
            "start_message_link": normalized_start_message_link,
            "start_message_id": start_message_id,
            "interval": str(
                payload.get("interval") or self.settings.REAL_BACKTEST_DEFAULT_INTERVAL
            ),
            "max_messages": int(
                payload.get("max_messages") or self.settings.REAL_BACKTEST_MAX_MESSAGES
            ),
            "initial_balance": Decimal(
                str(
                    payload.get("initial_balance")
                    or self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE
                )
            ),
            "risk_per_trade_pct": Decimal(
                str(
                    payload.get("risk_per_trade_pct")
                    or self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT
                )
            ),
            "use_ai": bool(payload.get("use_ai", self.settings.REAL_BACKTEST_USE_AI)),
            "send_telegram_summary": False,
            "send_log_channel": bool(
                payload.get("send_log_channel", self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL)
            ),
            "log_per_message": bool(payload.get("log_per_message", True)),
        }
        leverage_source = cast(
            Literal["signal_or_default", "fixed"],
            str(payload.get("leverage_source") or "signal_or_default"),
        )
        request = BacktestRunRequest(
            **common_request,
            capital_per_signal=Decimal(
                str(
                    payload.get("capital_per_signal")
                    or self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE
                )
            ),
            fill_policy=BacktestFillPolicy(
                str(payload.get("fill_policy") or self.settings.BACKTEST_DEFAULT_FILL_POLICY)
            ),
            leverage_source=leverage_source,
            fixed_leverage=(
                int(payload["fixed_leverage"])
                if str(payload.get("fixed_leverage") or "").strip()
                else None
            ),
            max_effective_leverage=Decimal(
                str(
                    payload.get("max_effective_leverage")
                    or self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE
                )
            ),
            default_signal_leverage=Decimal(
                str(
                    payload.get("default_signal_leverage")
                    or self.settings.LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE
                )
            ),
            min_allocation_pct=Decimal(
                str(
                    payload.get("min_allocation_pct")
                    or self.settings.LIVE_TRADING_MIN_ALLOCATION_PCT
                )
            ),
            max_allocation_pct=Decimal(
                str(
                    payload.get("max_allocation_pct")
                    or self.settings.LIVE_TRADING_MAX_ALLOCATION_PCT
                )
            ),
            default_stop_pct=Decimal(
                str(
                    payload.get("default_stop_pct")
                    or self.settings.LIVE_TRADING_DEFAULT_STOP_PCT
                )
            ),
            synthetic_stop_max_loss_pct_of_balance=Decimal(
                str(
                    payload.get("synthetic_stop_max_loss_pct")
                    or self.settings.LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT
                )
            ),
            max_stop_loss_pct_of_balance=Decimal(
                str(
                    payload.get("max_stop_loss_pct")
                    or self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE
                )
            ),
            fee_rate_pct=Decimal(
                str(payload.get("fee_rate_pct") or self.settings.LIVE_TRADING_FEE_RATE_PCT)
            ),
            consolidation_seconds=int(
                payload.get("consolidation_seconds")
                or self.settings.SIGNAL_CONSOLIDATION_SECONDS
            ),
            close_open_positions_at_end=bool(
                payload.get("close_open_positions_at_end", False)
            ),
            lifecycle_refresh_interval=str(
                payload.get("lifecycle_refresh_interval")
                or self.settings.BACKTEST_LIFECYCLE_REFRESH_INTERVAL
            ),
            max_parallel_signals=int(
                payload.get("max_parallel_signals") or default_backtest_parallel_workers()
            ),
            include_not_filled_signals=bool(
                payload.get("include_not_filled_signals", True)
            ),
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
        payload = run.model_dump(mode="json", exclude={"messages"})
        payload["message_count"] = len(run.messages)
        payload["messages_loaded"] = False
        return payload

    def get_backtest_run_messages(
        self,
        run_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int] | None:
        run = self.backtests.get_run(run_id)
        if run is None:
            return None
        messages = self.backtests.get_run_messages(run_id, limit=limit, offset=offset)
        if messages is None:
            return None
        return [message.model_dump(mode="json") for message in messages], len(run.messages)

    def list_backtest_runs(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            run.model_dump(mode="json")
            for run in self.backtests.list_run_summaries(
                limit=limit,
                offset=offset,
                run_type="backtest",
            )
        ]

    def count_backtest_runs(self) -> int:
        return self.backtests.count_runs(run_type="backtest")

    def latest_backtest_run(
        self,
        *,
        prefer_active: bool = True,
    ) -> dict[str, Any] | None:
        run = self.backtests.latest_run_summary(
            run_type="backtest",
            prefer_active=prefer_active,
        )
        return run.model_dump(mode="json") if run else None

    def backtest_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = BacktestAnalyticsFilters.model_validate(
            {
                "channel": self._optional_text(payload.get("channel")),
                "strategy": self._optional_text(payload.get("strategy")),
                "fill_policy": self._optional_text(payload.get("fill_policy")),
                "interval": self._optional_text(payload.get("interval")),
                "status": self._optional_text(payload.get("status")),
                "date_from": self._parse_datetime(payload.get("date_from")),
                "date_to": self._parse_datetime(payload.get("date_to")),
                "min_signals": max(int(payload.get("min_signals") or 0), 0),
                "sort_by": str(payload.get("sort_by") or "score"),
                "sort_order": str(payload.get("sort_order") or "desc"),
            }
        )
        summaries = self.backtests.list_run_summaries(
            limit=max(self.backtests.count_runs(run_type="backtest"), 1),
            run_type="backtest",
        )
        analytics_runs: list[BacktestAnalyticsRun] = []
        seen_report_paths: set[str] = set()
        for summary in summaries:
            run = self.backtests.get_run(summary.run_id)
            if run is None:
                continue
            analytics_run = BacktestAnalyticsRun.model_validate(
                run.model_dump(mode="python") | {"source": "dashboard"}
            )
            analytics_runs.append(analytics_run)
            if analytics_run.report_path:
                seen_report_paths.add(self._normalized_report_path(analytics_run.report_path))
        report_runs, skipped_reports = self._load_backtest_report_runs(
            seen_report_paths=seen_report_paths
        )
        analytics_runs.extend(report_runs)
        result = self.backtest_analytics.analyze(analytics_runs, filters=filters)
        result["data_sources"] = {
            "dashboard_runs": sum(1 for run in analytics_runs if run.source == "dashboard"),
            "report_only_runs": len(report_runs),
            "dashboard_report_references": len(seen_report_paths),
            "skipped_invalid_reports": skipped_reports,
        }
        return result

    def _load_backtest_report_runs(
        self,
        *,
        seen_report_paths: set[str],
    ) -> tuple[list[BacktestAnalyticsRun], int]:
        runs: list[BacktestAnalyticsRun] = []
        skipped = 0
        for path in self.backtest_runner.report_store.list_reports():
            normalized_path = self._normalized_report_path(str(path))
            if normalized_path in seen_report_paths:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("run_type") != "backtest":
                    continue
                request_payload = payload.get("request")
                aggregate = payload.get("aggregate")
                signals = payload.get("signals")
                if not isinstance(request_payload, dict):
                    request_payload = {}
                if not isinstance(aggregate, dict):
                    aggregate = {}
                if not isinstance(signals, list):
                    signals = []
                report_id = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:12]
                runs.append(
                    BacktestAnalyticsRun.model_validate(
                        {
                            "run_id": f"report_{report_id}",
                            "run_type": "backtest",
                            "channel_resolved": payload.get("channel"),
                            "from_date": payload.get("from_date"),
                            "to_date": payload.get("to_date"),
                            "interval": payload.get("interval"),
                            "strategy_key": payload.get("strategy_key")
                            or "default_risk_managed",
                            "status": "completed" if payload.get("success") else "failed",
                            "created_at": payload.get("generated_at")
                            or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                            "finished_at": payload.get("generated_at"),
                            "use_ai": bool(payload.get("ai_used")),
                            "request_payload": request_payload,
                            "backtest_aggregate": aggregate,
                            "signals": signals,
                            "errors": payload.get("errors") or [],
                            "warnings": payload.get("warnings") or [],
                            "source": "report",
                            "report_path": str(path),
                        }
                    )
                )
            except (OSError, ValueError, TypeError):
                skipped += 1
        return runs, skipped

    @staticmethod
    def _normalized_report_path(value: str) -> str:
        return str(Path(value).resolve())

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

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

    def resume_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.backtests.resume_run(run_id)
        if run is None:
            return None
        return {"started": True, "resumed_from": run_id, "run": run.model_dump(mode="json")}

    def list_saved_channels(self) -> list[dict[str, Any]]:
        state = self.state.get_saved_channels()
        return [item.model_dump(mode="json") for item in state.channels]

    def save_backtest_channel(self, channel_input: str) -> list[dict[str, Any]]:
        state = self.state.add_saved_channel(channel_input)
        return [item.model_dump(mode="json") for item in state.channels]

    def remove_backtest_channel(self, channel_reference: str) -> list[dict[str, Any]]:
        state = self.state.remove_saved_channel(channel_reference)
        return [item.model_dump(mode="json") for item in state.channels]


    def backtest_readiness(self) -> dict[str, Any]:
        return self.backtest_runner.readiness().model_dump(mode="json")

    def logs(self) -> dict[str, Any]:
        raw_lines = self._tail_dashboard_logs(lines=500)
        parsed = self._parse_log_entries(raw_lines)
        stats = self._log_stats(parsed)
        return {
            "log_channel": TelegramLogChannelClient(settings=self.settings).safe_status(),
            "runtime_logs": raw_lines[-100:],
            "parsed_log_entries": parsed[-200:],
            "log_stats": stats,
            "log_storage": self._dashboard_log_storage(),
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

        store = LiveTradingStore(
            self.settings.LIVE_TRADING_RUNTIME_DIR,
            session_factory=self.live_session_factory,
        )
        sessions = store.list_sessions(limit=200)

        channel_map: dict[str, dict[str, Any]] = {}
        for session in sessions:
            session_data = session.model_dump(mode="json")
            trades = store.list_trades(session.session_id, limit=500)
            total_trades = len(trades)
            closed_trades = [trade for trade in trades if not trade.is_open]
            open_trades = [trade for trade in trades if trade.is_open]
            channel_labels = session.channel_labels or [
                ch.rsplit("/", 1)[-1] if "/" in ch else ch
                for ch in session.channels
            ]
            session_total_pnl = session.total_pnl
            session_realized_pnl = session.total_realized_pnl
            session_unrealized_pnl = session.total_unrealized_pnl
            win_rate_pct = (
                Decimal(session.wins) / Decimal(len(closed_trades)) * Decimal("100")
                if closed_trades
                else Decimal("0")
            )
            avg_trade_pnl = (
                session_total_pnl / Decimal(total_trades)
                if total_trades
                else Decimal("0")
            )
            session_runtime_seconds = _duration_seconds(session.started_at, session.stopped_at)
            session_row = {
                **session_data,
                "total_trades": total_trades,
                "closed_trades_count": len(closed_trades),
                "open_positions_count": len(open_trades),
                "total_pnl": str(session_total_pnl),
                "total_pnl_label": _signed_decimal_label(session_total_pnl),
                "pnl_positive": session_total_pnl > 0,
                "pnl_negative": session_total_pnl < 0,
                "total_realized_pnl": str(session_realized_pnl),
                "total_unrealized_pnl": str(session_unrealized_pnl),
                "total_fees": str(session.total_fees),
                "win_rate_pct": _decimal_to_str(win_rate_pct),
                "avg_trade_pnl": _decimal_to_str(avg_trade_pnl),
                "runtime_seconds": session_runtime_seconds,
                "runtime_label": _format_elapsed_seconds(session_runtime_seconds),
            }
            for index, channel in enumerate(session.channels):
                if channel not in channel_map:
                    channel_map[channel] = {
                        "channel": channel,
                        "channel_label": (
                            channel_labels[index]
                            if index < len(channel_labels)
                            else channel
                        ),
                        "sessions": [],
                        "session_count": 0,
                        "total_trades": 0,
                        "closed_trades": 0,
                        "open_trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "total_pnl": Decimal("0"),
                        "realized_pnl": Decimal("0"),
                        "unrealized_pnl": Decimal("0"),
                        "fees": Decimal("0"),
                        "total_messages": 0,
                        "total_signals_received": 0,
                        "total_signals_opened": 0,
                        "strategies": set(),
                        "modes": set(),
                        "last_session_at": None,
                        "daily_pnl": defaultdict(lambda: Decimal("0")),
                        "weekly_pnl": defaultdict(lambda: Decimal("0")),
                        "monthly_pnl": defaultdict(lambda: Decimal("0")),
                    }
                entry = channel_map[channel]
                entry["sessions"].append(session_row)
                entry["session_count"] += 1
                entry["total_trades"] += total_trades
                entry["closed_trades"] += len(closed_trades)
                entry["open_trades"] += len(open_trades)
                entry["wins"] += session.wins
                entry["losses"] += session.losses
                entry["total_pnl"] += session_total_pnl
                entry["realized_pnl"] += session_realized_pnl
                entry["unrealized_pnl"] += session_unrealized_pnl
                entry["fees"] += session.total_fees
                entry["total_messages"] += session.total_messages_processed
                entry["total_signals_received"] += session.total_signals_received
                entry["total_signals_opened"] += session.total_signals_opened
                entry["strategies"].add(session.strategy_key)
                entry["modes"].add(session.trading_mode)
                started = session.started_at.isoformat()
                if entry["last_session_at"] is None or started > entry["last_session_at"]:
                    entry["last_session_at"] = started
                _add_live_trade_period_buckets(entry, closed_trades)

        channel_reports = []
        for ch_data in channel_map.values():
            total_trades = ch_data["total_trades"]
            closed_trades = ch_data["closed_trades"]
            total_pnl = ch_data["total_pnl"]
            win_rate_pct = (
                Decimal(ch_data["wins"]) / Decimal(closed_trades) * Decimal("100")
                if closed_trades
                else Decimal("0")
            )
            pnl_per_trade = (
                total_pnl / Decimal(total_trades)
                if total_trades
                else Decimal("0")
            )
            signal_open_rate = (
                Decimal(ch_data["total_signals_opened"])
                / Decimal(ch_data["total_signals_received"])
                * Decimal("100")
                if ch_data["total_signals_received"]
                else Decimal("0")
            )
            channel_reports.append(
                {
                    "channel": ch_data["channel"],
                    "channel_label": ch_data["channel_label"],
                    "sessions": ch_data["sessions"][:10],
                    "session_count": ch_data["session_count"],
                    "total_trades": total_trades,
                    "closed_trades": closed_trades,
                    "open_trades": ch_data["open_trades"],
                    "wins": ch_data["wins"],
                    "losses": ch_data["losses"],
                    "total_messages": ch_data["total_messages"],
                    "total_signals_received": ch_data["total_signals_received"],
                    "total_signals_opened": ch_data["total_signals_opened"],
                    "signal_open_rate_label": f"{signal_open_rate:.1f}%",
                    "total_pnl": str(total_pnl),
                    "total_pnl_label": _signed_decimal_label(total_pnl),
                    "realized_pnl_label": _signed_decimal_label(ch_data["realized_pnl"]),
                    "unrealized_pnl_label": _signed_decimal_label(ch_data["unrealized_pnl"]),
                    "fees_label": _signed_decimal_label(ch_data["fees"]),
                    "pnl_positive": total_pnl > 0,
                    "pnl_negative": total_pnl < 0,
                    "win_rate_pct": f"{win_rate_pct:.1f}",
                    "win_rate_label": f"{win_rate_pct:.1f}%",
                    "avg_trade_pnl_label": _signed_decimal_label(pnl_per_trade),
                    "strategies": sorted(ch_data["strategies"]),
                    "modes": sorted(ch_data["modes"]),
                    "last_session_at": ch_data["last_session_at"],
                    "daily_pnl": _sorted_live_period_rows(ch_data["daily_pnl"]),
                    "weekly_pnl": _sorted_live_period_rows(ch_data["weekly_pnl"]),
                    "monthly_pnl": _sorted_live_period_rows(ch_data["monthly_pnl"]),
                }
            )

        channel_reports.sort(key=lambda x: x["last_session_at"] or "", reverse=True)

        total_trades = sum(r["total_trades"] for r in channel_reports)
        total_wins = sum(r["wins"] for r in channel_reports)
        total_pnl = sum(
            (Decimal(str(report["total_pnl"])) for report in channel_reports),
            start=Decimal("0"),
        )
        overall_win_rate = (
            Decimal(total_wins) / Decimal(total_trades) * Decimal("100")
            if total_trades > 0
            else Decimal("0")
        )

        return {
            "channel_reports": channel_reports,
            "summary": {
                "total_channels": len(channel_reports),
                "total_sessions": len(sessions),
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_pnl": str(total_pnl),
                "total_pnl_label": _signed_decimal_label(total_pnl),
                "pnl_positive": total_pnl > 0,
                "pnl_negative": total_pnl < 0,
                "overall_win_rate_pct": f"{overall_win_rate:.1f}",
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

    def _dashboard_log_storage(self) -> dict[str, Any]:
        path = Path(self.settings.DASHBOARD_LOG_FILE)
        rotated = (
            sorted(path.parent.glob(f"{path.name}.*")) if path.parent.exists() else []
        )
        files = ([path] if path.exists() else []) + [item for item in rotated if item.is_file()]
        total_bytes = sum(item.stat().st_size for item in files)
        mebibyte = Decimal(1024 * 1024)
        return {
            "path": str(path),
            "current_bytes": path.stat().st_size if path.exists() else 0,
            "total_bytes": total_bytes,
            "total_megabytes": f"{Decimal(total_bytes) / mebibyte:.2f}",
            "rotated_files": len(rotated),
            "max_file_megabytes": (
                f"{Decimal(self.settings.DASHBOARD_LOG_MAX_BYTES) / mebibyte:.2f}"
            ),
            "backup_count": self.settings.DASHBOARD_LOG_BACKUP_COUNT,
            "file_logging_enabled": self.settings.DASHBOARD_FILE_LOG_ENABLED,
            "file_log_level": self.settings.DASHBOARD_LOG_LEVEL,
        }

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
                payload = data.get("payload") or {
                    key: value
                    for key, value in data.items()
                    if key
                    not in {
                        "timestamp",
                        "asctime",
                        "level",
                        "levelname",
                        "module",
                        "name",
                        "logger",
                        "event",
                    }
                }
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



def _decimal_to_str(value: Decimal) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") if value != value.to_integral() else str(value)


def _signed_decimal_label(value: Decimal) -> str:
    return f"{value:+.2f}"


def _duration_seconds(started_at: datetime, stopped_at: datetime | None) -> int:
    end_at = stopped_at or datetime.now(timezone.utc)
    return max(0, int((end_at - started_at).total_seconds()))


def _format_elapsed_seconds(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _add_live_trade_period_buckets(entry: dict[str, Any], trades: list[Any]) -> None:
    for trade in trades:
        if trade.closed_at is None:
            continue
        pnl = Decimal(trade.total_pnl)
        closed_at = trade.closed_at.astimezone(timezone.utc)
        entry["daily_pnl"][closed_at.strftime("%Y-%m-%d")] += pnl
        entry["weekly_pnl"][closed_at.strftime("%G-W%V")] += pnl
        entry["monthly_pnl"][closed_at.strftime("%Y-%m")] += pnl


def _sorted_live_period_rows(buckets: dict[str, Decimal]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period, pnl in sorted(buckets.items(), key=lambda item: item[0], reverse=True):
        rows.append(
            {
                "period": period,
                "pnl": str(pnl),
                "pnl_label": _signed_decimal_label(pnl),
                "positive": pnl > 0,
            }
        )
    return rows[:12]
