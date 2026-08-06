"""Live trading dashboard runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from triak_trade.backtesting.strategies.registry import (
    build_strategy_from_key,
    list_available_strategies,
)
from triak_trade.config.settings import Settings
from triak_trade.core.logging import log_event
from triak_trade.dashboard.saved_channels import SavedChannelStore
from triak_trade.dashboard.schemas import StrategyCatalogEntry
from triak_trade.exchange.toobit.futures import build_futures_client_from_settings
from triak_trade.live_trading.account_coordinator import AccountExecutionCoordinator
from triak_trade.live_trading.engine import (
    LiveTradingEngine,
    build_engine_from_config,
    build_engine_from_session,
)
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSessionConfig,
    LiveSessionDetail,
    LiveTrade,
    LiveTradingSnapshot,
)
from triak_trade.live_trading.store import LiveTradingStore
from triak_trade.telegram.shared_client import SharedTelethonTelegramClient

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LiveTradingReadiness(BaseModel):
    ready: bool
    live_trading_enabled: bool
    telegram_configured: bool
    toobit_configured: bool
    ai_ready: bool
    issues: list[str] = Field(default_factory=list)


class DashboardLiveOverview(BaseModel):
    generated_at: datetime = Field(default_factory=_utc_now)
    is_running: bool = False
    active_sessions: list[LiveSession] = Field(default_factory=list)
    recent_sessions: list[LiveSession] = Field(default_factory=list)
    open_trades: list[LiveTrade] = Field(default_factory=list)
    recent_closed_trades: list[LiveTrade] = Field(default_factory=list)
    recent_messages: list[LiveMessageTrace] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)


class DashboardLiveCoordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: LiveTradingStore | None = None,
        session_factory: sessionmaker[Session] | None = None,
        notifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or LiveTradingStore(
            settings.LIVE_TRADING_RUNTIME_DIR,
            session_factory=session_factory,
        )
        self.notifier = notifier
        self.runtime_root = Path(settings.LIVE_TRADING_RUNTIME_DIR)
        self.state_dir = self.runtime_root / "state"
        self.saved_channel_store = SavedChannelStore(settings)
        self.telegram_client = SharedTelethonTelegramClient(settings)
        self.account_coordinator = AccountExecutionCoordinator.from_settings(settings)
        self.account_coordinator.restore_from_store(self.store)
        self._lock = threading.Lock()
        self._engines: dict[str, LiveTradingEngine] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._force_stopped_session_ids: set[str] = set()
        self._flag_unresolved_inactive_sessions()
        self._recover_incomplete_sessions()

    def _log_event(self, level: int, event: str, /, **fields: Any) -> None:
        log_event(log, level, event, **fields)

    def live_mode_enabled(self) -> bool:
        return bool(getattr(self.settings, "LIVE_TRADING_LIVE_MODE_ENABLED", False))

    def readiness(self) -> LiveTradingReadiness:
        issues: list[str] = []
        live_trading_enabled = bool(self.settings.LIVE_TRADING_ENABLED)
        if not live_trading_enabled:
            issues.append("Live trading is disabled by configuration.")

        telegram_configured = (
            bool(getattr(self.settings, "TELEGRAM_API_ID", 0))
            and self._secret_present(getattr(self.settings, "TELEGRAM_API_HASH", None))
            and self._secret_present(getattr(self.settings, "TELEGRAM_STRING_SESSION", None))
        )
        if not telegram_configured:
            issues.append("Telegram credentials are not fully configured.")

        toobit_configured = (
            self._secret_present(getattr(self.settings, "TOOBIT_API_KEY", None))
            and self._secret_present(getattr(self.settings, "TOOBIT_API_SECRET", None))
        )
        if not toobit_configured:
            issues.append("Toobit credentials are not fully configured.")

        ai_ready = bool(
            self.settings.AI_GATEWAY_ENABLED and self.settings.AI_CLASSIFIER_ENABLED
        )
        if self.settings.LIVE_TRADING_REQUIRE_AI_CLASSIFIER and not ai_ready:
            issues.append("AI classifier is required before enabling live capital execution.")

        readiness = LiveTradingReadiness(
            ready=(
                live_trading_enabled
                and telegram_configured
                and toobit_configured
                and (ai_ready or not self.settings.LIVE_TRADING_REQUIRE_AI_CLASSIFIER)
            ),
            live_trading_enabled=live_trading_enabled,
            telegram_configured=telegram_configured,
            toobit_configured=toobit_configured,
            ai_ready=ai_ready,
            issues=issues,
        )
        self._log_event(
            logging.INFO,
            "dashboard.live_readiness_evaluated",
            ready=readiness.ready,
            issue_count=len(readiness.issues),
            issues=readiness.issues,
            live_trading_enabled=readiness.live_trading_enabled,
            telegram_configured=readiness.telegram_configured,
            toobit_configured=readiness.toobit_configured,
            ai_ready=readiness.ai_ready,
        )
        return readiness

    def bootstrap(self) -> dict[str, Any]:
        strategies = [
            StrategyCatalogEntry.model_validate(item).model_dump(mode="json")
            for item in list_available_strategies()
        ]
        default_strategy_key = next(
            (item["key"] for item in strategies if item.get("active")),
            self.settings.LIVE_TRADING_DEFAULT_STRATEGY_KEY,
        )
        active_sessions = self.list_active_sessions(limit=20)
        current_session = active_sessions[0] if active_sessions else self.get_current_session()
        default_trading_mode = self.settings.LIVE_TRADING_MODE
        if default_trading_mode == "live" and not self.live_mode_enabled():
            default_trading_mode = "demo"
        saved_channels = [
            item.model_dump(mode="json")
            for item in self.saved_channel_store.get().channels
        ]
        payload = {
            "readiness": self.readiness().model_dump(mode="json"),
            "is_running": self.is_running(),
            "current_session": current_session.model_dump(mode="json") if current_session else None,
            "active_sessions": [item.model_dump(mode="json") for item in active_sessions],
            "default_trading_mode": default_trading_mode,
            "live_mode_enabled": self.live_mode_enabled(),
            "default_risk_per_trade_pct": str(
                self.settings.LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT
            ),
            "default_strategy_key": default_strategy_key,
            "use_ai_default": (
                self.settings.LIVE_TRADING_USE_AI
                and self.settings.AI_GATEWAY_ENABLED
                and self.settings.AI_CLASSIFIER_ENABLED
            ),
            "default_channels": list(self.settings.LIVE_TRADING_DEFAULT_CHANNELS),
            "saved_channels": saved_channels,
            "available_strategies": strategies,
        }
        self._log_event(
            logging.INFO,
            "dashboard.live_bootstrap_built",
            strategy_count=len(strategies),
            active_session_count=len(active_sessions),
            saved_channel_count=len(saved_channels),
        )
        return payload

    def start_session(self, config: LiveSessionConfig) -> LiveSession:
        self._log_event(
            logging.INFO,
            "dashboard.live_start_requested",
            channels=config.channels,
            trading_mode=config.trading_mode,
            risk_per_trade_pct=str(config.risk_per_trade_pct),
            strategy_key=config.strategy_key,
            use_ai=config.use_ai,
        )
        readiness = self.readiness()
        if not readiness.live_trading_enabled:
            raise ValueError("Live trading feature is disabled by configuration.")
        if not readiness.telegram_configured:
            raise ValueError("Telegram credentials are not fully configured.")
        if not readiness.toobit_configured:
            raise ValueError("Toobit credentials are not fully configured.")
        if self.settings.KILL_SWITCH_ENABLED:
            raise ValueError(
                "Live trading start blocked because Kill Switch is active: "
                + (self.settings.KILL_SWITCH_REASON or "no reason provided")
            )
        if config.trading_mode == "live" and not self.live_mode_enabled():
            raise ValueError(
                "Live mode requires LIVE_TRADING_LIVE_MODE_ENABLED=true in root .env.local"
            )
        ai_required = (
            config.trading_mode in {"demo", "live"}
            or self.settings.LIVE_TRADING_REQUIRE_AI_CLASSIFIER
        )
        if ai_required and not readiness.ai_ready:
            raise ValueError(
                "Live trading requires AI gateway and AI classifier to be enabled."
            )
        if ai_required and not config.use_ai:
            raise ValueError("Live trading requires use_ai=true.")
        if config.risk_per_trade_pct > self.settings.LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT:
            raise ValueError(
                "risk_per_trade_pct exceeds LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT="
                f"{self.settings.LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT}"
            )
        if not config.strategy_key.strip():
            raise ValueError("strategy_key is required")
        build_strategy_from_key(config.strategy_key)
        session, engine = build_engine_from_config(
            config=config,
            settings=self.settings,
            store=self.store,
            notifier=self.notifier,
            telegram_client=self.telegram_client,
            account_coordinator=self.account_coordinator,
        )
        self.store.save_session(session)
        worker = threading.Thread(
            target=self._run_engine,
            args=(session.session_id, engine),
            name=f"live-session-{session.session_id}",
            daemon=True,
        )
        with self._lock:
            self._engines[session.session_id] = engine
            self._threads[session.session_id] = worker
        worker.start()
        self._log_event(
            logging.INFO,
            "dashboard.live_session_started",
            session_id=session.session_id,
            channels=session.channels,
            trading_mode=session.trading_mode,
        )
        return session

    def stop_session(
        self,
        session_id: str | None = None,
        *,
        force: bool = False,
    ) -> LiveSession | None:
        target_session_id = session_id or self._current_session_id()
        if not target_session_id:
            self._log_event(logging.INFO, "dashboard.live_stop_skipped", reason="no_target_session")
            return None
        engine: LiveTradingEngine | None = None
        open_trades = self.store.list_open_trades(target_session_id)
        if open_trades and not force:
            symbols = sorted({trade.symbol for trade in open_trades})
            self._log_event(
                logging.WARNING,
                "dashboard.live_stop_blocked_open_trades",
                session_id=target_session_id,
                open_trade_count=len(open_trades),
                symbols=symbols,
            )
            raise ValueError(
                "Session stop is blocked while open or pending trades exist: "
                + ", ".join(symbols)
            )
        if open_trades:
            self._log_event(
                logging.WARNING,
                "dashboard.live_stop_forced_with_open_trades",
                session_id=target_session_id,
                open_trade_count=len(open_trades),
                symbols=sorted({trade.symbol for trade in open_trades}),
                exchange_positions_left_unmanaged=True,
            )
        with self._lock:
            engine = self._engines.get(target_session_id)
        if engine is not None:
            if force:
                with self._lock:
                    self._force_stopped_session_ids.add(target_session_id)
                warning = (
                    "Session was force-stopped with open trades; exchange positions and "
                    "existing orders were left unmanaged by Triak Trade."
                )
                engine.session.mark_stopped()
                engine.session.health_status = "critical"
                engine.session.new_entries_blocked = True
                engine.session.last_error = warning
                if warning not in engine.session.health_issues:
                    engine.session.health_issues.append(warning)
                if warning not in engine.session.errors:
                    engine.session.errors.append(warning)
                self.store.save_session(engine.session)
                self._notify_session(engine.session)
            engine.stop()
            self._log_event(
                logging.INFO,
                "dashboard.live_stop_requested",
                session_id=target_session_id,
                source="running_engine",
            )
            return engine.session
        session = self.store.load_session(target_session_id)
        if session is None:
            self._log_event(
                logging.INFO,
                "dashboard.live_stop_skipped",
                session_id=target_session_id,
                reason="session_not_found",
            )
            return None
        if session.status not in {"running", "starting"}:
            self._log_event(
                logging.INFO,
                "dashboard.live_stop_skipped",
                session_id=target_session_id,
                reason="session_not_running",
                status=session.status,
            )
            return session
        session.mark_stopped(error="Session worker was not running and has been stopped.")
        self.store.save_session(session)
        self._notify_session(session)
        self._log_event(
            logging.WARNING,
            "dashboard.live_session_forced_stopped",
            session_id=session.session_id,
            status=session.status,
        )
        return session

    def is_running(self) -> bool:
        with self._lock:
            return any(thread.is_alive() for thread in self._threads.values())

    def get_current_session(self) -> LiveSession | None:
        active = self.list_active_sessions(limit=1)
        if active:
            return active[0]
        sessions = self.store.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def get_snapshot(self, session_id: str | None = None) -> LiveTradingSnapshot | None:
        target_session_id = session_id or self._current_session_id()
        if not target_session_id:
            return None
        with self._lock:
            engine = self._engines.get(target_session_id)
        if engine is not None:
            return engine.get_snapshot()
        session = self.store.load_session(target_session_id)
        if session is None:
            return None
        return LiveTradingSnapshot(
            session=session,
            open_trades=self.store.list_open_trades(target_session_id),
            recent_closed_trades=self.store.list_closed_trades(target_session_id, limit=30),
            account_info=session.account_info,
        )

    async def refresh_exchange_state(self, session_id: str) -> bool:
        """Run one Toobit state refresh on the owning live-engine event loop."""

        with self._lock:
            engine = self._engines.get(session_id)
        if engine is None:
            return False

        engine_loop = engine._loop
        if engine_loop is None or engine_loop.is_closed():
            return await engine.refresh_exchange_state()
        if engine_loop is asyncio.get_running_loop():
            return await engine.refresh_exchange_state()

        future = asyncio.run_coroutine_threadsafe(
            engine.refresh_exchange_state(),
            engine_loop,
        )
        try:
            refreshed = await asyncio.wrap_future(future)
        except Exception as exc:
            log_event(
                log,
                logging.WARNING,
                "dashboard.live_exchange_refresh_failed",
                session_id=session_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return False
        log_event(
            log,
            logging.INFO,
            "dashboard.live_exchange_refreshed",
            session_id=session_id,
            refreshed=refreshed,
        )
        return refreshed

    async def refresh_active_exchange_states(self) -> None:
        """Refresh all running sessions before rendering aggregate positions."""

        with self._lock:
            session_ids = list(self._engines)
        if session_ids:
            await asyncio.gather(
                *(self.refresh_exchange_state(session_id) for session_id in session_ids)
            )

    def get_overview(
        self,
        *,
        session_limit: int = 20,
        message_limit: int = 80,
        closed_trade_limit: int = 60,
    ) -> DashboardLiveOverview:
        recent_sessions = self.store.list_sessions(limit=session_limit)
        active_sessions = [
            item for item in recent_sessions if item.status in {"starting", "running"}
        ]
        active_session_ids = {item.session_id for item in active_sessions}
        open_trades: list[LiveTrade] = []
        recent_closed_trades: list[LiveTrade] = []
        recent_messages: list[LiveMessageTrace] = []
        for session in recent_sessions:
            # Aggregate Positions is an operational, exchange-synchronised
            # view. A stopped session has no worker to refresh its Toobit
            # state, so its retained records belong to historical session
            # detail rather than the current open-position total.
            if session.session_id in active_session_ids:
                open_trades.extend(self.store.list_open_trades(session.session_id))
            recent_closed_trades.extend(
                self.store.list_closed_trades(session.session_id, limit=12)
            )
            recent_messages.extend(
                self.store.list_message_traces(session.session_id, limit=20)
            )
        open_trades.sort(key=lambda item: item.opened_at, reverse=True)
        recent_closed_trades.sort(
            key=lambda item: item.closed_at or item.updated_at or item.opened_at,
            reverse=True,
        )
        recent_messages.sort(
            key=lambda item: item.received_at or item.message_date,
            reverse=True,
        )
        totals = {
            "active_sessions": len(active_sessions),
            "recent_sessions": len(recent_sessions),
            "open_positions": len(open_trades),
            "closed_trades": sum(item.closed_trades_count for item in recent_sessions),
            "messages_processed": sum(item.total_messages_processed for item in recent_sessions),
            "realized_pnl": str(
                sum((item.total_realized_pnl for item in recent_sessions), Decimal("0"))
            ),
            "unrealized_pnl": str(
                sum((item.total_unrealized_pnl for item in recent_sessions), Decimal("0"))
            ),
        }
        return DashboardLiveOverview(
            is_running=self.is_running(),
            active_sessions=active_sessions,
            recent_sessions=recent_sessions,
            open_trades=open_trades,
            recent_closed_trades=recent_closed_trades[:closed_trade_limit],
            recent_messages=recent_messages[:message_limit],
            totals=totals,
        )

    def get_session_detail(
        self,
        session_id: str,
        *,
        message_limit: int = 120,
        closed_trade_limit: int = 40,
    ) -> LiveSessionDetail | None:
        session = self.store.load_session(session_id)
        if session is None:
            return None
        snapshot = self.get_snapshot(session_id)
        open_trades = self.store.list_open_trades(session_id)
        closed_trades = self.store.list_closed_trades(session_id, limit=closed_trade_limit)
        messages = self.store.list_message_traces(session_id, limit=message_limit)
        signals = self.store.list_signal_snapshots(session_id, limit=200)
        messages.sort(key=lambda item: item.received_at or item.message_date, reverse=True)
        signals.sort(
            key=lambda item: (
                item.status_group != "active",
                item.closed_at or item.updated_at,
            ),
            reverse=True,
        )
        return LiveSessionDetail(
            session=session,
            snapshot=snapshot,
            messages=messages,
            signals=signals,
            open_trades=open_trades,
            closed_trades=closed_trades,
        )

    def list_sessions(self, limit: int = 20) -> list[LiveSession]:
        return self.store.list_sessions(limit=limit)

    def list_active_sessions(self, limit: int = 20) -> list[LiveSession]:
        return self.store.list_active_sessions(limit=limit)

    def list_trades(self, session_id: str, *, open_only: bool = False) -> list[LiveTrade]:
        if open_only:
            return self.store.list_open_trades(session_id)
        return self.store.list_trades(session_id)

    def get_recent_messages(self, limit: int = 50) -> list[LiveMessageTrace]:
        messages: list[LiveMessageTrace] = []
        for session in self.store.list_sessions(limit=20):
            messages.extend(self.store.list_message_traces(session.session_id, limit=20))
        messages.sort(key=lambda item: item.received_at or item.message_date, reverse=True)
        return messages[:limit]

    def delete_session_history(self, session_id: str) -> bool:
        session = self.store.load_session(session_id)
        if session is None:
            return False
        if session.status in {"running", "starting"}:
            raise ValueError("session_must_be_stopped")
        return self.store.delete_session(session_id)

    async def forward_test_message(
        self,
        *,
        message_link: str,
        destination_channel: str,
    ) -> LiveMessageTrace:
        raw = await self.telegram_client.forward_message_by_link(
            message_link,
            destination_channel,
        )
        channel_label = (
            f"@{raw.channel_username}" if raw.channel_username else raw.channel_id
        )
        return LiveMessageTrace(
            session_id="telegram_test_forward",
            message_id=raw.message_id,
            channel_id=raw.channel_id,
            channel_username=raw.channel_username,
            channel_label=channel_label,
            reply_to_msg_id=raw.reply_to_msg_id,
            message_date=raw.date,
            received_at=_utc_now(),
            preview_text=(raw.text or "")[:200],
            full_text=raw.text,
            final_status="forwarded_for_live_test",
        )

    def delete_trade_record(self, session_id: str, trade_id: str) -> bool:
        deleted = self.store.delete_trade(session_id, trade_id)
        if deleted:
            for signal in self.store.list_signal_snapshots(session_id, limit=500):
                if signal.trade_id != trade_id:
                    continue
                updated = signal.model_copy(
                    update={
                        "trade_id": None,
                        "trade_status": None,
                        "exchange_position": None,
                        "exchange_order_history": [],
                        "notes": [*signal.notes, "trade_record_deleted_from_dashboard"],
                    }
                )
                self.store.save_signal_snapshot(session_id, updated)
        return deleted

    def delete_message_record(
        self,
        session_id: str,
        message_id: int,
        channel_id: str,
    ) -> bool:
        deleted = self.store.delete_message_trace(session_id, message_id, channel_id)
        if deleted:
            for signal in self.store.list_signal_snapshots(session_id, limit=500):
                if message_id not in signal.related_message_ids:
                    continue
                updated_related = [
                    item for item in signal.related_message_ids if item != message_id
                ]
                updated = signal.model_copy(
                    update={
                        "related_message_ids": updated_related,
                        "message_count": len(updated_related),
                        "notes": [
                            *signal.notes,
                            f"message_{message_id}_deleted_from_dashboard",
                        ],
                    }
                )
                if signal.last_message_id == message_id:
                    updated = updated.model_copy(
                        update={
                            "last_message_id": updated_related[-1] if updated_related else None,
                            "last_message_date": None,
                        }
                    )
                self.store.save_signal_snapshot(session_id, updated)
        return deleted

    def get_saved_channels(self) -> list[dict[str, Any]]:
        state = self.saved_channel_store.get()
        return [item.model_dump(mode="json") for item in state.channels]

    def save_channel(self, channel_input: str) -> list[dict[str, Any]]:
        updated = self.saved_channel_store.add(channel_input)
        saved = updated.channels[0]
        self._log_event(
            logging.INFO,
            "dashboard.live_channel_saved",
            channel_input=saved.channel_input,
            channel_resolved=saved.channel_resolved,
            total_saved_channels=len(updated.channels),
        )
        return [item.model_dump(mode="json") for item in updated.channels]

    def remove_channel(self, channel_reference: str) -> list[dict[str, Any]]:
        updated = self.saved_channel_store.remove(channel_reference)
        self._log_event(
            logging.INFO,
            "dashboard.live_channel_removed",
            channel_reference=channel_reference,
            total_saved_channels=len(updated.channels),
        )
        return [item.model_dump(mode="json") for item in updated.channels]

    async def fetch_account_info_direct(self) -> dict[str, Any]:
        if not self.readiness().toobit_configured:
            self._log_event(
                logging.WARNING,
                "dashboard.live_account_fetch_blocked",
                reason="toobit_not_configured",
            )
            return {
                "success": False,
                "error": "Toobit credentials are not configured.",
            }
        try:
            futures_client = build_futures_client_from_settings(self.settings)
            futures_task = asyncio.create_task(futures_client.get_full_account_info())
            spot_task = asyncio.create_task(futures_client.get_spot_account())
            futures_info, spot_info = await asyncio.gather(futures_task, spot_task)
        except Exception as exc:
            self._log_event(
                logging.WARNING,
                "dashboard.live_account_fetch_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {
                "success": False,
                "error": f"account fetch failed: {type(exc).__name__}: {exc}",
            }

        usdt_balance = spot_info.usdt_balance()
        spot_balances = [
            {
                "asset": item.asset,
                "total": str(item.total),
                "free": str(item.free),
                "locked": str(item.locked),
            }
            for item in spot_info.nonzero_balances()
        ]
        payload = {
            "success": True,
            "user_id": futures_info.user_id,
            "api_key_type": futures_info.api_key_type,
            "futures": {
                "wallet_balance": str(futures_info.total_wallet_balance),
                "available_balance": str(futures_info.available_balance),
                "unrealized_pnl": str(futures_info.total_unrealized_profit),
                "position_margin": str(futures_info.total_position_margin),
                "day_profit": str(futures_info.day_profit),
                "day_profit_rate": str(futures_info.day_profit_rate),
            },
            "spot": {
                "total": str(usdt_balance.total if usdt_balance else Decimal("0")),
                "free": str(usdt_balance.free if usdt_balance else Decimal("0")),
                "locked": str(usdt_balance.locked if usdt_balance else Decimal("0")),
                "all_balances": spot_balances,
            },
        }
        self._log_event(
            logging.INFO,
            "dashboard.live_account_fetch_completed",
            user_id=futures_info.user_id,
            spot_balance_count=len(spot_balances),
        )
        return payload

    def _run_engine(self, session_id: str, engine: LiveTradingEngine) -> None:
        try:
            while True:
                self._log_event(
                    logging.INFO,
                    "dashboard.live_engine_thread_started",
                    session_id=session_id,
                )
                failure: Exception | None = None
                try:
                    asyncio.run(engine.start())
                except Exception as exc:
                    failure = exc
                    self._log_event(
                        logging.ERROR,
                        "dashboard.live_engine_thread_failed",
                        session_id=session_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )

                with self._lock:
                    force_stopped = session_id in self._force_stopped_session_ids
                if force_stopped:
                    self._log_event(
                        logging.WARNING,
                        "dashboard.live_engine_recovery_suppressed_after_force_stop",
                        session_id=session_id,
                    )
                    break

                open_trades = self.store.list_open_trades(session_id)
                if not open_trades:
                    if failure is not None:
                        session = self.store.load_session(session_id) or engine.session
                        session.mark_stopped(
                            error=f"engine crashed: {type(failure).__name__}: {failure}"
                        )
                        self.store.save_session(session)
                        self._notify_session(session)
                    break

                session = self.store.load_session(session_id) or engine.session
                issue = (
                    "Engine worker exited with open trades; recovery supervision is active "
                    "and new entries are blocked."
                )
                session.status = "starting"
                session.stopped_at = None
                session.health_status = "critical"
                session.new_entries_blocked = True
                session.last_error = issue
                if issue not in session.health_issues:
                    session.health_issues.append(issue)
                if issue not in session.errors:
                    session.errors.append(issue)
                session.last_update_at = datetime.now(timezone.utc)
                self.store.save_session(session)
                self._notify_session(session)
                retry_seconds = int(
                    getattr(
                        self.settings,
                        "LIVE_TRADING_ENGINE_RECOVERY_RETRY_SECONDS",
                        30,
                    )
                )
                self._log_event(
                    logging.CRITICAL,
                    "dashboard.live_engine_recovery_scheduled",
                    session_id=session_id,
                    open_trade_count=len(open_trades),
                    retry_seconds=retry_seconds,
                )
                threading.Event().wait(max(1, retry_seconds))
                recovered_session, engine = build_engine_from_session(
                    session=session,
                    settings=self.settings,
                    store=self.store,
                    notifier=self.notifier,
                    telegram_client=self.telegram_client,
                    account_coordinator=self.account_coordinator,
                )
                with self._lock:
                    self._engines[session_id] = engine
                self._log_event(
                    logging.WARNING,
                    "dashboard.live_engine_recovery_started",
                    session_id=recovered_session.session_id,
                    open_trade_count=len(open_trades),
                )
        finally:
            with self._lock:
                self._engines.pop(session_id, None)
                self._threads.pop(session_id, None)
                self._force_stopped_session_ids.discard(session_id)
            self._log_event(
                logging.INFO,
                "dashboard.live_engine_thread_stopped",
                session_id=session_id,
            )

    def _current_session_id(self) -> str | None:
        session = self.get_current_session()
        return session.session_id if session else None

    def _recover_incomplete_sessions(self) -> None:
        for session in self.store.list_active_sessions(limit=200):
            open_trades = self.store.list_open_trades(session.session_id)
            if not self.settings.LIVE_TRADING_AUTO_RESUME_SESSIONS and not open_trades:
                session.mark_stopped(
                    error="Dashboard restart interrupted the in-memory worker for this session."
                )
                self.store.save_session(session)
                self._log_event(
                    logging.WARNING,
                    "dashboard.live_session_marked_stopped_on_recovery",
                    session_id=session.session_id,
                )
                continue
            if not self.settings.LIVE_TRADING_AUTO_RESUME_SESSIONS:
                issue = (
                    "Open trades require recovery supervision after dashboard restart; "
                    "new entries are blocked."
                )
                session.new_entries_blocked = True
                session.health_status = "critical"
                session.last_error = issue
                if issue not in session.health_issues:
                    session.health_issues.append(issue)
                if issue not in session.errors:
                    session.errors.append(issue)
                self.store.save_session(session)
            recovered_session, engine = build_engine_from_session(
                session=session,
                settings=self.settings,
                store=self.store,
                notifier=self.notifier,
                telegram_client=self.telegram_client,
                account_coordinator=self.account_coordinator,
            )
            worker = threading.Thread(
                target=self._run_engine,
                args=(recovered_session.session_id, engine),
                name=f"live-session-{recovered_session.session_id}",
                daemon=True,
            )
            with self._lock:
                self._engines[recovered_session.session_id] = engine
                self._threads[recovered_session.session_id] = worker
            worker.start()
            self._log_event(
                logging.INFO,
                "dashboard.live_session_recovered",
                session_id=recovered_session.session_id,
                channels=recovered_session.channels,
            )

    def _flag_unresolved_inactive_sessions(self) -> None:
        issue_prefix = "Inactive session has "
        for session in self.store.list_sessions(limit=500):
            if session.status in {"running", "starting"}:
                continue
            open_trades = self.store.list_open_trades(session.session_id)
            if not open_trades:
                previous_issues = list(session.health_issues)
                session.health_issues = [
                    issue
                    for issue in session.health_issues
                    if not issue.startswith(issue_prefix)
                ]
                if session.health_issues != previous_issues:
                    if not session.health_issues:
                        session.health_status = "healthy"
                        session.new_entries_blocked = False
                    if session.last_error and session.last_error.startswith(issue_prefix):
                        session.last_error = None
                    session.last_update_at = datetime.now(timezone.utc)
                    self.store.save_session(session)
                continue
            issue = (
                f"{issue_prefix}{len(open_trades)} unresolved open or pending trade(s); "
                "exchange fill reconciliation is required before reuse."
            )
            session.health_status = "critical"
            session.new_entries_blocked = True
            session.last_error = issue
            if issue not in session.health_issues:
                session.health_issues.append(issue)
            if issue not in session.errors:
                session.errors.append(issue)
            session.last_update_at = datetime.now(timezone.utc)
            self.store.save_session(session)
            self._log_event(
                logging.CRITICAL,
                "dashboard.inactive_session_unresolved_trades_flagged",
                session_id=session.session_id,
                session_status=session.status,
                open_trade_count=len(open_trades),
                symbols=sorted({trade.symbol for trade in open_trades}),
            )

    def _notify_session(self, session: LiveSession) -> None:
        if not self.notifier:
            return
        self.notifier(
            {
                "type": "live_session",
                "session": session.model_dump(mode="json"),
            }
        )

    async def aclose(self) -> None:
        await self.telegram_client.aclose()

    @staticmethod
    def _secret_present(secret: Any) -> bool:
        if secret is None:
            return False
        if hasattr(secret, "get_secret_value"):
            value = secret.get_secret_value()
        else:
            value = str(secret)
        normalized = str(value).strip()
        return bool(normalized) and normalized.lower() != "replace_me"

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
