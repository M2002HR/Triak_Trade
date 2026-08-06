"""Live / demo trading engine — real-time Telegram signal processing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

from triak_trade.agents.classifier import MessageClassifier, RegexMessageClassifier
from triak_trade.agents.context import ChannelContext
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.backtesting.correlation import resolve_related_signal_id
from triak_trade.backtesting.directives import (
    apply_text_directive_action,
    detect_close_all_instruction,
    detect_move_stop_to_entry,
    detect_percentage_tp_update,
    detect_stop_loss_value,
    detect_tp_list_update,
    extract_close_fraction,
    normalize_related_signal_action,
)
from triak_trade.backtesting.strategies.registry import load_strategy
from triak_trade.config.settings import Settings
from triak_trade.core.logging import log_event, safe_preview
from triak_trade.core.symbols import canonical_market_symbol
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, SignalStatus, TradeSide
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.errors import ToobitAPIError
from triak_trade.exchange.toobit.futures import (
    ToobitFuturesClient,
    _from_exchange_contract_quantity,
    _to_exchange_contract_quantity,
    build_futures_client_from_settings,
    from_futures_symbol,
    to_exchange_futures_symbol,
)
from triak_trade.live_trading.account_coordinator import (
    AccountExecutionConflictError,
    AccountExecutionCoordinator,
)
from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveEntryOrderPlan,
    LiveExchangeOrderSnapshot,
    LiveExchangePositionSnapshot,
    LiveExchangeSnapshot,
    LiveMessageTrace,
    LiveSession,
    LiveSessionConfig,
    LiveSignalSnapshot,
    LiveTakeProfitOrderPlan,
    LiveTrade,
    LiveTradingSnapshot,
    MessageAttribution,
    _utc_now,
    build_live_session_label,
)
from triak_trade.live_trading.position_manager import LivePositionManager
from triak_trade.live_trading.store import LiveTradingStore
from triak_trade.live_trading.take_profit_planner import (
    TakeProfitCandidate,
    plan_maximum_take_profit_orders,
)
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.client import TelegramClientInterface
from triak_trade.telegram.telethon_client import TelethonTelegramClient

log = logging.getLogger(__name__)

_RETRO_SIGNAL_TEXT_MARKERS = (
    "target",
    "targets",
    "tp",
    "stop",
    "stoploss",
    "sl",
    "entry",
    "entries",
    "market",
    "limit",
    "leverage",
    "تارگت",
    "حد ضرر",
    "استاپ",
    "ورود",
    "نقطه ورود",
)

_VISUAL_SIGNAL_PROMOTABLE_ACTIONS = {
    SignalAction.UPDATE_SL,
    SignalAction.UPDATE_TP,
    SignalAction.UPDATE_ENTRY,
    SignalAction.UPDATE_LEVERAGE,
}


@dataclass
class _ExchangeCloseResult:
    order_id: str
    status: str
    avg_price: Decimal
    executed_quantity: Decimal
    realized_pnl: Decimal
    fees: Decimal


@dataclass(frozen=True)
class _TakeProfitLadderPlan:
    pending_take_profits: list[Decimal]
    required_quantity: Decimal | None
    original_count: int
    target_count: int


@dataclass(frozen=True)
class _StartupBootstrapResult:
    replay_messages: list[RawTelegramMessage]
    backlog_messages: list[RawTelegramMessage]


@dataclass(frozen=True)
class _EntryOrderRequest:
    order_type: str
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class _StopCooldownDecision:
    blocked: bool
    remaining_seconds: int = 0
    consecutive_stops: int = 0
    source_trade_id: str | None = None


class LiveTradingEngine:
    """
    Orchestrates live/demo trading:
    - Listens to Telegram channels in real-time
    - Classifies and parses signals (AI or regex)
    - Opens/updates/closes positions (paper in demo, real on Toobit in live)
    - Refreshes mark prices every 60s, checks SL/TP
    - Emits WebSocket updates for dashboard message feed + stats
    """

    def __init__(
        self,
        *,
        settings: Settings,
        session: LiveSession,
        store: LiveTradingStore,
        notifier: Callable[[dict[str, Any]], None] | None = None,
        telegram_client: TelegramClientInterface | None = None,
        account_coordinator: AccountExecutionCoordinator | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.store = store
        self.notifier = notifier
        self._account_coordinator = (
            account_coordinator or AccountExecutionCoordinator.from_settings(settings)
        )
        self._owns_telegram_client = telegram_client is None

        self._running = False
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._message_task: asyncio.Task[None] | None = None
        self._last_poll_heartbeat_at: datetime | None = None

        # Per-channel signal state
        self._contexts: dict[str, ChannelContext] = {}
        self._last_seen_message_ids: dict[str, int] = {}
        self._channel_poll_anchor: dict[str, Any] = {}

        # signal_id → open trade (in-memory, backed by store)
        self._open_trades: dict[str, LiveTrade] = {}

        # Recent message traces for dashboard feed (up to 200)
        self._message_traces: list[LiveMessageTrace] = []

        # Components built in _setup_components()
        self._classifier: MessageClassifier | None = None
        self._telegram_client: TelegramClientInterface | None = telegram_client
        self._futures_client: ToobitFuturesClient | None = None
        self._pm: LivePositionManager | None = None
        self._strategy: Any = None
        self._validator = ParsedSignalValidator()

    def _log_event(self, level: int, event: str, /, **fields: Any) -> None:
        log_event(
            log,
            level,
            event,
            session_id=self.session.session_id,
            trading_mode=self.session.trading_mode,
            session_status=self.session.status,
            **fields,
        )

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _message_log_fields(self, message: RawTelegramMessage) -> dict[str, Any]:
        payload = message.raw_payload
        return {
            "channel_id": message.channel_id,
            "channel_username": message.channel_username,
            "message_id": message.message_id,
            "reply_to_msg_id": message.reply_to_msg_id,
            "message_date": message.date.isoformat(),
            "message_preview": safe_preview(message.text),
            "has_media": bool(payload.get("has_media")),
            "caption_present": bool(payload.get("caption_present")),
            "image_count": len(payload.get("image_data_urls") or []),
            "has_context_images": bool(payload.get("context_image_message_ids")),
            "context_image_message_ids": payload.get("context_image_message_ids") or [],
        }

    def _trade_log_fields(self, trade: LiveTrade) -> dict[str, Any]:
        return {
            "trade_id": trade.trade_id,
            "trade_signal_id": trade.signal_id,
            "symbol": trade.symbol,
            "exchange_symbol": trade.exchange_symbol,
            "side": trade.side,
            "trade_status": trade.status,
            "leverage": trade.leverage,
            "entry_price": self._decimal_text(trade.entry_price),
            "entry_type": trade.entry_type,
            "entry_order_type": trade.entry_order_type,
            "entry_order_status": trade.entry_order_status,
            "quantity": self._decimal_text(trade.quantity),
            "remaining_quantity": self._decimal_text(trade.remaining_quantity),
            "mark_price": self._decimal_text(trade.mark_price),
            "stop_loss": self._decimal_text(trade.stop_loss),
            "targets_hit": trade.targets_hit,
            "take_profit_count": len(trade.take_profits),
            "pending_take_profit_count": len(trade.take_profits[trade.targets_hit :]),
            "sl_order_id": trade.sl_order_id,
            "tp_order_count": len(trade.tp_order_ids),
        }

    def _protection_log_fields(self, trade: LiveTrade) -> dict[str, Any]:
        pending_take_profits = trade.take_profits[trade.targets_hit :]
        return {
            "required_stop_loss": trade.stop_loss is not None,
            "required_take_profit_count": len(pending_take_profits),
            "pending_take_profits": [str(item) for item in pending_take_profits],
            "tracked_tp_order_ids": list(trade.tp_order_ids),
            "required_exchange_tp_order_count": trade.required_tp_order_count,
        }

    def _parsed_signal_log_fields(self, parsed: ParsedSignal) -> dict[str, Any]:
        return {
            "action": parsed.action.value,
            "symbol": parsed.symbol,
            "side": parsed.side.value,
            "entry_type": parsed.entry_type.value,
            "entry_low": self._decimal_text(parsed.entry_low),
            "entry_high": self._decimal_text(parsed.entry_high),
            "stop_loss": self._decimal_text(parsed.stop_loss),
            "take_profit_count": len(parsed.take_profits),
            "take_profits": [str(item) for item in parsed.take_profits],
            "leverage": parsed.leverage,
            "confidence": str(parsed.confidence),
        }

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._log_event(
            logging.INFO,
            "live_trading.session_starting",
            channel_count=len(self.session.channels),
            channels=self.session.channels,
            use_ai=self.session.use_ai,
            strategy_key=self.session.strategy_key,
        )
        if self._stop_requested:
            self.session.mark_stopped()
            self.store.save_session(self.session)
            self._emit_session_update()
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._setup_components()
        self._restore_runtime_state()
        startup_bootstrap = (
            _StartupBootstrapResult(replay_messages=[], backlog_messages=[])
            if self.session.recovery_only
            else await self._bootstrap_channel_cursors()
        )
        if self._stop_requested:
            return

        # Fetch initial account info immediately
        await self._refresh_account()
        if self._stop_requested:
            return

        self.session.mark_running()
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(
            logging.INFO,
            "live_trading.session_started",
            channel_count=len(self.session.channels),
            channels=self.session.channels,
            open_trade_count=len(self._open_trades),
        )
        for message in startup_bootstrap.backlog_messages:
            self._record_startup_backlog_message(message)
        for message in startup_bootstrap.replay_messages:
            await self._handle_message(message)

        bg_tasks = [
            asyncio.create_task(self._price_refresh_loop(), name="price_refresh"),
            asyncio.create_task(self._account_refresh_loop(), name="account_refresh"),
        ]
        if not self.session.recovery_only:
            bg_tasks.extend(
                [
                    asyncio.create_task(
                        self._pending_entry_monitor_loop(),
                        name="pending_entry_monitor",
                    ),
                    asyncio.create_task(
                        self._consolidation_tick_loop(), name="consolidation"
                    ),
                ]
            )

        try:
            if self.session.recovery_only:
                self._message_task = asyncio.create_task(
                    self._recovery_only_monitor_loop(),
                    name="recovery_only_monitor",
                )
            else:
                assert self._telegram_client is not None
                self._message_task = asyncio.create_task(
                    self._poll_messages_loop(),
                    name="tg_poll_loop",
                )
            await self._message_task
        except asyncio.CancelledError:
            self._log_event(
                logging.INFO,
                "live_trading.telegram_poller_cancelled",
            )
        except Exception as exc:
            worker_name = "Recovery monitor" if self.session.recovery_only else "Telegram poller"
            error = f"{worker_name} failed: {type(exc).__name__}: {exc}"
            self._log_event(
                logging.ERROR,
                "live_trading.telegram_poller_failed",
                error=error,
                error_type=type(exc).__name__,
            )
            log.exception("Live trading telegram poller failed")
            self.session.mark_stopped(error=error)
            self.store.save_session(self.session)
            self._emit_session_update()
        finally:
            self._running = False
            self._message_task = None
            for t in bg_tasks:
                t.cancel()
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        if self.session.status == "running":
            self.session.mark_stopped()
            self.store.save_session(self.session)
            self._emit_session_update()
        self._log_event(
            logging.INFO,
            "live_trading.session_stopped",
            stopped_at=self.session.stopped_at.isoformat() if self.session.stopped_at else None,
            open_trade_count=len(self._open_trades),
            total_messages_processed=self.session.total_messages_processed,
        )

    async def _recovery_only_monitor_loop(self) -> None:
        self._log_event(
            logging.WARNING,
            "live_trading.recovery_only_monitor_started",
            open_trade_count=len(self._open_trades),
        )
        while self._running and self._open_trades:
            await asyncio.sleep(1)
        self._log_event(
            logging.INFO,
            "live_trading.recovery_only_monitor_completed",
            open_trade_count=len(self._open_trades),
        )

    def stop(self) -> None:
        """Thread-safe stop — cancels the Telegram polling task."""
        self._stop_requested = True
        self._running = False
        self._log_event(logging.INFO, "live_trading.stop_requested")
        loop = self._loop
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._async_stop(), loop)

    async def _async_stop(self) -> None:
        if self._telegram_client is not None:
            try:
                if self._owns_telegram_client:
                    await self._telegram_client.stop()
            except Exception:
                self._log_event(logging.WARNING, "live_trading.telegram_client_stop_failed")
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
        self.session.mark_stopped()
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(logging.INFO, "live_trading.stop_completed")

    def get_snapshot(self) -> LiveTradingSnapshot:
        open_trades = list(self._open_trades.values())
        closed = self.store.list_closed_trades(self.session.session_id, limit=30)
        return LiveTradingSnapshot(
            session=self.session,
            open_trades=open_trades,
            recent_closed_trades=closed,
            account_info=self.session.account_info,
        )

    async def refresh_exchange_state(self) -> bool:
        """Refresh account and position state from Toobit for dashboard reads."""

        if self._futures_client is None:
            return False
        return await self._refresh_account()

    def get_recent_messages(self, limit: int = 50) -> list[LiveMessageTrace]:
        return list(reversed(self._message_traces[-limit:]))

    # ── Component Setup ────────────────────────────────────────────────────

    def _setup_components(self) -> None:
        s = self.settings
        self._pm = LivePositionManager(s)
        self._strategy = load_strategy(self.session.strategy_key)
        self._log_event(
            logging.INFO,
            "live_trading.components_initializing",
            strategy_key=self.session.strategy_key,
        )

        # Build AI or regex classifier
        use_ai = self.session.use_ai and s.AI_GATEWAY_ENABLED and s.AI_CLASSIFIER_ENABLED
        if use_ai:
            try:
                gateway = AjilGatewayClient(
                    base_url=s.AI_GATEWAY_BASE_URL,
                    auth_token=s.AI_GATEWAY_AUTH_TOKEN.get_secret_value(),
                    timeout_seconds=s.AI_GATEWAY_TIMEOUT_SECONDS,
                    classify_path=s.AI_GATEWAY_CLASSIFY_PATH,
                    auth_header_name=s.AI_GATEWAY_AUTH_HEADER_NAME,
                    retry_attempts=s.AI_GATEWAY_RETRY_ATTEMPTS,
                    retry_backoff_seconds=s.AI_GATEWAY_RETRY_BACKOFF_SECONDS,
                    trust_env=s.AI_GATEWAY_TRUST_ENV,
                )
                self._classifier = AIMessageClassifier(
                    gateway_client=gateway,
                    settings=s,
                    require_gateway_for_all_messages=self._must_fail_closed_without_ai(),
                )
                self._log_event(
                    logging.INFO,
                    "live_trading.classifier_selected",
                    classifier="ai",
                )
            except Exception as exc:
                if self._must_fail_closed_without_ai():
                    raise RuntimeError(
                        "AI classifier init failed for exchange execution session"
                    ) from exc
                self._log_event(
                    logging.WARNING,
                    "live_trading.classifier_fallback",
                    requested_classifier="ai",
                    fallback_classifier="regex",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                log.exception("AI classifier init failed, falling back to regex")
                self._classifier = RegexMessageClassifier()
        else:
            if self._must_fail_closed_without_ai():
                raise RuntimeError("AI classifier is required for exchange execution session")
            self._classifier = RegexMessageClassifier()
            self._log_event(
                logging.INFO,
                "live_trading.classifier_selected",
                classifier="regex",
            )

        if self._telegram_client is None:
            self._telegram_client = TelethonTelegramClient(settings=s)

        # Build futures client for both demo (mark prices) and live (orders)
        try:
            self._futures_client = build_futures_client_from_settings(s)
            self._log_event(
                logging.INFO,
                "live_trading.futures_client_ready",
            )
        except Exception as exc:
            self._log_event(
                logging.WARNING,
                "live_trading.futures_client_init_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            log.exception("Failed to build futures client")

    def _restore_runtime_state(self) -> None:
        self._open_trades = {
            trade.signal_id: trade for trade in self.store.list_open_trades(self.session.session_id)
        }
        self.session.open_positions_count = len(self._open_trades)
        for trade in self._open_trades.values():
            self._register_account_trade(trade)
        self._prune_terminal_trade_health_issues()
        self._message_traces = list(
            reversed(self.store.list_message_traces(self.session.session_id, limit=200))
        )
        self._last_seen_message_ids = {}
        self._channel_poll_anchor = {
            channel: self.session.started_at for channel in self.session.channels
        }

        traces = list(reversed(self.store.list_message_traces(self.session.session_id, limit=5000)))
        by_channel_messages: dict[str, list[RawTelegramMessage]] = {}
        for trace in traces:
            by_channel_messages.setdefault(trace.channel_id, []).append(
                RawTelegramMessage(
                    channel_id=trace.channel_id,
                    channel_username=trace.channel_username,
                    message_id=trace.message_id,
                    text=trace.full_text,
                    date=trace.message_date,
                    edited_at=None,
                    reply_to_msg_id=trace.reply_to_msg_id,
                    raw_payload={},
                )
            )
            current_max = self._last_seen_message_ids.get(trace.channel_id, 0)
            self._last_seen_message_ids[trace.channel_id] = max(current_max, trace.message_id)
        for channel_id, messages in by_channel_messages.items():
            context = self._get_or_create_context(channel_id)
            messages.sort(key=lambda item: (item.date, item.message_id))
            for message in messages:
                context.add_recent_message(message)
            context.seed_message_catalog(messages)

        snapshots = list(
            reversed(self.store.list_signal_snapshots(self.session.session_id, limit=5000))
        )
        for snapshot in snapshots:
            context = self._get_or_create_context(snapshot.channel_id)
            state = self._signal_state_from_snapshot(snapshot)
            if (
                state.status is not SignalStatus.PENDING_CONSOLIDATION
                and state.status
                in {
                    SignalStatus.ORDER_PLANNED,
                    SignalStatus.ORDER_SUBMITTED,
                    SignalStatus.OPEN,
                }
                and state.signal_id not in self._open_trades
            ):
                state.status = SignalStatus.CLOSED
                self._sync_signal_snapshot(context=context, state=state, trade=None)
                self._log_event(
                    logging.WARNING,
                    "live_trading.stale_signal_snapshot_closed_on_restore",
                    signal_id=state.signal_id,
                    previous_status=snapshot.status,
                    channel_id=state.channel_id,
                )
            context.add_signal(
                state,
                pending=state.status is SignalStatus.PENDING_CONSOLIDATION,
            )
        self._log_event(
            logging.INFO,
            "live_trading.runtime_state_restored",
            open_trade_count=len(self._open_trades),
            trace_count=len(self._message_traces),
            context_count=len(self._contexts),
            restored_last_seen_count=len(self._last_seen_message_ids),
        )

    def _prune_terminal_trade_health_issues(self) -> None:
        """Remove durable health blocks whose owning trade is already terminal."""

        terminal_trades = [
            trade
            for trade in self.store.list_trades(self.session.session_id, limit=5000)
            if not trade.is_open
        ]
        if not terminal_trades or not self.session.health_issues:
            return
        prefixes = tuple(
            prefix
            for trade in terminal_trades
            for prefix in (
                f"protection unavailable for {trade.trade_id}:",
                f"exchange quantity drift for {trade.trade_id}",
                f"exchange reconciliation required for {trade.trade_id}:",
            )
        )
        retained = [
            issue
            for issue in self.session.health_issues
            if not issue.startswith(prefixes)
        ]
        if retained == self.session.health_issues:
            return
        for trade in terminal_trades:
            for prefix in (
                f"protection unavailable for {trade.trade_id}:",
                f"exchange quantity drift for {trade.trade_id}",
                f"exchange reconciliation required for {trade.trade_id}:",
            ):
                self._account_coordinator.clear_trade_bucket_block(
                    trade,
                    trading_mode=self.session.trading_mode,
                    reason_prefix=prefix,
                )
        self.session.health_issues = retained
        if not retained:
            self.session.health_status = "healthy"
            self.session.new_entries_blocked = False
            self.session.last_error = None
        elif self.session.last_error and self.session.last_error.startswith(prefixes):
            self.session.last_error = retained[-1]
        self.session.last_update_at = _utc_now()
        self.store.save_session(self.session)

    async def _poll_messages_loop(self) -> None:
        poll_seconds = max(1, int(self.settings.LIVE_TRADING_TELEGRAM_POLL_SECONDS))
        self._log_event(
            logging.INFO,
            "live_trading.telegram_poller_started",
            channels=self.session.channels,
            poll_interval_seconds=poll_seconds,
        )
        while self._running:
            await self._poll_messages_once()
            if not self._running:
                break
            await asyncio.sleep(poll_seconds)

    async def _poll_messages_once(self) -> None:
        assert self._telegram_client is not None
        for channel in self.session.channels:
            if not self._running:
                return
            try:
                await self._poll_channel_messages(channel)
            except Exception as exc:
                self._log_event(
                    logging.WARNING,
                    "live_trading.channel_poll_failed",
                    channel=channel,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                log.exception("Live trading channel poll failed")
        self._record_poll_heartbeat()

    async def _bootstrap_channel_cursors(self) -> _StartupBootstrapResult:
        assert self._telegram_client is not None
        replay_messages: list[RawTelegramMessage] = []
        backlog_messages: list[RawTelegramMessage] = []
        startup_history_limit = max(
            1,
            int(getattr(self.settings, "LIVE_TRADING_STARTUP_HISTORY_LIMIT", 5)),
        )
        startup_replay_seconds = max(
            0,
            int(getattr(self.settings, "LIVE_TRADING_STARTUP_REPLAY_SECONDS", 180)),
        )
        startup_cutoff = self.session.started_at - timedelta(seconds=startup_replay_seconds)
        for channel in self.session.channels:
            if self._last_seen_message_ids.get(channel, 0) > 0:
                continue
            try:
                latest_messages = await self._telegram_client.fetch_history(
                    channel,
                    limit=startup_history_limit,
                )
            except Exception as exc:
                self._log_event(
                    logging.WARNING,
                    "live_trading.cursor_bootstrap_failed",
                    channel=channel,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                log.exception("Live trading cursor bootstrap failed")
                continue
            if not latest_messages:
                self._log_event(
                    logging.DEBUG,
                    "live_trading.cursor_bootstrap_empty",
                    channel=channel,
                )
                continue
            latest_messages.sort(key=lambda item: (item.date, item.message_id))
            latest_message_id = max(item.message_id for item in latest_messages)
            self._last_seen_message_ids[channel] = latest_message_id
            replayable_messages = [item for item in latest_messages if item.date >= startup_cutoff]
            backlog_only_messages = [item for item in latest_messages if item.date < startup_cutoff]
            replay_messages.extend(replayable_messages)
            backlog_messages.extend(backlog_only_messages)
            self._log_event(
                logging.INFO,
                "live_trading.cursor_bootstrapped",
                channel=channel,
                last_seen_message_id=latest_message_id,
                replay_candidate_count=len(replayable_messages),
                backlog_candidate_count=len(backlog_only_messages),
                startup_cutoff=startup_cutoff.isoformat(),
            )
        replay_messages.sort(key=lambda item: (item.date, item.message_id))
        backlog_messages.sort(key=lambda item: (item.date, item.message_id))
        return _StartupBootstrapResult(
            replay_messages=replay_messages,
            backlog_messages=backlog_messages,
        )

    def _record_startup_backlog_message(self, message: RawTelegramMessage) -> None:
        self.session.total_messages_processed += 1
        trace = LiveMessageTrace(
            session_id=self.session.session_id,
            message_id=message.message_id,
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            channel_label=self._channel_label(message.channel_id),
            reply_to_msg_id=message.reply_to_msg_id,
            message_date=message.date,
            preview_text=(message.text or "")[:200],
            full_text=message.text,
            classification="startup_backlog",
            final_status="startup_backlog",
            effect_summary="Loaded from recent channel history at session start.",
            debug_notes=["startup_backlog"],
        )
        self._push_trace(trace)
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(
            logging.INFO,
            "live_trading.startup_backlog_message_recorded",
            final_status=trace.final_status,
            effect_summary=trace.effect_summary,
            total_messages_processed=self.session.total_messages_processed,
            **self._message_log_fields(message),
        )

    async def _poll_channel_messages(self, channel: str) -> None:
        assert self._telegram_client is not None
        last_seen = self._last_seen_message_ids.get(channel, 0)
        if last_seen > 0:
            messages = await self._telegram_client.fetch_history(
                channel,
                min_message_id=last_seen + 1,
            )
        else:
            anchor = self._channel_poll_anchor.get(channel, self.session.started_at)
            messages = await self._telegram_client.fetch_history(
                channel,
                start=anchor,
            )
            self._channel_poll_anchor[channel] = _utc_now()
        if not messages:
            self._log_event(
                logging.DEBUG,
                "live_trading.channel_poll_empty",
                channel=channel,
                last_seen_message_id=last_seen,
            )
            return
        self._log_event(
            logging.INFO,
            "live_trading.channel_poll_fetched",
            channel=channel,
            message_count=len(messages),
            last_seen_before=last_seen,
            first_message_id=messages[0].message_id,
            last_message_id=messages[-1].message_id,
        )
        for message in messages:
            current_seen = self._last_seen_message_ids.get(channel, 0)
            if message.message_id <= current_seen:
                continue
            await self._handle_message(message)
            self._last_seen_message_ids[channel] = max(
                self._last_seen_message_ids.get(channel, 0),
                message.message_id,
            )

    def _record_poll_heartbeat(self) -> None:
        now = _utc_now()
        last = self._last_poll_heartbeat_at
        interval_seconds = max(
            1,
            int(getattr(self.settings, "LIVE_TRADING_TELEGRAM_POLL_SECONDS", 5)),
        )
        if last is not None and (now - last).total_seconds() < interval_seconds:
            return
        self._last_poll_heartbeat_at = now
        self.session.last_update_at = now
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(
            logging.DEBUG,
            "live_trading.poll_heartbeat_recorded",
            open_trade_count=len(self._open_trades),
            total_messages_processed=self.session.total_messages_processed,
        )

    def _signal_state_from_snapshot(self, snapshot: LiveSignalSnapshot) -> SignalState:
        parsed_signal: ParsedSignal | None = None
        if snapshot.symbol is not None or snapshot.stop_loss is not None or snapshot.take_profits:
            side = TradeSide.UNKNOWN
            if snapshot.side in {"long", "buy"}:
                side = TradeSide.LONG
            elif snapshot.side in {"short", "sell"}:
                side = TradeSide.SHORT
            try:
                entry_type = EntryType(snapshot.entry_type or "")
            except ValueError:
                entry_type = (
                    EntryType.RANGE
                    if snapshot.entry_low is not None and snapshot.entry_high is not None
                    else EntryType.MARKET
                )
            parsed_signal = ParsedSignal(
                action=SignalAction.OPEN,
                market=MarketType.FUTURES,
                symbol=snapshot.symbol,
                side=side,
                entry_type=entry_type,
                entry_low=snapshot.entry_low,
                entry_high=snapshot.entry_high,
                stop_loss=snapshot.stop_loss,
                take_profits=list(snapshot.take_profits),
                leverage=snapshot.leverage,
                confidence=Decimal("1"),
                invalid_reason=None,
                source_channel_id=snapshot.channel_id,
                source_message_id=snapshot.created_from_message_id,
                parser_version="recovered",
            )
        base_created_at = snapshot.opened_at or snapshot.updated_at
        created_at = min(base_created_at, snapshot.updated_at)
        updated_at = max(snapshot.updated_at, created_at)
        return SignalState(
            signal_id=snapshot.signal_id,
            channel_id=snapshot.channel_id,
            status=SignalStatus(snapshot.status),
            created_from_message_id=snapshot.created_from_message_id,
            related_message_ids=(
                list(snapshot.related_message_ids) or [snapshot.created_from_message_id]
            ),
            current_signal=parsed_signal,
            version=max(1, snapshot.message_count or 1),
            created_at=created_at,
            updated_at=updated_at,
            expires_at=None,
        )

    def _must_fail_closed_without_ai(self) -> bool:
        return self.session.trading_mode in {"demo", "live"} or bool(
            getattr(self.settings, "LIVE_TRADING_REQUIRE_AI_CLASSIFIER", False)
        )

    def _get_or_create_context(self, channel_id: str) -> ChannelContext:
        if channel_id not in self._contexts:
            self._contexts[channel_id] = ChannelContext(
                channel_id=channel_id,
                max_message_limit=self.settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
                max_update_window_hours=self.settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
            )
        return self._contexts[channel_id]

    # ── Message Handler ────────────────────────────────────────────────────

    async def _handle_message(self, message: RawTelegramMessage) -> None:
        if not self._running:
            return
        try:
            self._log_event(
                logging.INFO,
                "live_trading.message_received",
                **self._message_log_fields(message),
            )
            await self._process_message(message)
        except Exception as exc:
            self._log_event(
                logging.ERROR,
                "live_trading.message_processing_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self._message_log_fields(message),
            )
            log.exception("Error processing live trading message")

    async def _process_message(self, message: RawTelegramMessage) -> None:
        self.session.total_messages_processed += 1
        channel_label = self._channel_label(message.channel_id)
        self._log_event(
            logging.INFO,
            "live_trading.message_processing_started",
            total_messages_processed=self.session.total_messages_processed,
            **self._message_log_fields(message),
        )

        # Build message trace for dashboard feed
        trace = LiveMessageTrace(
            session_id=self.session.session_id,
            message_id=message.message_id,
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            channel_label=channel_label,
            reply_to_msg_id=message.reply_to_msg_id,
            message_date=message.date,
            preview_text=(message.text or "")[:200],
            full_text=message.text,
        )

        context = self._get_or_create_context(message.channel_id)
        message = await self._prepare_message_for_classification(
            message=message,
            context=context,
            trace=trace,
        )
        context.add_recent_message(message)

        # Classify
        assert self._classifier is not None
        classified = self._classifier.classify(message, context)
        parsed = classified.parsed_signal
        for note in getattr(classified, "debug_notes", []):
            if note not in trace.debug_notes:
                trace.debug_notes.append(note)
        self._log_event(
            logging.INFO,
            "live_trading.message_classified",
            classification=(
                classified.classification if hasattr(classified, "classification") else None
            ),
            raw_related_signal_id=getattr(classified, "related_signal_id", None),
            **self._message_log_fields(message),
            **self._parsed_signal_log_fields(parsed),
        )

        # ── Text-directive upgrades (matches backtest real_runner logic) ──────
        # 1. close_all overrides ANY AI action, even IGNORE/UNKNOWN
        ai_non_actionable = (
            parsed.action is SignalAction.IGNORE
            and any(
                note.startswith("classification=")
                and note.split("=", 1)[1] in {
                    "UNRELATED",
                    "ADVERTISEMENT",
                    "GENERAL_ANALYSIS",
                    "RESULT_REPORT",
                }
                for note in trace.debug_notes
            )
        )
        close_all_detected = detect_close_all_instruction(message.text) and not ai_non_actionable
        if close_all_detected and parsed.action is not SignalAction.CLOSE:
            parsed = parsed.model_copy(update={"action": SignalAction.CLOSE})

        # 2. For UNKNOWN/IGNORE: try text-based promotion before giving up
        if parsed.action in (SignalAction.IGNORE, SignalAction.UNKNOWN) and not ai_non_actionable:
            upgraded = apply_text_directive_action(parsed.action, message.text)
            if upgraded not in (SignalAction.IGNORE, SignalAction.UNKNOWN):
                parsed = parsed.model_copy(update={"action": upgraded})
            else:
                # TP list rows often classified as UNKNOWN by AI (e.g. "Tp List: 100 200 300")
                tp_list_values = detect_tp_list_update(message.text)
                if tp_list_values:
                    parsed = parsed.model_copy(
                        update={
                            "action": SignalAction.UPDATE_TP,
                            "take_profits": tp_list_values,
                        }
                    )

        trace.classification = (
            classified.classification
            if hasattr(classified, "classification")
            else str(parsed.action.value)
        )
        trace.parsed_action = parsed.action.value
        trace.symbol = parsed.symbol
        trace.side = parsed.side.value if parsed.side else None
        trace.confidence = str(parsed.confidence)

        # ── Resolve correlation using the *effective* action ─────────────────
        raw_related_id = getattr(classified, "related_signal_id", None)
        corr = resolve_related_signal_id(
            context=context,
            parsed=parsed,
            raw_related_id=raw_related_id,
            message=message,
            action=parsed.action,
            allow_last_resort=self.settings.REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH,
        )
        related_signal_id = corr.signal_id if corr is not None else None
        trace.correlation_method = corr.method if corr is not None else None
        trace.correlation_note = corr.note if corr is not None else None
        if corr is not None and corr.note:
            trace.debug_notes.append(corr.note)
        if parsed.action is SignalAction.OPEN and related_signal_id is not None:
            related_state = context.get_signal(related_signal_id)
            is_pending_consolidation = (
                related_state is not None
                and related_state.status is SignalStatus.PENDING_CONSOLIDATION
            )
            if not is_pending_consolidation and related_signal_id not in self._open_trades:
                trace.debug_notes.append(f"stale_related_signal_detached={related_signal_id}")
                related_signal_id = None
                trace.correlation_method = "new_signal"
                trace.correlation_note = "stale_related_signal_without_open_trade"
        if related_signal_id is not None and self._uses_exchange_execution():
            related_trade = self._open_trades.get(related_signal_id)
            if related_trade is not None:
                trade_is_live = await self._ensure_trade_still_open_on_exchange(
                    context=context,
                    trade=related_trade,
                    reason="followup_exchange_position_missing",
                )
                if not trade_is_live:
                    if not related_trade.is_open:
                        trace.debug_notes.append(
                            "related_signal_detached_confirmed_exchange_position_missing="
                            f"{related_trade.trade_id}"
                        )
                        related_signal_id = None
                        trace.correlation_method = (
                            "new_signal"
                            if parsed.action is SignalAction.OPEN
                            else "closed_exchange_trade_detached"
                        )
                        trace.correlation_note = (
                            "related_trade_auto_closed_after_confirmed_missing_"
                            "exchange_position"
                        )
                        self._log_event(
                            logging.INFO,
                            "live_trading.related_signal_detached_after_exchange_close",
                            signal_id=related_trade.signal_id,
                            **self._trade_log_fields(related_trade),
                            **self._message_log_fields(message),
                        )
                    else:
                        trace.debug_notes.append(
                            "related_signal_blocked_exchange_reconciliation_required="
                            f"{related_trade.trade_id}"
                        )
                        trace.signal_id = related_signal_id
                        trace.trade_id = related_trade.trade_id
                        trace.correlation_method = "exchange_reconciliation_required"
                        trace.correlation_note = "related_trade_state_is_not_authoritative"
                        trace.final_status = "exchange_reconciliation_required"
                        trace.effect_summary = (
                            f"Trade {related_trade.trade_id} requires exchange reconciliation; "
                            "message was not applied"
                        )
                        self._push_trace(trace)
                        self.store.save_session(self.session)
                        self._emit_session_update()
                        self._log_event(
                            logging.CRITICAL,
                            "live_trading.message_blocked_exchange_reconciliation_required",
                            signal_id=related_signal_id,
                            **self._trade_log_fields(related_trade),
                            **self._message_log_fields(message),
                        )
                        return
        promoted_reason = self._unmatched_visual_followup_promotion_reason(
            message=message,
            parsed=parsed,
            related_signal_id=related_signal_id,
        )
        if promoted_reason is not None:
            promoted_from_action = parsed.action
            parsed = parsed.model_copy(update={"action": SignalAction.OPEN})
            trace.parsed_action = parsed.action.value
            trace.correlation_method = "promoted_visual_signal"
            trace.correlation_note = promoted_reason
            trace.debug_notes = [
                note for note in trace.debug_notes if note != "no_signal_for_followup"
            ]
            trace.debug_notes.append(f"promoted_visual_signal_from={promoted_from_action.value}")
            self._log_event(
                logging.INFO,
                "live_trading.unmatched_visual_followup_promoted",
                promoted_from_action=promoted_from_action.value,
                promoted_to_action=parsed.action.value,
                promotion_reason=promoted_reason,
                **self._message_log_fields(message),
                **self._parsed_signal_log_fields(parsed),
            )
        if parsed.action is SignalAction.OPEN and related_signal_id is None:
            if trace.correlation_method != "promoted_visual_signal":
                trace.correlation_method = "new_signal"
                trace.correlation_note = None
            trace.debug_notes = [
                note for note in trace.debug_notes if note != "no_signal_for_followup"
            ]
        related_trade_for_directive = (
            self._open_trades.get(related_signal_id) if related_signal_id is not None else None
        )
        percentage_tps = detect_percentage_tp_update(message.text)
        if (
            related_trade_for_directive is not None
            and parsed.action is SignalAction.UNKNOWN
            and percentage_tps
        ):
            multiplier = Decimal("100") * Decimal(str(max(1, related_trade_for_directive.leverage)))
            prices = [
                (
                    related_trade_for_directive.entry_price
                    * (Decimal("1") + pct / multiplier)
                    if related_trade_for_directive.side == TradeSide.LONG.value
                    else related_trade_for_directive.entry_price
                    * (Decimal("1") - pct / multiplier)
                ).quantize(Decimal("0.00000001"))
                for pct in percentage_tps
            ]
            parsed = parsed.model_copy(
                update={
                    "action": SignalAction.UPDATE_TP,
                    "take_profits": prices,
                    "stop_loss": detect_stop_loss_value(message.text),
                }
            )
            trace.debug_notes.append(
                "percentage_tp_update_converted="
                + ",".join(str(price) for price in prices)
            )
        self._log_event(
            logging.INFO,
            "live_trading.message_correlated",
            related_signal_id=related_signal_id,
            correlation_method=trace.correlation_method,
            correlation_note=trace.correlation_note,
            debug_notes=trace.debug_notes,
            **self._message_log_fields(message),
            **self._parsed_signal_log_fields(parsed),
        )

        # ── Route by effective action ─────────────────────────────────────────
        if parsed.action in (SignalAction.IGNORE, SignalAction.UNKNOWN):
            # Still ambiguous after all upgrades → drop
            trace.final_status = "ignored"
            trace.effect_summary = "Message ignored by classifier"
            self._push_trace(trace)
            self.store.save_session(self.session)
            self._emit_session_update()
            self._log_event(
                logging.INFO,
                "live_trading.message_ignored",
                final_status=trace.final_status,
                effect_summary=trace.effect_summary,
                **self._message_log_fields(message),
            )
            self._log_event(
                logging.INFO,
                "live_trading.message_processing_completed",
                final_status=trace.final_status,
                effect_summary=trace.effect_summary,
                signal_id=trace.signal_id,
                trade_id=trace.trade_id,
                impact_notes=trace.impact_notes,
                **self._message_log_fields(message),
            )
            return

        # close_all closes EVERY open trade regardless of correlation (matches backtest simulator)
        if close_all_detected and parsed.action is SignalAction.CLOSE:
            await self._handle_close_all_trades(message, trace, channel_label)
            self._push_trace(trace)
            self.store.save_session(self.session)
            self._emit_session_update()
            self._log_event(
                logging.INFO,
                "live_trading.close_all_processed",
                final_status=trace.final_status,
                effect_summary=trace.effect_summary,
                **self._message_log_fields(message),
            )
            self._log_event(
                logging.INFO,
                "live_trading.message_processing_completed",
                final_status=trace.final_status,
                effect_summary=trace.effect_summary,
                signal_id=trace.signal_id,
                trade_id=trace.trade_id,
                impact_notes=trace.impact_notes,
                **self._message_log_fields(message),
            )
            return

        if parsed.action is SignalAction.OPEN and related_signal_id is None:
            # Brand-new signal — queue for consolidation
            signal_id = make_signal_id(message.channel_id, message.message_id)
            state = SignalState(
                signal_id=signal_id,
                channel_id=message.channel_id,
                status=SignalStatus.PENDING_CONSOLIDATION,
                created_from_message_id=message.message_id,
                related_message_ids=[message.message_id],
                current_signal=parsed,
                version=1,
                created_at=_utc_now(),
                updated_at=_utc_now(),
                expires_at=None,
            )
            context.add_signal(state, pending=True)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self.session.total_signals_received += 1
            trace.signal_id = signal_id
            trace.final_status = "pending_consolidation"
            trace.effect_summary = (
                f"New {parsed.side.value.upper()} signal on {parsed.symbol} — "
                f"waiting {self.settings.SIGNAL_CONSOLIDATION_SECONDS}s consolidation"
            )
            self._log_event(
                logging.INFO,
                "live_trading.signal_queued_for_consolidation",
                signal_id=signal_id,
                consolidation_seconds=self.settings.SIGNAL_CONSOLIDATION_SECONDS,
                **self._message_log_fields(message),
                **self._parsed_signal_log_fields(parsed),
            )

        elif parsed.action is SignalAction.OPEN and related_signal_id is not None:
            # AI says OPEN but message is correlated with an existing signal.
            # normalize_related_signal_action decides if it's really SL/TP/leverage update.
            normalized_action = normalize_related_signal_action(parsed, is_related=True)
            if (
                normalized_action is not SignalAction.OPEN
                and related_signal_id in self._open_trades
            ):
                # Actually a parameter update for an already-open trade
                parsed_followup = parsed.model_copy(update={"action": normalized_action})
                effective_action = apply_text_directive_action(normalized_action, message.text)
                if effective_action is not normalized_action:
                    parsed_followup = parsed_followup.model_copy(
                        update={"action": effective_action}
                    )
                context.attach_message(related_signal_id, message)
                if effective_action in {
                    SignalAction.UPDATE_SL,
                    SignalAction.UPDATE_TP,
                    SignalAction.UPDATE_LEVERAGE,
                    SignalAction.UPDATE_ENTRY,
                }:
                    context.merge_signal(related_signal_id, parsed_followup, message.date)
                trace.signal_id = related_signal_id
                await self._handle_followup(
                    signal_id=related_signal_id,
                    parsed=parsed_followup,
                    message=message,
                    context=context,
                    trace=trace,
                )
            else:
                # Still OPEN (entry update) or trade not yet open → merge pending
                existing = context.get_signal(related_signal_id)
                if existing is not None:
                    context.merge_signal(related_signal_id, parsed, message.date)
                    context.attach_message(related_signal_id, message)
                    self._sync_signal_snapshot(
                        context=context,
                        state=existing,
                        trade=self._open_trades.get(related_signal_id),
                    )
                trace.signal_id = related_signal_id
                trace.final_status = "signal_updated"
                trace.effect_summary = f"Updated pending signal {related_signal_id}"

        else:
            # Follow-up directive (CLOSE, UPDATE_SL, UPDATE_TP, CANCEL, etc.)
            if related_signal_id is not None:
                trace.signal_id = related_signal_id
                context.attach_message(related_signal_id, message)
                existing = context.get_signal(related_signal_id)
                is_pending_signal = (
                    existing is not None
                    and related_signal_id not in self._open_trades
                    and existing.status is SignalStatus.PENDING_CONSOLIDATION
                )
                if is_pending_signal and parsed.action in {
                    SignalAction.UPDATE_SL,
                    SignalAction.UPDATE_TP,
                    SignalAction.UPDATE_LEVERAGE,
                    SignalAction.UPDATE_ENTRY,
                }:
                    assert existing is not None
                    pending_action = (
                        existing.current_signal.action
                        if existing.current_signal is not None
                        else SignalAction.OPEN
                    )
                    context.merge_signal(
                        related_signal_id,
                        parsed.model_copy(update={"action": pending_action}),
                        message.date,
                    )
                    self._sync_signal_snapshot(
                        context=context,
                        state=existing,
                        trade=None,
                    )
                    trace.final_status = "signal_updated"
                    trace.effect_summary = f"Updated pending signal {related_signal_id}"
                elif parsed.action in {
                    SignalAction.UPDATE_SL,
                    SignalAction.UPDATE_TP,
                    SignalAction.UPDATE_LEVERAGE,
                    SignalAction.UPDATE_ENTRY,
                }:
                    context.merge_signal(related_signal_id, parsed, message.date)
                    await self._handle_followup(
                        signal_id=related_signal_id,
                        parsed=parsed,
                        message=message,
                        context=context,
                        trace=trace,
                    )
                else:
                    await self._handle_followup(
                        signal_id=related_signal_id,
                        parsed=parsed,
                        message=message,
                        context=context,
                        trace=trace,
                    )
            else:
                trace.final_status = "no_match"
                trace.effect_summary = "Follow-up with no matching open signal"
                self._log_event(
                    logging.INFO,
                    "live_trading.followup_unmatched",
                    final_status=trace.final_status,
                    effect_summary=trace.effect_summary,
                    **self._message_log_fields(message),
                    **self._parsed_signal_log_fields(parsed),
                )

        self._push_trace(trace)
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(
            logging.INFO,
            "live_trading.message_processing_completed",
            final_status=trace.final_status,
            effect_summary=trace.effect_summary,
            signal_id=trace.signal_id,
            trade_id=trace.trade_id,
            impact_notes=trace.impact_notes,
            **self._message_log_fields(message),
        )

    async def _prepare_message_for_classification(
        self,
        *,
        message: RawTelegramMessage,
        context: ChannelContext,
        trace: LiveMessageTrace,
    ) -> RawTelegramMessage:
        payload = message.raw_payload
        needs_caption_media = bool(payload.get("has_media")) and bool(
            payload.get("caption_present")
        )
        if not needs_caption_media:
            return await self._maybe_attach_prior_captionless_media(
                message=message,
                context=context,
                trace=trace,
            )
        if self._telegram_client is None:
            return message
        try:
            hydrated = await self._telegram_client.ensure_media_payload(message)
        except Exception as exc:
            trace.debug_notes.append(f"caption_media_download_failed={type(exc).__name__}")
            return message
        payload = hydrated.raw_payload
        if payload.get("media_downloaded") is True:
            trace.debug_notes.append(
                f"caption_media_downloaded={len(payload.get('image_data_urls') or [])}"
            )
        return await self._maybe_attach_prior_captionless_media(
            message=hydrated,
            context=context,
            trace=trace,
        )

    async def _maybe_attach_prior_captionless_media(
        self,
        *,
        message: RawTelegramMessage,
        context: ChannelContext,
        trace: LiveMessageTrace,
    ) -> RawTelegramMessage:
        if not self._message_needs_retro_media_context(message):
            return message
        prior = self._find_recent_captionless_media_message(
            context=context,
            message=message,
        )
        if prior is None:
            return message
        prior_payload = prior.raw_payload
        if prior_payload.get("image_data_urls"):
            return self._attach_context_images(message, prior, trace)
        if self._telegram_client is None:
            return message
        try:
            hydrated_prior = await self._telegram_client.ensure_media_payload(
                prior,
                allow_captionless=True,
            )
        except Exception as exc:
            trace.debug_notes.append(f"retro_media_download_failed={type(exc).__name__}")
            return message
        if not hydrated_prior.raw_payload.get("image_data_urls"):
            trace.debug_notes.append(f"retro_media_unavailable={prior.message_id}")
            return message
        return self._attach_context_images(message, hydrated_prior, trace)

    @staticmethod
    def _message_needs_retro_media_context(message: RawTelegramMessage) -> bool:
        text = (message.text or "").strip().lower()
        if not text:
            return False
        payload = message.raw_payload
        if payload.get("image_data_urls"):
            return False
        if bool(payload.get("has_media")) and bool(payload.get("caption_present")):
            return False
        return any(marker in text for marker in _RETRO_SIGNAL_TEXT_MARKERS)

    @staticmethod
    def _find_recent_captionless_media_message(
        *,
        context: ChannelContext,
        message: RawTelegramMessage,
    ) -> RawTelegramMessage | None:
        recent = list(context.recent_messages)
        for candidate in reversed(recent[-3:]):
            if candidate.message_id >= message.message_id:
                continue
            payload = candidate.raw_payload
            if not bool(payload.get("has_media")):
                continue
            if bool(payload.get("caption_present")):
                continue
            if (candidate.text or "").strip():
                continue
            return candidate
        return None

    @staticmethod
    def _attach_context_images(
        message: RawTelegramMessage,
        prior_media: RawTelegramMessage,
        trace: LiveMessageTrace,
    ) -> RawTelegramMessage:
        message_payload = dict(message.raw_payload)
        context_images = prior_media.raw_payload.get("image_data_urls")
        if not isinstance(context_images, list) or not context_images:
            return message
        merged_images = list(message_payload.get("image_data_urls") or [])
        merged_images.extend(context_images)
        message_payload["image_data_urls"] = merged_images
        message_payload["context_image_message_ids"] = list(
            dict.fromkeys(
                [
                    *(message_payload.get("context_image_message_ids") or []),
                    prior_media.message_id,
                ]
            )
        )
        trace.debug_notes.append(f"retro_media_context_from={prior_media.message_id}")
        return message.model_copy(update={"raw_payload": message_payload})

    @staticmethod
    def _message_has_visual_signal_context(message: RawTelegramMessage) -> bool:
        payload = message.raw_payload
        return bool(payload.get("image_data_urls")) or bool(
            payload.get("context_image_message_ids")
        )

    @staticmethod
    def _parsed_signal_has_openable_visual_details(parsed: ParsedSignal) -> bool:
        return any(
            (
                parsed.stop_loss is not None,
                bool(parsed.take_profits),
                parsed.entry_low is not None,
                parsed.entry_high is not None,
                parsed.leverage is not None,
            )
        )

    def _unmatched_visual_followup_promotion_reason(
        self,
        *,
        message: RawTelegramMessage,
        parsed: ParsedSignal,
        related_signal_id: str | None,
    ) -> str | None:
        if related_signal_id is not None:
            return None
        if parsed.action not in _VISUAL_SIGNAL_PROMOTABLE_ACTIONS:
            return None
        if not self._message_has_visual_signal_context(message):
            return None
        if not parsed.symbol or parsed.side is TradeSide.UNKNOWN:
            return None
        if not self._parsed_signal_has_openable_visual_details(parsed):
            return None
        payload = message.raw_payload
        evidence: list[str] = []
        if bool(payload.get("caption_present")) and bool(payload.get("has_media")):
            evidence.append("caption_media")
        if payload.get("context_image_message_ids"):
            evidence.append("retro_media_context")
        if payload.get("image_data_urls"):
            evidence.append(f"image_count={len(payload.get('image_data_urls') or [])}")
        if parsed.stop_loss is not None:
            evidence.append("stop_loss")
        if parsed.take_profits:
            evidence.append(f"take_profit_count={len(parsed.take_profits)}")
        if parsed.entry_low is not None or parsed.entry_high is not None:
            evidence.append("entry")
        if parsed.leverage is not None:
            evidence.append("leverage")
        return "unmatched_visual_followup_with_openable_signal:" + ",".join(evidence)

    # ── Close-All Handler ─────────────────────────────────────────────────

    async def _handle_close_all_trades(
        self,
        message: RawTelegramMessage,
        trace: LiveMessageTrace,
        channel_label: str,
    ) -> None:
        """Close every open trade — matches BacktestSimulator close_all behavior."""
        self._log_event(
            logging.INFO,
            "live_trading.close_all_started",
            open_trade_count=len(self._open_trades),
            **self._message_log_fields(message),
        )
        if not self._open_trades:
            trace.final_status = "no_open_positions"
            trace.effect_summary = "Close-all detected but no open trades"
            return

        assert self._pm is not None
        fee_rate = self.settings.LIVE_TRADING_FEE_RATE_PCT
        closed_count = 0
        for trade in list(self._open_trades.values()):
            attribution = MessageAttribution(
                message_id=message.message_id,
                channel_id=message.channel_id,
                channel_label=channel_label,
                message_preview=(message.text or "")[:200],
                message_date=message.date,
                action="closed",
            )
            if trade.is_waiting_entry:
                try:
                    await self._cancel_waiting_entry(
                        trade=trade,
                        reason="manual_close_all_pending_entry",
                        signal_status=SignalStatus.CANCELLED,
                    )
                except Exception:
                    continue
                closed_count += 1
                continue
            if self._uses_exchange_execution():
                try:
                    await self._execute_exchange_close(
                        trade=trade,
                        fraction=Decimal("1"),
                        reason="manual_close_all",
                        message=attribution,
                    )
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    self._log_event(
                        logging.WARNING,
                        "live_trading.close_all_trade_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **self._trade_log_fields(trade),
                    )
                    continue
            else:
                mark = await self._get_mark_price(trade.symbol)
                close_price = mark if mark else trade.entry_price
                self._pm.close_trade(
                    trade=trade,
                    close_price=close_price,
                    reason="manual_close_all",
                    fee_rate_pct=fee_rate,
                    message=attribution,
                )
            self._finalize_closed_trade(trade)
            context = self._contexts.get(trade.channel_id)
            if context is not None:
                self._mark_signal_terminal(
                    context=context,
                    signal_id=trade.signal_id,
                    status=SignalStatus.CLOSED,
                    trade=trade,
                )
            closed_count += 1

        trace.final_status = "closed_all_trades"
        trace.effect_summary = f"Close-all: closed {closed_count} trade(s)"
        self._log_event(
            logging.INFO,
            "live_trading.close_all_completed",
            closed_count=closed_count,
            **self._message_log_fields(message),
        )

    # ── Follow-up Handler ──────────────────────────────────────────────────

    async def _handle_followup(
        self,
        *,
        signal_id: str,
        parsed: ParsedSignal,
        message: RawTelegramMessage,
        context: ChannelContext,
        trace: LiveMessageTrace,
    ) -> None:
        trade = self._open_trades.get(signal_id)
        if trade is None:
            trace.final_status = "no_open_trade"
            trace.effect_summary = f"Signal {signal_id} has no open trade to update"
            self._log_event(
                logging.INFO,
                "live_trading.followup_ignored_no_open_trade",
                signal_id=signal_id,
                **self._message_log_fields(message),
                **self._parsed_signal_log_fields(parsed),
            )
            return
        if self._uses_exchange_execution():
            trade_is_live = await self._ensure_trade_still_open_on_exchange(
                context=context,
                trade=trade,
                reason="followup_exchange_position_missing",
            )
            if not trade_is_live:
                if not trade.is_open:
                    trace.final_status = "no_open_trade"
                    trace.effect_summary = (
                        f"Trade {trade.trade_id} was already closed on the exchange"
                    )
                    self._log_event(
                        logging.INFO,
                        "live_trading.followup_ignored_exchange_trade_closed",
                        signal_id=signal_id,
                        **self._trade_log_fields(trade),
                        **self._message_log_fields(message),
                    )
                else:
                    trace.final_status = "exchange_reconciliation_required"
                    trace.effect_summary = (
                        f"Trade {trade.trade_id} requires exchange reconciliation; "
                        "follow-up was not applied"
                    )
                    self._log_event(
                        logging.CRITICAL,
                        "live_trading.followup_blocked_exchange_reconciliation_required",
                        signal_id=signal_id,
                        **self._trade_log_fields(trade),
                        **self._message_log_fields(message),
                    )
                return

        channel_label = self._channel_label(message.channel_id)
        attribution = MessageAttribution(
            message_id=message.message_id,
            channel_id=message.channel_id,
            channel_label=channel_label,
            message_preview=(message.text or "")[:200],
            message_date=message.date,
            action="update",
        )

        close_all = detect_close_all_instruction(message.text)
        move_to_entry = detect_move_stop_to_entry(message.text)
        close_fraction_raw = extract_close_fraction(message.text)
        effective_action = apply_text_directive_action(parsed.action, message.text)
        self._log_event(
            logging.INFO,
            "live_trading.followup_processing",
            signal_id=signal_id,
            requested_action=parsed.action.value,
            effective_action=effective_action.value,
            move_to_entry=move_to_entry,
            close_all=close_all,
            close_fraction=str(close_fraction_raw) if close_fraction_raw is not None else None,
            **self._trade_log_fields(trade),
            **self._message_log_fields(message),
        )

        assert self._pm is not None
        fee_rate = self.settings.LIVE_TRADING_FEE_RATE_PCT

        if trade.is_waiting_entry and effective_action is SignalAction.UPDATE_ENTRY:
            try:
                replacement_result = await self._replace_pending_entry_order(
                    trade=trade,
                    parsed=parsed,
                )
            except Exception as exc:
                trade.last_exchange_sync_error = str(exc)
                trace.final_status = "update_entry_failed"
                trace.effect_summary = f"Pending entry update failed safely: {exc}"
            else:
                trace.final_status = (
                    "opened_trade" if not trade.is_waiting_entry else "updated_entry_order"
                )
                trace.effect_summary = replacement_result
        elif effective_action is SignalAction.CLOSE or close_all:
            fraction = close_fraction_raw or Decimal("1")
            if trade.is_waiting_entry:
                try:
                    await self._cancel_waiting_entry(
                        trade=trade,
                        reason="manual_close_pending_entry",
                        signal_status=SignalStatus.CANCELLED,
                    )
                except Exception as exc:
                    trace.final_status = "cancel_failed"
                    trace.effect_summary = f"Pending entry cancel failed: {exc}"
                else:
                    trace.final_status = "cancelled_entry_order"
                    trace.effect_summary = f"Cancelled pending entry for {trade.symbol}"
                trace.trade_id = trade.trade_id
                return
            if self._uses_exchange_execution():
                try:
                    result = await self._execute_exchange_close(
                        trade=trade,
                        fraction=fraction,
                        reason="manual_close",
                        message=attribution,
                    )
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    trace.final_status = "close_failed"
                    trace.effect_summary = f"Exchange close failed: {exc}"
                else:
                    if not trade.is_open:
                        self._finalize_closed_trade(trade)
                        self._mark_signal_terminal(
                            context=context,
                            signal_id=signal_id,
                            status=SignalStatus.CLOSED,
                            trade=trade,
                        )
                        trace.final_status = "closed_trade"
                        trace.effect_summary = (
                            f"Closed {trade.symbol} @ {result.avg_price}, "
                            f"PnL={result.realized_pnl:.8f}"
                        )
                    else:
                        trace.final_status = "partial_close"
                        trace.effect_summary = (
                            f"Partial close executed for {trade.symbol} @ {result.avg_price}"
                        )
            else:
                mark_price = await self._get_mark_price(trade.symbol)
                close_price = mark_price if mark_price else trade.entry_price

                if fraction >= Decimal("1") or close_all:
                    pnl = self._pm.close_trade(
                        trade=trade,
                        close_price=close_price,
                        reason="manual_close",
                        fee_rate_pct=fee_rate,
                        message=attribution,
                    )
                    self._finalize_closed_trade(trade)
                    self._mark_signal_terminal(
                        context=context,
                        signal_id=signal_id,
                        status=SignalStatus.CLOSED,
                        trade=trade,
                    )
                    trace.final_status = "closed_trade"
                    trace.effect_summary = f"Closed {trade.symbol} @ {close_price}, PnL={pnl:.4f}"
                else:
                    before_realized = trade.realized_pnl
                    self._pm.apply_partial_close(
                        trade=trade,
                        close_fraction=fraction,
                        close_price=close_price,
                        reason=f"partial_{int(fraction * 100)}pct",
                        fee_rate_pct=fee_rate,
                        message=attribution,
                    )
                    self._book_trade_realized_totals(trade)
                    if self.session.trading_mode == "demo":
                        self.session.paper_balance += trade.realized_pnl - before_realized
                    trace.final_status = "partial_close"
                    trace.effect_summary = (
                        f"Partial close {int(fraction * 100)}% of {trade.symbol} @ {close_price}"
                    )

        elif effective_action is SignalAction.CANCEL:
            if trade.is_waiting_entry:
                try:
                    await self._cancel_waiting_entry(
                        trade=trade,
                        reason="signal_cancelled_before_entry",
                        signal_status=SignalStatus.CANCELLED,
                    )
                except Exception as exc:
                    trace.final_status = "cancel_failed"
                    trace.effect_summary = f"Pending entry cancel failed: {exc}"
                else:
                    trace.final_status = "cancelled_entry_order"
                    trace.effect_summary = f"Cancelled pending entry for {trade.symbol}"
                trace.trade_id = trade.trade_id
                return
            if self._uses_exchange_execution():
                try:
                    result = await self._execute_exchange_close(
                        trade=trade,
                        fraction=Decimal("1"),
                        reason="cancelled",
                        message=attribution,
                    )
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    trace.final_status = "cancel_failed"
                    trace.effect_summary = f"Exchange cancel-close failed: {exc}"
                else:
                    if trade.is_open:
                        trace.final_status = "partial_close"
                        trace.effect_summary = (
                            "Cancel instruction partially closed "
                            f"{trade.symbol} @ {result.avg_price}"
                        )
                    else:
                        self._finalize_closed_trade(trade)
                        self._mark_signal_terminal(
                            context=context,
                            signal_id=signal_id,
                            status=SignalStatus.CANCELLED,
                            trade=trade,
                        )
                        trace.final_status = "cancelled_trade"
                        trace.effect_summary = (
                            f"Cancelled {trade.symbol} @ {result.avg_price}, "
                            f"PnL={result.realized_pnl:.8f}"
                        )
            else:
                mark_price = await self._get_mark_price(trade.symbol)
                close_price = mark_price if mark_price else trade.entry_price
                pnl = self._pm.close_trade(
                    trade=trade,
                    close_price=close_price,
                    reason="cancelled",
                    fee_rate_pct=fee_rate,
                    message=attribution,
                )
                self._finalize_closed_trade(trade)
                self._mark_signal_terminal(
                    context=context,
                    signal_id=signal_id,
                    status=SignalStatus.CANCELLED,
                    trade=trade,
                )
                trace.final_status = "cancelled_trade"
                trace.effect_summary = f"Cancelled {trade.symbol} @ {close_price}, PnL={pnl:.4f}"

        elif effective_action is SignalAction.UPDATE_SL or move_to_entry:
            if trade.is_waiting_entry and move_to_entry and parsed.stop_loss is None:
                trace.final_status = "move_sl_to_entry_ignored"
                trace.effect_summary = "Breakeven ignored because entry has not filled"
                trace.trade_id = trade.trade_id
                return
            previous_stop_loss = trade.stop_loss
            previous_take_profits = list(trade.take_profits)
            stop_update = self._pm.update_stop_loss(
                trade=trade,
                new_sl=parsed.stop_loss,
                message=attribution,
                move_to_entry=move_to_entry,
            )
            if stop_update is not None and stop_update.was_capped:
                trace.final_status = "unsafe_sl_update_rejected"
                trace.effect_summary = (
                    "SL update rejected because it exceeds the position risk budget; "
                    f"existing SL remains {previous_stop_loss}"
                )
                trace.trade_id = trade.trade_id
                trace.impact_notes = list(attribution.notes)
                self.store.save_trade(trade)
                state = context.get_signal(signal_id)
                if state is not None:
                    self._sync_signal_snapshot(
                        context=context,
                        state=state,
                        trade=trade,
                    )
                return
            if trade.is_waiting_entry and parsed.stop_loss is not None:
                trade.requested_stop_loss = parsed.stop_loss
            if self._uses_exchange_execution() and not trade.is_waiting_entry:
                try:
                    await self._sync_trade_protection(
                        trade,
                        refresh_take_profits=False,
                        refresh_stop_loss=True,
                    )
                except Exception as exc:
                    trade.stop_loss = previous_stop_loss
                    trade.last_exchange_sync_error = str(exc)
                    attribution.notes.append(f"exchange_trading_stop_failed={exc}")
                    attribution.notes.append(f"local_sl_reverted_to={previous_stop_loss}")
                    restored = await self._restore_previous_trade_protection(
                        trade=trade,
                        previous_stop_loss=previous_stop_loss,
                        previous_take_profits=previous_take_profits,
                        original_error=exc,
                        attribution=attribution,
                        sync_context="update_sl",
                    )
                    if restored:
                        attribution.notes.append("exchange_previous_sl_restored")
                    trace.final_status = "update_sl_failed"
                    trace.effect_summary = (
                        f"SL update failed on exchange: {exc}"
                        if restored
                        else "SL update failed; fail-closed protection handling executed"
                    )
            new_sl = trade.stop_loss
            if trace.final_status != "update_sl_failed":
                trace.final_status = "updated_sl"
                trace.effect_summary = (
                    "SL moved to entry (breakeven)" if move_to_entry else f"SL updated to {new_sl}"
                )

        elif effective_action is SignalAction.UPDATE_TP:
            # Try AI-parsed TPs first, then extract from text
            new_tps = parsed.take_profits or detect_tp_list_update(message.text)
            previous_stop_loss = trade.stop_loss
            previous_take_profits = list(trade.take_profits)
            tp_updates_applied: list[str] = []
            take_profits_updated = False
            if parsed.stop_loss is not None:
                stop_update = self._pm.update_stop_loss(
                    trade=trade,
                    new_sl=parsed.stop_loss,
                    message=attribution,
                    move_to_entry=False,
                )
                if stop_update is not None and stop_update.was_capped:
                    trace.final_status = "unsafe_sl_update_rejected"
                    trace.effect_summary = (
                        "Combined TP/SL update rejected because the SL exceeds "
                        "the position risk budget"
                    )
                    trace.trade_id = trade.trade_id
                    trace.impact_notes = list(attribution.notes)
                    self.store.save_trade(trade)
                    state = context.get_signal(signal_id)
                    if state is not None:
                        self._sync_signal_snapshot(
                            context=context,
                            state=state,
                            trade=trade,
                        )
                    return
                if trade.is_waiting_entry:
                    trade.requested_stop_loss = parsed.stop_loss
                tp_updates_applied.append(f"SL={trade.stop_loss}")
            if new_tps:
                take_profits_updated = self._pm.update_take_profits(
                    trade=trade,
                    new_tps=new_tps,
                    message=attribution,
                )
                if take_profits_updated:
                    if trade.is_waiting_entry:
                        trade.requested_take_profits = list(new_tps)
                    tp_updates_applied.append(
                        "TPs=" + str([str(t) for t in trade.take_profits[trade.targets_hit :]])
                    )
            if tp_updates_applied:
                protection_update_failed = False
                if self._uses_exchange_execution() and not trade.is_waiting_entry:
                    try:
                        if take_profits_updated and parsed.stop_loss is not None:
                            await self._sync_trade_protection(trade)
                        else:
                            await self._sync_trade_protection(
                                trade,
                                refresh_take_profits=(
                                    take_profits_updated
                                    or not trade.take_profits[trade.targets_hit :]
                                ),
                                refresh_stop_loss=parsed.stop_loss is not None,
                            )
                    except Exception as exc:
                        trade.stop_loss = previous_stop_loss
                        trade.take_profits = previous_take_profits
                        trade.last_exchange_sync_error = str(exc)
                        attribution.notes.append(f"exchange_trading_stop_failed={exc}")
                        restored = await self._restore_previous_trade_protection(
                            trade=trade,
                            previous_stop_loss=previous_stop_loss,
                            previous_take_profits=previous_take_profits,
                            original_error=exc,
                            attribution=attribution,
                            sync_context="update_tp",
                        )
                        if restored:
                            attribution.notes.append("exchange_previous_protection_restored")
                        else:
                            protection_update_failed = True
                trace.final_status = (
                    "update_tp_failed_fail_closed" if protection_update_failed else "updated_tp"
                )
                trace.effect_summary = (
                    "TP update failed; fail-closed protection handling executed"
                    if protection_update_failed
                    else " · ".join(tp_updates_applied)
                )
            else:
                trace.final_status = "invalid_tp_update_ignored"
                trace.effect_summary = "Invalid TP update ignored; existing targets retained"
        elif effective_action is SignalAction.UPDATE_LEVERAGE:
            updated = self._pm.update_leverage(
                trade=trade,
                new_leverage=parsed.leverage,
                message=attribution,
            )
            if updated:
                trace.final_status = "updated_leverage"
                trace.effect_summary = f"Leverage updated to {trade.leverage}x"
                if self._uses_exchange_execution():
                    assert self._futures_client is not None
                    try:
                        await self._futures_client.set_leverage(
                            trade.symbol,
                            trade.leverage,
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                    except Exception as exc:
                        trade.last_exchange_sync_error = str(exc)
                        attribution.notes.append(f"exchange_set_leverage_failed={exc}")
            else:
                trace.final_status = "no_leverage_found"
                trace.effect_summary = "UPDATE_LEVERAGE but no leverage value extracted"
        elif effective_action is SignalAction.UPDATE_ENTRY:
            updates_applied: list[str] = []
            protection_needs_sync = False
            previous_stop_loss = trade.stop_loss
            previous_take_profits = list(trade.take_profits)
            if parsed.stop_loss is not None:
                self._pm.update_stop_loss(
                    trade=trade,
                    new_sl=parsed.stop_loss,
                    message=attribution,
                    move_to_entry=False,
                )
                updates_applied.append(f"SL={trade.stop_loss}")
                protection_needs_sync = True
            if parsed.take_profits:
                self._pm.update_take_profits(
                    trade=trade,
                    new_tps=parsed.take_profits,
                    message=attribution,
                )
                updates_applied.append(
                    "TPs=" + ",".join(str(item) for item in trade.take_profits[trade.targets_hit :])
                )
                protection_needs_sync = True
            leverage_updated = self._pm.update_leverage(
                trade=trade,
                new_leverage=parsed.leverage,
                message=attribution,
            )
            if leverage_updated:
                updates_applied.append(f"Leverage={trade.leverage}x")
                if self._uses_exchange_execution():
                    assert self._futures_client is not None
                    try:
                        await self._futures_client.set_leverage(
                            trade.symbol,
                            trade.leverage,
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                    except Exception as exc:
                        trade.last_exchange_sync_error = str(exc)
                        attribution.notes.append(f"exchange_set_leverage_failed={exc}")
            protection_update_failed = False
            if (
                protection_needs_sync
                and self._uses_exchange_execution()
                and not trade.is_waiting_entry
            ):
                try:
                    await self._sync_trade_protection(trade)
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    attribution.notes.append(f"exchange_trading_stop_failed={exc}")
                    trade.stop_loss = previous_stop_loss
                    trade.take_profits = previous_take_profits
                    restored = await self._restore_previous_trade_protection(
                        trade=trade,
                        previous_stop_loss=previous_stop_loss,
                        previous_take_profits=previous_take_profits,
                        original_error=exc,
                        attribution=attribution,
                        sync_context="update_entry",
                    )
                    if restored:
                        attribution.notes.append("exchange_previous_protection_restored")
                    else:
                        protection_update_failed = True
            if not updates_applied:
                updates_applied.append("entry replay ignored for already-open trade")
            trace.final_status = (
                "update_entry_failed_fail_closed" if protection_update_failed else "updated_entry"
            )
            trace.effect_summary = (
                "Entry update failed; fail-closed protection handling executed"
                if trace.final_status == "update_entry_failed_fail_closed"
                else " / ".join(updates_applied)
            )
        else:
            trace.final_status = "unhandled_followup"
            trace.effect_summary = f"Action={effective_action.value} not handled"

        trace.trade_id = trade.trade_id
        trace.impact_notes = list(attribution.notes)
        state = context.get_signal(signal_id)
        if state is not None:
            self._sync_signal_snapshot(context=context, state=state, trade=trade)
        self.store.save_trade(trade)
        self._emit_trade_update(trade)
        self._log_event(
            logging.INFO,
            "live_trading.followup_processed",
            signal_id=signal_id,
            final_status=trace.final_status,
            effect_summary=trace.effect_summary,
            impact_notes=trace.impact_notes,
            **self._trade_log_fields(trade),
        )

    # ── Consolidation Tick ─────────────────────────────────────────────────

    async def _consolidation_tick_loop(self) -> None:
        sleep_seconds = max(1, int(self.settings.SIGNAL_CONSOLIDATION_SECONDS))
        while self._running:
            await asyncio.sleep(sleep_seconds)
            now = _utc_now()
            for _channel_id, context in list(self._contexts.items()):
                for signal_id in list(context.pending_signal_ids):
                    state = context.get_signal(signal_id)
                    if state is None:
                        context.pending_signal_ids.discard(signal_id)
                        continue
                    elapsed = (now - state.created_at).total_seconds()
                    if elapsed >= self.settings.SIGNAL_CONSOLIDATION_SECONDS:
                        await self._try_open_signal(signal_id, state, context)

    async def _try_open_signal(
        self, signal_id: str, state: SignalState, context: ChannelContext
    ) -> None:
        context.pending_signal_ids.discard(signal_id)
        if state.current_signal is None:
            return

        parsed = state.current_signal
        self._log_event(
            logging.INFO,
            "live_trading.signal_open_evaluation_started",
            signal_id=signal_id,
            signal_status=state.status.value,
            **self._parsed_signal_log_fields(parsed),
        )
        parsed, geometry_error = self._validator.normalize_for_execution(parsed)
        normalized_symbol = canonical_market_symbol(parsed.symbol)
        if normalized_symbol is not None and normalized_symbol != parsed.symbol:
            parsed = parsed.model_copy(update={"symbol": normalized_symbol})
            state.current_signal = parsed

        if not parsed.symbol:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._log_event(
                logging.WARNING,
                "live_trading.signal_rejected",
                signal_id=signal_id,
                reason="missing_symbol",
            )
            return

        cooldown = self._stop_cooldown_decision(parsed, state.channel_id)
        if cooldown.blocked:
            state.status = SignalStatus.CANCELLED
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            note = (
                "Blocked by same-channel/symbol/side stop cooldown: "
                f"remaining_seconds={cooldown.remaining_seconds}, "
                f"consecutive_stops={cooldown.consecutive_stops}"
            )
            self._emit_trace_update(
                signal_id=signal_id,
                status="stop_cooldown_blocked",
                note=note,
            )
            self._log_event(
                logging.WARNING,
                "live_trading.signal_blocked_by_stop_cooldown",
                signal_id=signal_id,
                remaining_seconds=cooldown.remaining_seconds,
                consecutive_stops=cooldown.consecutive_stops,
                source_trade_id=cooldown.source_trade_id,
                **self._parsed_signal_log_fields(parsed),
            )
            return

        if self._futures_client is not None:
            try:
                await self._futures_client.validate_symbol_tradable(
                    parsed.symbol,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                state.status = SignalStatus.INVALID
                context.add_signal(state, pending=False)
                self._sync_signal_snapshot(context=context, state=state, trade=None)
                self._emit_trace_update(
                    signal_id=signal_id,
                    status="invalid_symbol",
                    note=str(exc),
                )
                self._log_event(
                    logging.WARNING,
                    "live_trading.signal_rejected",
                    signal_id=signal_id,
                    reason="invalid_exchange_symbol",
                    error=str(exc),
                    **self._parsed_signal_log_fields(parsed),
                )
                return

        # Get current mark price for MARKET entries
        mark_price = await self._get_mark_price(parsed.symbol)
        from triak_trade.domain.enums import EntryType

        if mark_price and (
            parsed.entry_type is EntryType.MARKET
            or (parsed.entry_low is None and parsed.entry_high is None)
        ):
            parsed = parsed.model_copy(update={"entry_low": mark_price, "entry_high": mark_price})
        if geometry_error is not None:
            self._log_event(
                logging.INFO,
                "live_trading.signal_rejected",
                signal_id=signal_id,
                reason="invalid_geometry",
                error=geometry_error,
                **self._parsed_signal_log_fields(parsed),
            )
            state.current_signal = parsed
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(
                signal_id=signal_id,
                status="invalid_geometry",
                note=geometry_error,
            )
            return
        ok, errors = self._validator.validate_for_backtest_open(parsed)
        if not ok:
            self._log_event(
                logging.INFO,
                "live_trading.signal_rejected",
                signal_id=signal_id,
                reason="validator_rejected",
                validation_errors=str(errors),
                **self._parsed_signal_log_fields(parsed),
            )
            state.current_signal = parsed
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(signal_id=signal_id, status="invalid", note=str(errors))
            return

        await self._open_position(
            signal_id=signal_id,
            state=state,
            parsed=parsed,
            context=context,
            current_mark_price=mark_price,
        )

    def _stop_cooldown_decision(
        self,
        parsed: ParsedSignal,
        channel_id: str,
    ) -> _StopCooldownDecision:
        symbol = canonical_market_symbol(parsed.symbol)
        if symbol is None or parsed.side not in {TradeSide.LONG, TradeSide.SHORT}:
            return _StopCooldownDecision(blocked=False)
        base_seconds = max(
            0,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_STOP_COOLDOWN_BASE_SECONDS",
                    3600,
                )
            ),
        )
        max_seconds = max(
            base_seconds,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_STOP_COOLDOWN_MAX_SECONDS",
                    21600,
                )
            ),
        )
        if base_seconds <= 0:
            return _StopCooldownDecision(blocked=False)

        matching = [
            trade
            for trade in self.store.list_closed_trades(
                self.session.session_id,
                limit=200,
            )
            if canonical_market_symbol(trade.symbol) == symbol
            and trade.channel_id == channel_id
            and trade.side == parsed.side.value
            and trade.closed_at is not None
        ]
        matching.sort(
            key=lambda trade: self._as_utc(trade.closed_at or trade.opened_at),
            reverse=True,
        )
        if not matching or not self._is_stop_close_reason(matching[0].close_reason):
            return _StopCooldownDecision(blocked=False)

        consecutive_stops = 0
        for trade in matching:
            if not self._is_stop_close_reason(trade.close_reason):
                break
            consecutive_stops += 1
        latest = matching[0]
        cooldown_seconds = min(
            max_seconds,
            base_seconds * (2 ** max(0, consecutive_stops - 1)),
        )
        elapsed_seconds = max(
            0,
            int((_utc_now() - self._as_utc(latest.closed_at)).total_seconds()),
        )
        remaining_seconds = max(0, cooldown_seconds - elapsed_seconds)
        return _StopCooldownDecision(
            blocked=remaining_seconds > 0,
            remaining_seconds=remaining_seconds,
            consecutive_stops=consecutive_stops,
            source_trade_id=latest.trade_id,
        )

    @staticmethod
    def _is_stop_close_reason(reason: str | None) -> bool:
        normalized = str(reason or "").strip().lower()
        return normalized in {
            "sl_hit",
            "stop_loss_hit",
            "exchange_stop_loss_fill",
        } or normalized.startswith("sl_hit:")

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime:
        if value is None:
            return _utc_now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _normalize_missing_entry_to_market(parsed: ParsedSignal) -> ParsedSignal:
        """Compatibility wrapper for callers that exercised the former helper."""

        return ParsedSignalValidator().normalize_for_execution(parsed)[0]

    @staticmethod
    def _entry_reference(parsed: ParsedSignal) -> Decimal | None:
        return ParsedSignalValidator._entry_reference(parsed)

    def _normalize_or_reject_open_geometry(
        self,
        parsed: ParsedSignal,
    ) -> tuple[ParsedSignal, str | None]:
        """Compatibility wrapper around the shared execution normalizer."""

        return self._validator.normalize_for_execution(parsed)

    async def _open_position(
        self,
        *,
        signal_id: str,
        state: SignalState,
        parsed: ParsedSignal,
        context: ChannelContext,
        current_mark_price: Decimal | None,
    ) -> None:
        assert self._pm is not None and self._strategy is not None
        self._log_event(
            logging.INFO,
            "live_trading.position_open_started",
            signal_id=signal_id,
            open_trade_count=len(self._open_trades),
            **self._parsed_signal_log_fields(parsed),
        )

        if self.settings.KILL_SWITCH_ENABLED or self.session.new_entries_blocked:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(
                signal_id=signal_id,
                status="kill_switch_blocked",
                note=(
                    self.settings.KILL_SWITCH_REASON
                    or self.session.last_error
                    or "Runtime execution health block is active"
                ),
            )
            self._log_event(
                logging.WARNING,
                "live_trading.position_open_blocked",
                signal_id=signal_id,
                reason=("kill_switch" if self.settings.KILL_SWITCH_ENABLED else "runtime_health"),
                note=(
                    self.settings.KILL_SWITCH_REASON
                    or self.session.last_error
                    or "Runtime execution health block is active"
                ),
            )
            return
        if len(self._open_trades) >= self.settings.LIVE_TRADING_MAX_CONCURRENT_POSITIONS:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(
                signal_id=signal_id,
                status="max_positions_blocked",
                note=(
                    "LIVE_TRADING_MAX_CONCURRENT_POSITIONS reached: "
                    f"{self.settings.LIVE_TRADING_MAX_CONCURRENT_POSITIONS}"
                ),
            )
            self._log_event(
                logging.WARNING,
                "live_trading.position_open_blocked",
                signal_id=signal_id,
                reason="max_concurrent_positions",
                max_positions=self.settings.LIVE_TRADING_MAX_CONCURRENT_POSITIONS,
            )
            return

        orig_msg = context.get_message(state.created_from_message_id)
        trigger_id = state.created_from_message_id
        trigger_text = (orig_msg.text or "")[:200] if orig_msg else ""
        trigger_date = orig_msg.date if orig_msg else state.created_at
        channel_label = self._channel_label(state.channel_id)
        channel_input = self._channel_input(state.channel_id)

        current_balance = (
            self._demo_balance_for_sizing()
            if self.session.trading_mode == "demo" and self._uses_exchange_execution()
            else (
                self.session.paper_balance
                if self.session.trading_mode == "demo"
                else self._live_balance_for_sizing()
            )
        )

        try:
            sizing = self._pm.compute_position_sizing(
                session=self.session,
                signal=parsed,
                current_balance=current_balance,
                strategy=self._strategy,
            )
        except ValueError as exc:
            self._log_event(
                logging.WARNING,
                "live_trading.position_open_rejected",
                signal_id=signal_id,
                reason="position_sizing_failed",
                error=str(exc),
                current_balance=str(current_balance),
                **self._parsed_signal_log_fields(parsed),
            )
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(signal_id=signal_id, status="invalid_sizing", note=str(exc))
            return
        self._log_event(
            logging.INFO,
            "live_trading.position_sizing_completed",
            signal_id=signal_id,
            current_balance=str(current_balance),
            sized_quantity=str(sizing.quantity),
            margin=str(sizing.margin),
            leverage=sizing.leverage,
            stop_loss=self._decimal_text(sizing.stop_loss),
            take_profit_count=len(sizing.take_profits),
            notes=sizing.notes,
        )

        trade = self._pm.create_trade(
            session=self.session,
            signal=parsed,
            sizing=sizing,
            trigger_message_id=trigger_id,
            trigger_message_preview=trigger_text,
            trigger_message_date=trigger_date,
            channel_id=state.channel_id,
            channel_input=channel_input,
            channel_label=channel_label,
            signal_id=signal_id,
        )

        # Live mode: place real order
        if self._uses_exchange_execution():
            try:
                await self._real_open_position(
                    trade,
                    current_mark_price=current_mark_price,
                )
            except Exception as exc:
                self._account_coordinator.unregister_trade(trade.trade_id)
                self._log_event(
                    logging.ERROR,
                    "live_trading.real_order_failed",
                    signal_id=signal_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
                trade.status = "closed"
                trade.close_reason = f"order_failed: {exc}"
                trade.closed_at = _utc_now()
                trade.last_exchange_sync_error = str(exc)
                state.status = SignalStatus.INVALID
                context.add_signal(state, pending=False)
                self._sync_signal_snapshot(context=context, state=state, trade=trade)
                self.store.save_trade(trade)
                self._emit_trace_update(signal_id=signal_id, status="order_failed", note=str(exc))
                return
        else:
            trade.status = "open"
            trade.entry_order_type = "PAPER"
            trade.entry_order_status = "FILLED"
            trade.entry_filled_at = _utc_now()

        if not trade.is_open:
            state.status = SignalStatus.CLOSED
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=trade)
            self.store.save_trade(trade)
            self._emit_trade_update(trade)
            self._emit_trace_update(
                signal_id=signal_id,
                status=trade.account_coordination_action or "not_opened",
                note=trade.close_reason or "Account coordination did not open exposure.",
                trade_id=trade.trade_id,
            )
            self._log_event(
                logging.INFO,
                "live_trading.account_open_coordinated_without_new_exposure",
                account_coordination_action=trade.account_coordination_action,
                account_netted_quantity=str(trade.account_netted_quantity),
                consensus_duplicate_trade_id=trade.consensus_duplicate_trade_id,
                **self._trade_log_fields(trade),
            )
            return

        # Register trade
        self._open_trades[signal_id] = trade
        self._register_account_trade(trade)
        state.status = SignalStatus.ORDER_SUBMITTED if trade.is_waiting_entry else SignalStatus.OPEN
        context.add_signal(state, pending=False)
        self._sync_signal_snapshot(context=context, state=state, trade=trade)
        self.session.total_signals_opened += 1
        self.session.open_positions_count += 1
        if self.session.trading_mode == "demo" and not self._uses_exchange_execution():
            self.session.paper_balance -= trade.margin
        self.store.save_trade(trade)
        self.store.save_session(self.session)
        self._emit_trade_update(trade)
        self._emit_session_update()
        self._emit_trace_update(
            signal_id=signal_id,
            status=("entry_order_submitted" if trade.is_waiting_entry else "opened_trade"),
            note=(
                (
                    f"Entry {trade.entry_order_type} submitted for "
                    f"{trade.side.upper()} {trade.symbol} "
                    f"qty={trade.quantity} @ {trade.entry_price} "
                    f"lev={trade.leverage}x"
                )
                if trade.is_waiting_entry
                else (
                    f"Opened {trade.side.upper()} {trade.symbol} "
                    f"qty={trade.quantity} @ {trade.entry_price} "
                    f"lev={trade.leverage}x"
                )
            ),
            trade_id=trade.trade_id,
        )
        self._log_event(
            logging.INFO,
            (
                "live_trading.entry_order_submitted"
                if trade.is_waiting_entry
                else "live_trading.position_opened"
            ),
            signal_id=signal_id,
            channel_id=state.channel_id,
            **self._trade_log_fields(trade),
        )

    # ── Price Refresh ──────────────────────────────────────────────────────

    async def _price_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.LIVE_TRADING_PRICE_REFRESH_SECONDS)
            try:
                await self._refresh_all_prices()
            except Exception as exc:
                self._log_event(
                    logging.ERROR,
                    "live_trading.price_refresh_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    async def _refresh_all_prices(self) -> None:
        if not self._open_trades:
            return
        assert self._pm is not None
        fee_rate = self.settings.LIVE_TRADING_FEE_RATE_PCT

        # Fetch all mark prices in parallel
        symbols = list({t.symbol for t in self._open_trades.values() if t.symbol})
        price_map: dict[str, Decimal] = {}
        for sym in symbols:
            p = await self._get_mark_price(sym)
            if p:
                price_map[sym] = p
        self._log_event(
            logging.DEBUG,
            "live_trading.price_refresh_prices_loaded",
            symbol_count=len(symbols),
            refreshed_symbol_count=len(price_map),
            symbols=symbols,
        )

        to_remove: list[str] = []
        for signal_id, trade in list(self._open_trades.items()):
            mark = price_map.get(trade.symbol)
            if mark is None:
                continue
            if trade.is_waiting_entry:
                trade.mark_price = mark
                trade.unrealized_pnl = Decimal("0")
                self.store.save_trade(trade)
                continue
            self._pm.apply_mark_price(trade=trade, mark_price=mark, fee_rate_pct=fee_rate)
            if self._uses_exchange_execution() and self._trade_has_exchange_protection(trade):
                self.store.save_trade(trade)
                continue

            events = self._pm.check_sl_tp_hit(
                trade=trade,
                mark_price=mark,
                strategy=self._strategy,
                fee_rate_pct=fee_rate,
            )
            for event in events:
                if event.startswith("tp") and "_hit" in event:
                    await self._apply_tp_hit(trade, mark, event)
                    if not trade.is_open:
                        to_remove.append(signal_id)
                    break
                elif event == "sl_hit":
                    if self._uses_exchange_execution():
                        try:
                            await self._execute_exchange_close(
                                trade=trade,
                                fraction=Decimal("1"),
                                reason="sl_hit",
                            )
                        except Exception as exc:
                            trade.last_exchange_sync_error = str(exc)
                            self._log_event(
                                logging.WARNING,
                                "live_trading.exchange_stop_loss_close_failed",
                                error_type=type(exc).__name__,
                                error=str(exc),
                                **self._trade_log_fields(trade),
                            )
                            break
                    else:
                        self._pm.close_trade(
                            trade=trade,
                            close_price=mark,
                            reason="sl_hit",
                            fee_rate_pct=fee_rate,
                        )
                    self._finalize_closed_trade(trade)
                    context = self._contexts.get(trade.channel_id)
                    if context is not None:
                        self._mark_signal_terminal(
                            context=context,
                            signal_id=signal_id,
                            status=SignalStatus.CLOSED,
                            trade=trade,
                        )
                    to_remove.append(signal_id)
                    break

            if not trade.is_open and signal_id not in to_remove:
                to_remove.append(signal_id)
            self.store.save_trade(trade)

        for sid in to_remove:
            self._open_trades.pop(sid, None)

        self.session.total_unrealized_pnl = sum(
            (
                t.exchange_position.unrealized_pnl
                if self._uses_exchange_execution() and t.exchange_position is not None
                else t.unrealized_pnl
                for t in self._open_trades.values()
            ),
            Decimal("0"),
        )
        self.session.last_update_at = _utc_now()
        self.store.save_session(self.session)
        self._emit_session_update()
        self._log_event(
            logging.DEBUG,
            "live_trading.price_refresh_completed",
            open_trade_count=len(self._open_trades),
            removed_trade_count=len(to_remove),
            total_unrealized_pnl=str(self.session.total_unrealized_pnl),
        )

    async def _apply_tp_hit(self, trade: LiveTrade, mark: Decimal, event: str) -> None:
        assert self._pm is not None and self._strategy is not None
        idx = trade.targets_hit
        remaining = len(trade.take_profits) - idx
        action = self._strategy.get_target_hit_action(
            targets_hit_so_far=idx,
            remaining_targets_including_this=remaining,
            entry_price=trade.entry_price,
            take_profits=trade.take_profits,
        )
        fee_rate = self.settings.LIVE_TRADING_FEE_RATE_PCT
        if self._uses_exchange_execution():
            await self._execute_exchange_close(
                trade=trade,
                fraction=action.close_fraction,
                reason=f"tp{idx + 1}_hit",
            )
        else:
            before_realized = trade.realized_pnl
            self._pm.apply_partial_close(
                trade=trade,
                close_fraction=action.close_fraction,
                close_price=mark,
                reason=f"tp{idx + 1}_hit",
                fee_rate_pct=fee_rate,
                is_tp_hit=True,
            )
            self._book_trade_realized_totals(trade)
            if self.session.trading_mode == "demo":
                self.session.paper_balance += trade.realized_pnl - before_realized

        if action.new_stop_loss is not None and trade.stop_loss != action.new_stop_loss:
            trade.stop_loss = action.new_stop_loss
        elif action.move_sl_to_entry and trade.stop_loss != trade.entry_price:
            trade.stop_loss = trade.entry_price
        if self._uses_exchange_execution():
            trade.targets_hit += 1

        context = self._contexts.get(trade.channel_id)
        if context is not None:
            state = context.get_signal(trade.signal_id)
            if state is not None:
                self._sync_signal_snapshot(context=context, state=state, trade=trade)

        if not trade.is_open:
            self._finalize_closed_trade(trade)
            if context is not None:
                self._mark_signal_terminal(
                    context=context,
                    signal_id=trade.signal_id,
                    status=SignalStatus.CLOSED,
                    trade=trade,
                )

    async def _pending_entry_monitor_loop(self) -> None:
        poll_seconds = max(
            1,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_PENDING_ENTRY_POLL_SECONDS",
                    2,
                )
            ),
        )
        while self._running:
            await asyncio.sleep(poll_seconds)
            for trade in list(self._open_trades.values()):
                if not trade.is_waiting_entry and not self._range_entry_plan_has_pending_orders(
                    trade
                ):
                    continue
                try:
                    await self._reconcile_pending_entry(
                        trade=trade,
                        history_orders=[],
                        open_orders=[],
                        symbol_user_trades=[],
                    )
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    self.store.save_trade(trade)
                    self._log_event(
                        logging.ERROR,
                        "live_trading.pending_entry_monitor_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **self._trade_log_fields(trade),
                    )

    # ── Account Refresh ────────────────────────────────────────────────────

    async def _account_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.LIVE_TRADING_ACCOUNT_REFRESH_SECONDS)
            try:
                await self._refresh_account()
            except Exception as exc:
                self._log_event(
                    logging.ERROR,
                    "live_trading.account_refresh_loop_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    async def _refresh_account(self) -> bool:
        if self._futures_client is None:
            return False
        try:
            use_demo_account = self._use_demo_exchange_symbol()
            info = await self._futures_client.get_full_account_info(
                use_demo_account=use_demo_account
            )
            self.session.account_info = LiveAccountInfo(
                wallet_balance=info.total_wallet_balance,
                available_balance=info.available_balance,
                unrealized_pnl=info.total_unrealized_profit,
                margin_balance=info.available_balance + info.total_position_margin,
                total_position_margin=info.total_position_margin,
                max_withdraw=info.available_balance,
            )
            if use_demo_account:
                balance_anchor = (
                    info.available_balance
                    if info.available_balance > 0
                    else info.total_wallet_balance
                )
                if balance_anchor > 0:
                    self.session.paper_balance = balance_anchor
                    if (
                        self.session.paper_initial_balance <= 0
                        or self.session.total_messages_processed == 0
                    ):
                        self.session.paper_initial_balance = info.total_wallet_balance
            # Store user_id once
            if info.user_id and not getattr(self.session, "_user_id", None):
                self.session.label = self.session.label or f"User {info.user_id}"
            self._log_event(
                logging.INFO,
                "live_trading.account_refreshed",
                use_demo_account=use_demo_account,
                wallet_balance=str(self.session.account_info.wallet_balance),
                available_balance=str(self.session.account_info.available_balance),
                unrealized_pnl=str(self.session.account_info.unrealized_pnl),
            )
        except Exception as exc:
            self.session.account_info = LiveAccountInfo(error=f"account fetch failed: {exc}")
            self.session.exchange_snapshot = None
            self.store.save_session(self.session)
            self._log_event(
                logging.WARNING,
                "live_trading.account_refresh_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return False
        try:
            await self._sync_exchange_state()
        except Exception as exc:
            self.session.exchange_snapshot = self.session.exchange_snapshot or None
            if self.session.exchange_snapshot is not None:
                self.session.exchange_snapshot.error = f"exchange sync failed: {exc}"
            self._log_event(
                logging.WARNING,
                "live_trading.exchange_state_sync_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        self.store.save_session(self.session)
        return self.session.exchange_snapshot is not None and not bool(
            self.session.exchange_snapshot.error
        )

    def _live_balance_for_sizing(self) -> Decimal:
        account = self.session.account_info
        if account is None or not account.is_valid:
            raise ValueError("Live account balance is unavailable for position sizing")
        if account.available_balance > 0:
            return account.available_balance
        if account.wallet_balance > 0:
            return account.wallet_balance
        raise ValueError("Live account balance is zero; cannot size a position")

    def _demo_balance_for_sizing(self) -> Decimal:
        account = self.session.account_info
        if account is not None and account.is_valid:
            if account.available_balance > 0:
                return account.available_balance
            if account.wallet_balance > 0:
                return account.wallet_balance
        if self.session.paper_balance > 0:
            return self.session.paper_balance
        raise ValueError("Demo account balance is unavailable for position sizing")

    # ── Real Exchange Execution ────────────────────────────────────────────

    def _register_account_trade(self, trade: LiveTrade) -> None:
        self._account_coordinator.register_trade(
            trade,
            trading_mode=self.session.trading_mode,
            close_handler=self._dispatch_account_net_close,
        )

    async def _dispatch_account_net_close(
        self,
        trade_id: str,
        quantity: Decimal,
        reason: str,
    ) -> Decimal:
        async def close_owned_trade() -> Decimal:
            trade = next(
                (
                    item
                    for item in self._open_trades.values()
                    if item.trade_id == trade_id
                ),
                None,
            )
            if trade is None or not trade.is_open:
                return Decimal("0")
            requested_quantity = min(quantity, trade.remaining_quantity)
            if requested_quantity <= Decimal("0"):
                return Decimal("0")
            result = await self._execute_exchange_close_unlocked(
                trade=trade,
                fraction=requested_quantity / trade.remaining_quantity,
                reason=reason,
            )
            if not trade.is_open:
                self._finalize_closed_trade(trade)
                context = self._contexts.get(trade.channel_id)
                if context is not None:
                    self._mark_signal_terminal(
                        context=context,
                        signal_id=trade.signal_id,
                        status=SignalStatus.CLOSED,
                        trade=trade,
                    )
            else:
                self._persist_trade_runtime_state(trade)
            return result.executed_quantity

        owner_loop = self._loop
        if owner_loop is None or owner_loop is asyncio.get_running_loop():
            return await close_owned_trade()
        if owner_loop.is_closed():
            raise AccountExecutionConflictError(
                f"owner engine loop is unavailable for trade {trade_id}"
            )
        future = asyncio.run_coroutine_threadsafe(close_owned_trade(), owner_loop)
        return await asyncio.wrap_future(future)

    async def _real_open_position(
        self,
        trade: LiveTrade,
        *,
        current_mark_price: Decimal | None = None,
    ) -> None:
        async with self._account_coordinator.execution_guard():
            await self._real_open_position_unlocked(
                trade,
                current_mark_price=current_mark_price,
            )

    async def _real_open_position_unlocked(
        self,
        trade: LiveTrade,
        *,
        current_mark_price: Decimal | None = None,
    ) -> None:
        assert self._futures_client is not None
        use_demo_symbol = self._use_demo_exchange_symbol()
        self._log_event(
            logging.INFO,
            "live_trading.exchange_open_started",
            use_demo_symbol=use_demo_symbol,
            **self._trade_log_fields(trade),
        )
        trade.exchange_symbol = to_exchange_futures_symbol(
            trade.symbol,
            use_demo_symbol=use_demo_symbol,
        )
        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(f"Contract spec unavailable for {trade.symbol}")
        await self._reconcile_stopped_session_bucket_if_flat(trade)
        decision = await self._account_coordinator.coordinate_open(
            trade,
            trading_mode=self.session.trading_mode,
        )
        trade.account_coordination_action = decision.action
        trade.account_coordination_notes = list(decision.notes)
        trade.account_netted_quantity = decision.offset_quantity
        trade.consensus_duplicate_trade_id = decision.duplicate_trade_id
        if trade.message_history:
            trade.message_history[-1].notes.extend(decision.notes)
        self._log_event(
            logging.INFO,
            "live_trading.account_open_coordinated",
            account_coordination_action=decision.action,
            original_quantity=str(decision.original_quantity),
            approved_quantity=str(decision.approved_quantity),
            offset_quantity=str(decision.offset_quantity),
            duplicate_trade_id=decision.duplicate_trade_id,
            coordination_notes=list(decision.notes),
            **self._trade_log_fields(trade),
        )
        if not decision.should_open:
            trade.status = "closed"
            trade.remaining_quantity = Decimal("0")
            trade.margin = Decimal("0")
            trade.closed_at = _utc_now()
            trade.entry_order_type = (
                "ACCOUNT_NET" if decision.action == "netted" else "CONSENSUS"
            )
            trade.entry_order_status = (
                "NETTED" if decision.action == "netted" else "DEDUPLICATED"
            )
            trade.close_reason = (
                "account_net_offset"
                if decision.action == "netted"
                else f"consensus_duplicate:{decision.duplicate_trade_id}"
            )
            return

        # Do not mutate symbol leverage for a consensus-only or fully-netted
        # signal. Exchange tier negotiation starts only when residual exposure
        # really needs to be opened.
        self._apply_exchange_risk_limit_to_open_trade(trade, spec)
        try:
            await self._ensure_supported_exchange_leverage(
                trade=trade,
                spec=spec,
                use_demo_symbol=use_demo_symbol,
            )
        except Exception as exc:
            if self.settings.LIVE_TRADING_FAIL_CLOSED_ON_LEVERAGE_SYNC_ERROR:
                raise ValueError(f"set_leverage failed for {trade.symbol}: {exc}") from exc
            self._log_event(
                logging.WARNING,
                "live_trading.exchange_leverage_sync_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
            trade.last_exchange_sync_error = str(exc)

        # Account policy and exchange risk tiers can both reduce quantity. TP
        # capacity and margin are calculated only from the final executable
        # amount.
        self._ensure_exchange_quantity_supports_market_entry(trade, spec)
        self._ensure_exchange_quantity_supports_take_profit_ladder(trade, spec)
        trade.margin = (
            trade.entry_price * trade.quantity / Decimal(str(trade.leverage))
        ).quantize(Decimal("0.00000001"))

        range_plan = self._build_range_entry_order_plan(trade=trade, spec=spec)
        if range_plan:
            await self._submit_range_entry_order_plan(
                trade=trade,
                spec=spec,
                plan=range_plan,
                use_demo_symbol=use_demo_symbol,
            )
            return

        if current_mark_price is None and trade.entry_type == EntryType.TRIGGER.value:
            current_mark_price = await self._get_mark_price(trade.symbol)
        entry_request = self._build_entry_order_request(
            trade=trade,
            current_mark_price=current_mark_price,
        )
        client_order_id = f"triak_entry_{trade.trade_id}_{uuid.uuid4().hex[:10]}"
        if trade.side == "long":
            if entry_request.order_type == "MARKET":
                order = await self._futures_client.open_long(
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    leverage=trade.leverage,
                    use_demo_symbol=use_demo_symbol,
                )
            else:
                order = await self._futures_client.open_long(
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    leverage=trade.leverage,
                    order_type=entry_request.order_type,
                    price=entry_request.price,
                    stop_price=entry_request.stop_price,
                    client_order_id=client_order_id,
                    use_demo_symbol=use_demo_symbol,
                )
        else:
            if entry_request.order_type == "MARKET":
                order = await self._futures_client.open_short(
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    leverage=trade.leverage,
                    use_demo_symbol=use_demo_symbol,
                )
            else:
                order = await self._futures_client.open_short(
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    leverage=trade.leverage,
                    order_type=entry_request.order_type,
                    price=entry_request.price,
                    stop_price=entry_request.stop_price,
                    client_order_id=client_order_id,
                    use_demo_symbol=use_demo_symbol,
                )

        now = _utc_now()
        trade.entry_order_id = order.order_id
        trade.entry_order_client_id = getattr(order, "client_order_id", None) or client_order_id
        trade.entry_order_type = entry_request.order_type
        trade.entry_order_status = str(getattr(order, "status", "")).upper() or "SUBMITTED"
        trade.entry_submitted_at = now
        pending_ttl_seconds = max(
            1,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_PENDING_ENTRY_TTL_SECONDS",
                    86400,
                )
            ),
        )
        trade.entry_order_expires_at = now + timedelta(seconds=pending_ttl_seconds)
        trade.exchange_symbol = getattr(order, "exchange_symbol", None) or trade.exchange_symbol
        self._account_coordinator.mark_order_owner(
            trade.entry_order_id,
            trade,
            trading_mode=self.session.trading_mode,
        )
        if trade.message_history:
            trade.message_history[-1].notes[0:0] = [
                f"entry_order_type={entry_request.order_type}",
                f"entry_order_reason={entry_request.reason}",
                f"entry_order_status={trade.entry_order_status}",
            ]

        terminal_statuses = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
        if trade.entry_order_status in terminal_statuses:
            raise ValueError(
                f"Toobit open order {order.order_id} finished with status "
                f"{getattr(order, 'status', '')}"
            )

        confirmed_order = order
        fills: list[Any] = []
        if entry_request.order_type == "MARKET":
            confirmed, fills = await self._futures_client.wait_for_order_fill(
                symbol=trade.symbol,
                order_id=order.order_id,
                use_demo_symbol=use_demo_symbol,
                timeout_seconds=float(self.settings.LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS),
            )
            if confirmed is None:
                raise ValueError(f"Toobit did not return market entry {order.order_id} in history")
            confirmed_order = confirmed
            if confirmed_order.status.upper() in terminal_statuses:
                raise ValueError(
                    f"Toobit open order {confirmed_order.order_id} finished with status "
                    f"{confirmed_order.status}"
                )
            if not self._entry_order_has_fill(confirmed_order):
                raise ValueError(
                    f"Toobit market entry {confirmed_order.order_id} was not filled "
                    f"(status={confirmed_order.status})"
                )

        if not self._entry_order_has_fill(confirmed_order):
            trade.status = "waiting_entry"
            self._register_account_trade(trade)
            trade.last_exchange_sync_error = None
            self._log_event(
                logging.INFO,
                "live_trading.exchange_entry_waiting",
                order_id=trade.entry_order_id,
                requested_price=str(trade.entry_price),
                entry_order_reason=entry_request.reason,
                entry_order_expires_at=trade.entry_order_expires_at.isoformat(),
                **self._trade_log_fields(trade),
            )
            return

        await self._activate_filled_entry(
            trade=trade,
            confirmed_order=confirmed_order,
            fills=fills,
            spec=spec,
        )
        self._log_event(
            logging.INFO,
            "live_trading.exchange_open_completed",
            order_id=confirmed_order.order_id,
            executed_contract_quantity=str(confirmed_order.executed_qty),
            fill_count=len(fills),
            **self._trade_log_fields(trade),
        )

    async def _reconcile_stopped_session_bucket_if_flat(self, trade: LiveTrade) -> None:
        """Release stale stopped-session ownership only after exchange-flat confirmation."""

        blocked_reason = self._account_coordinator.bucket_block_reason(
            trade,
            trading_mode=self.session.trading_mode,
        )
        if not blocked_reason or "belongs to a stopped session" not in blocked_reason:
            return
        assert self._futures_client is not None
        use_demo_symbol = self._use_demo_exchange_symbol()
        positions = await self._futures_client.get_open_positions(
            use_demo_symbol=use_demo_symbol
        )
        position_snapshots = [self._position_snapshot(item) for item in positions]
        if self._find_matching_exchange_position(trade=trade, positions=position_snapshots):
            self._log_event(
                logging.WARNING,
                "live_trading.stopped_session_bucket_retained_exchange_position_present",
                blocked_reason=blocked_reason,
                **self._trade_log_fields(trade),
            )
            return
        open_orders = await self._futures_client.get_open_orders(
            trade.symbol,
            use_demo_symbol=use_demo_symbol,
        )
        try:
            open_orders.extend(
                await self._futures_client.get_open_algo_orders(
                    trade.symbol,
                    use_demo_symbol=use_demo_symbol,
                )
            )
        except Exception:
            pass
        open_order_ids = {
            str(getattr(order, "order_id", None) or getattr(order, "orderId", None) or "")
            for order in open_orders
        }
        released_trade_ids: list[str] = []
        for session in self.store.list_sessions(limit=500):
            if session.status in {"starting", "running"}:
                continue
            for stale_trade in self.store.list_open_trades(session.session_id):
                if stale_trade.symbol != trade.symbol or stale_trade.side != trade.side:
                    continue
                owned_order_ids = {
                    str(order_id)
                    for order_id in (
                        stale_trade.entry_order_id,
                        stale_trade.sl_order_id,
                        *stale_trade.tp_order_ids,
                    )
                    if order_id
                }
                if owned_order_ids & open_order_ids:
                    self._log_event(
                        logging.WARNING,
                        "live_trading.stopped_session_bucket_retained_owned_order_present",
                        stale_trade_id=stale_trade.trade_id,
                        owned_open_order_ids=sorted(owned_order_ids & open_order_ids),
                        **self._trade_log_fields(trade),
                    )
                    continue
                unaccounted_quantity = max(Decimal("0"), stale_trade.remaining_quantity)
                stale_trade.status = "closed"
                stale_trade.close_reason = "stopped_session_exchange_flat_auto_reconciled"
                stale_trade.closed_at = _utc_now()
                stale_trade.remaining_quantity = Decimal("0")
                stale_trade.pending_fill_audit_quantity += unaccounted_quantity
                stale_trade.unrealized_pnl = Decimal("0")
                stale_trade.exchange_position = None
                stale_trade.last_exchange_sync_error = None
                self.store.save_trade(stale_trade)
                session.open_positions_count = max(0, session.open_positions_count - 1)
                session.closed_trades_count += 1
                issue_prefix = (
                    f"unresolved open trade {stale_trade.trade_id} belongs to a stopped session"
                )
                session.health_issues = [
                    issue for issue in session.health_issues if not issue.startswith(issue_prefix)
                ]
                if not session.health_issues:
                    session.health_status = "healthy"
                    session.new_entries_blocked = False
                    if session.last_error and issue_prefix in session.last_error:
                        session.last_error = None
                self.store.save_session(session)
                self._account_coordinator.clear_trade_bucket_block(
                    stale_trade,
                    trading_mode=session.trading_mode,
                    reason_prefix=issue_prefix,
                )
                released_trade_ids.append(stale_trade.trade_id)
        if released_trade_ids:
            self._log_event(
                logging.WARNING,
                "live_trading.stopped_session_bucket_auto_reconciled",
                released_trade_ids=released_trade_ids,
                exchange_position_present=False,
                owned_open_order_present=False,
                **self._trade_log_fields(trade),
            )

    def _build_range_entry_order_plan(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
    ) -> list[LiveEntryOrderPlan]:
        """Split a true range entry without exceeding its risk-sized quantity."""

        if str(trade.entry_type).lower() != EntryType.RANGE.value:
            return []
        low = trade.requested_entry_low
        high = trade.requested_entry_high
        if low is None or high is None or low <= 0 or high <= 0 or low == high:
            return []
        range_start, range_end = sorted((low, high))
        midpoint = (range_start + range_end) / Decimal("2")
        step = self._coerce_decimal(getattr(spec, "contract_step_size", Decimal("0")))
        if step <= 0:
            step = Decimal("0.00000001")
        total_contracts = self._floor_exchange_contract_quantity(trade.quantity, spec)
        total_units = self._exchange_quantity_units(total_contracts, step)
        minimum_contracts = self._minimum_exchange_contract_quantity(spec)
        minimum_units = max(
            1,
            self._exchange_quantity_units_ceiling(minimum_contracts, step),
        )
        endpoint_units = total_units // 4
        midpoint_units = total_units - (endpoint_units * 2)
        if endpoint_units < minimum_units or midpoint_units < minimum_units:
            self._log_event(
                logging.WARNING,
                "live_trading.range_entry_ladder_fallback",
                reason="quantity_cannot_support_three_exchange_orders",
                total_contract_quantity=str(total_contracts),
                minimum_contract_quantity=str(minimum_contracts),
                **self._trade_log_fields(trade),
            )
            return []

        contract_quantities = (
            Decimal(endpoint_units) * step,
            Decimal(midpoint_units) * step,
            Decimal(endpoint_units) * step,
        )
        prices = (range_start, midpoint, range_end)
        labels = ("range_start", "range_midpoint", "range_end")
        fractions = (Decimal("0.25"), Decimal("0.50"), Decimal("0.25"))
        plan = [
            LiveEntryOrderPlan(
                leg_index=index,
                label=labels[index],
                price=prices[index],
                quantity=_from_exchange_contract_quantity(
                    contract_quantities[index],
                    spec,
                ).quantize(Decimal("0.00000001")),
                fraction=fractions[index],
            )
            for index in range(3)
        ]
        planned_quantity = sum((leg.quantity for leg in plan), Decimal("0"))
        if planned_quantity <= 0 or planned_quantity > trade.quantity:
            raise AssertionError("Range entry plan exceeded the risk-sized quantity")
        return plan

    @staticmethod
    def _range_entry_plan_has_pending_orders(trade: LiveTrade) -> bool:
        terminal = {
            "CANCELED",
            "CANCELLED",
            "ORDER_CANCELED",
            "REJECTED",
            "ORDER_REJECTED",
            "FAILED",
            "ORDER_FAILED",
            "EXPIRED",
            "FILLED",
            "ORDER_FILLED",
            "PARTIALLY_CANCELED",
            "PARTIALLY_CANCELLED",
            "NOT_SUBMITTED",
        }
        return any(leg.status.upper() not in terminal for leg in trade.entry_order_plan)

    async def _submit_range_entry_order_plan(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
        plan: list[LiveEntryOrderPlan],
        use_demo_symbol: bool,
    ) -> None:
        assert self._futures_client is not None
        now = _utc_now()
        trade.entry_order_plan = plan
        trade.entry_planned_quantity = sum((leg.quantity for leg in plan), Decimal("0"))
        trade.entry_filled_quantity = Decimal("0")
        trade.quantity = trade.entry_planned_quantity
        # Pending quantity remains reserved in the account coordinator. On the
        # first fill it is replaced with confirmed physical exposure.
        trade.remaining_quantity = trade.entry_planned_quantity
        trade.entry_price = plan[1].price
        trade.entry_order_type = "LIMIT_LADDER"
        trade.entry_order_status = "SUBMITTING"
        trade.entry_submitted_at = now
        pending_ttl_seconds = max(
            1,
            int(getattr(self.settings, "LIVE_TRADING_PENDING_ENTRY_TTL_SECONDS", 86400)),
        )
        trade.entry_order_expires_at = now + timedelta(seconds=pending_ttl_seconds)
        trade.status = "waiting_entry"
        self._persist_trade_runtime_state(trade)

        submitted_orders: list[Any] = []
        try:
            for leg in plan:
                client_order_id = (
                    f"triak_entry_{trade.trade_id}_{leg.leg_index + 1}_{uuid.uuid4().hex[:8]}"
                )
                leg.client_order_id = client_order_id
                self._persist_trade_runtime_state(trade)
                open_method = (
                    self._futures_client.open_long
                    if trade.side == "long"
                    else self._futures_client.open_short
                )
                order = await open_method(
                    symbol=trade.symbol,
                    quantity=leg.quantity,
                    leverage=trade.leverage,
                    order_type="LIMIT",
                    price=leg.price,
                    client_order_id=client_order_id,
                    use_demo_symbol=use_demo_symbol,
                )
                leg.order_id = order.order_id
                leg.client_order_id = getattr(order, "client_order_id", None) or client_order_id
                leg.status = str(getattr(order, "status", "")).upper() or "SUBMITTED"
                submitted_orders.append(order)
                self._account_coordinator.mark_order_owner(
                    order.order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
                if leg.leg_index == 1:
                    trade.entry_order_id = leg.order_id
                    trade.entry_order_client_id = leg.client_order_id
                trade.entry_order_status = "LADDER_SUBMITTING"
                self._persist_trade_runtime_state(trade)
                await self._reconcile_range_entry_orders(
                    trade=trade,
                    history_orders=submitted_orders,
                    open_orders=[],
                    symbol_user_trades=[],
                    spec=spec,
                    finalize_unfilled=False,
                )
                if leg.status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    raise ValueError(
                        f"Toobit range entry {order.order_id} finished with status {leg.status}"
                    )
        except Exception as submission_exc:
            for pending_leg in trade.entry_order_plan:
                if pending_leg.client_order_id is None:
                    pending_leg.status = "NOT_SUBMITTED"
            try:
                await self._cancel_range_entry_orders(
                    trade=trade,
                    reason="range_entry_submission_failed",
                    reconcile_fills=True,
                    spec=spec,
                )
            except Exception as cleanup_exc:
                self._mark_trade_reconciliation_required(
                    trade=trade,
                    reason=(
                        "range_entry_submission_cleanup_unconfirmed:"
                        f"{type(cleanup_exc).__name__}:{cleanup_exc}"
                    ),
                )
                trade.last_exchange_sync_error = (
                    f"range entry submission failed ({submission_exc}); "
                    f"cleanup unconfirmed ({cleanup_exc})"
                )
                self._register_account_trade(trade)
                if trade.entry_filled_quantity > 0:
                    await self._enforce_trade_protection_or_flatten(
                        trade=trade,
                        reason="range_entry_uncertain_cleanup_protection",
                    )
                self._persist_trade_runtime_state(trade)
                return
            if trade.entry_filled_quantity > 0:
                await self._enforce_trade_protection_or_flatten(
                    trade=trade,
                    reason="range_entry_submission_failure_protection",
                )
                trade.last_exchange_sync_error = "range_entry_submission_incomplete"
                self._register_account_trade(trade)
                self._persist_trade_runtime_state(trade)
                self._log_event(
                    logging.ERROR,
                    "live_trading.range_entry_submission_partially_failed",
                    **self._trade_log_fields(trade),
                )
                return
            raise

        trade.entry_order_status = "LADDER_PENDING"
        if trade.message_history:
            trade.message_history[-1].notes[0:0] = [
                "entry_order_type=LIMIT_LADDER",
                "entry_distribution=range_start:25%,range_midpoint:50%,range_end:25%",
                f"entry_planned_quantity={trade.entry_planned_quantity}",
            ]
        await self._reconcile_range_entry_orders(
            trade=trade,
            history_orders=submitted_orders,
            open_orders=[],
            symbol_user_trades=[],
            spec=spec,
            finalize_unfilled=False,
        )
        self._register_account_trade(trade)
        self._persist_trade_runtime_state(trade)
        self._log_event(
            logging.INFO,
            "live_trading.range_entry_ladder_submitted",
            leg_count=len(plan),
            planned_quantity=str(trade.entry_planned_quantity),
            prices=[str(leg.price) for leg in plan],
            quantities=[str(leg.quantity) for leg in plan],
            **self._trade_log_fields(trade),
        )

    def _build_entry_order_request(
        self,
        *,
        trade: LiveTrade,
        current_mark_price: Decimal | None,
    ) -> _EntryOrderRequest:
        entry_type = str(trade.entry_type or EntryType.UNKNOWN.value).lower()
        requested_price = trade.entry_price
        if entry_type == EntryType.MARKET.value:
            return _EntryOrderRequest(order_type="MARKET", reason="explicit_market")
        if entry_type == EntryType.TRIGGER.value:
            if current_mark_price is None or current_mark_price <= Decimal("0"):
                raise ValueError("Current mark price is required for a trigger entry")
            already_triggered = (
                current_mark_price >= requested_price
                if trade.side == "long"
                else current_mark_price <= requested_price
            )
            if not already_triggered:
                return _EntryOrderRequest(
                    order_type="STOP",
                    stop_price=requested_price,
                    reason="trigger_not_reached",
                )
            slippage_pct = (
                abs(current_mark_price - requested_price) * Decimal("100") / requested_price
            )
            max_slippage_pct = max(
                Decimal("0"),
                Decimal(
                    str(
                        getattr(
                            self.settings,
                            "LIVE_TRADING_MAX_TRIGGER_SLIPPAGE_PCT",
                            Decimal("0.5"),
                        )
                    )
                ),
            )
            if slippage_pct > max_slippage_pct:
                raise ValueError(
                    "Trigger entry is stale: "
                    f"mark={current_mark_price}, trigger={requested_price}, "
                    f"slippage_pct={slippage_pct}, max={max_slippage_pct}"
                )
            return _EntryOrderRequest(
                order_type="MARKET",
                reason="trigger_reached_within_slippage_limit",
            )
        if requested_price <= Decimal("0"):
            raise ValueError("Pending entry requires a positive requested price")
        return _EntryOrderRequest(
            order_type="LIMIT",
            price=requested_price,
            reason=(
                "explicit_limit_or_range"
                if entry_type in {EntryType.LIMIT.value, EntryType.RANGE.value}
                else "priced_unknown_treated_as_limit"
            ),
        )

    @staticmethod
    def _entry_order_has_fill(order: Any) -> bool:
        status = str(getattr(order, "status", "")).upper()
        executed_qty = getattr(order, "executed_qty", Decimal("0"))
        return status in {"FILLED", "ORDER_FILLED"} or (
            status
            in {
                "PARTIALLY_FILLED",
                "PARTIAL_FILLED",
                "PARTIALLY_CANCELED",
                "PARTIALLY_CANCELLED",
            }
            and executed_qty > Decimal("0")
        )

    @staticmethod
    def _exchange_fill_key(fill: Any) -> str:
        trade_id = str(getattr(fill, "trade_id", "") or "").strip()
        if trade_id:
            return f"trade:{trade_id}"
        return ":".join(
            (
                "fill",
                str(getattr(fill, "order_id", "") or ""),
                str(getattr(fill, "time", "") or ""),
                str(getattr(fill, "side", "") or ""),
                str(getattr(fill, "price", "") or ""),
                str(getattr(fill, "qty", "") or ""),
            )
        )

    def _unprocessed_exchange_fills(
        self,
        trade: LiveTrade,
        fills: list[Any],
    ) -> list[Any]:
        processed = set(trade.processed_exchange_fill_ids)
        return [
            fill
            for fill in fills
            if self._exchange_fill_key(fill) not in processed
            and self._account_coordinator.order_is_available(
                str(getattr(fill, "order_id", "") or ""),
                trade.trade_id,
            )
            and self._account_coordinator.fill_is_available(
                self._exchange_fill_key(fill),
                trade.trade_id,
            )
        ]

    def _record_processed_exchange_fills(
        self,
        trade: LiveTrade,
        fills: list[Any],
    ) -> None:
        existing = set(trade.processed_exchange_fill_ids)
        for fill in fills:
            key = self._exchange_fill_key(fill)
            if key not in existing:
                if not self._account_coordinator.mark_fill_owner(
                    key,
                    trade,
                    trading_mode=self.session.trading_mode,
                ):
                    raise AccountExecutionConflictError(
                        f"exchange fill {key} belongs to another logical trade"
                    )
                trade.processed_exchange_fill_ids.append(key)
                existing.add(key)

    async def _activate_filled_entry(
        self,
        *,
        trade: LiveTrade,
        confirmed_order: Any,
        fills: list[Any],
        spec: Any,
    ) -> None:
        assert (
            self._futures_client is not None and self._pm is not None and self._strategy is not None
        )
        confirmed_status = confirmed_order.status.upper()
        if confirmed_status in {"PARTIALLY_FILLED", "PARTIAL_FILLED"}:
            await self._futures_client.cancel_order(
                symbol=trade.symbol,
                order_id=confirmed_order.order_id,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            confirmed_status = "PARTIALLY_FILLED_REMAINDER_CANCELED"
        elif confirmed_status in {"PARTIALLY_CANCELED", "PARTIALLY_CANCELLED"}:
            confirmed_status = "PARTIALLY_FILLED_REMAINDER_CANCELED"
        trade.entry_order_id = confirmed_order.order_id
        trade.entry_order_status = confirmed_status
        trade.exchange_symbol = confirmed_order.exchange_symbol or trade.exchange_symbol
        executed_contract_qty = (
            sum((fill.qty for fill in fills), Decimal("0"))
            if fills
            else confirmed_order.executed_qty
        )
        executed_quantity = _from_exchange_contract_quantity(executed_contract_qty, spec)
        if executed_quantity <= 0:
            raise ValueError(
                f"Entry order {confirmed_order.order_id} has no confirmed executed quantity"
            )
        fill_price = Decimal("0")
        if fills:
            weighted_notional = sum((fill.price * fill.qty for fill in fills), Decimal("0"))
            weighted_qty = sum((fill.qty for fill in fills), Decimal("0"))
            if weighted_qty > 0:
                fill_price = weighted_notional / weighted_qty
        else:
            average_price = getattr(confirmed_order, "avg_price", Decimal("0"))
            if average_price and average_price > 0:
                fill_price = average_price
        if fill_price <= Decimal("0"):
            raise ValueError(f"Entry order {confirmed_order.order_id} has no confirmed fill price")
        trade.quantity = executed_quantity
        trade.remaining_quantity = executed_quantity
        trade.entry_price = fill_price
        if trade.leverage > 0:
            trade.margin = (
                trade.entry_price * trade.quantity / Decimal(str(trade.leverage))
            ).quantize(Decimal("0.00000001"))
        fill_time = _utc_now()
        trade.status = "open"
        trade.opened_at = fill_time
        trade.entry_filled_at = fill_time
        trade.updated_at = fill_time
        trade.entry_order_expires_at = None
        self._register_account_trade(trade)
        entry_fills = self._unprocessed_exchange_fills(trade, fills)
        if entry_fills:
            trade.fees += sum(
                (fill.commission for fill in entry_fills),
                Decimal("0"),
            )
            self._record_processed_exchange_fills(trade, entry_fills)
            self._book_trade_realized_totals(trade)
        protection_notes = self._pm.rebase_trade_protection_after_entry_fill(
            trade=trade,
            strategy=self._strategy,
        )
        if trade.message_history:
            trade.message_history[-1].action = "opened"
            trade.message_history[-1].notes.extend(protection_notes)
        trade.last_exchange_sync_error = None
        try:
            await self._ensure_trade_protection_after_open(trade)
        except Exception as exc:
            trade.last_exchange_sync_error = str(exc)
            try:
                await self._execute_exchange_close(
                    trade=trade,
                    fraction=Decimal("1"),
                    reason="protection_sync_failed_flatten",
                )
            except Exception as close_exc:
                raise ValueError(
                    "Failed to set trading protection and auto-flatten also failed: "
                    f"{exc}; close_error={close_exc}"
                ) from close_exc
            raise ValueError(
                f"Failed to set trading protection; position was flattened automatically: {exc}"
            ) from exc

    async def _ensure_trade_protection_after_open(self, trade: LiveTrade) -> None:
        attempts = max(
            1,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_PROTECTION_SYNC_RETRY_ATTEMPTS",
                    1,
                )
            ),
        )
        delay_seconds = max(
            0.0,
            float(
                getattr(
                    self.settings,
                    "LIVE_TRADING_PROTECTION_SYNC_RETRY_DELAY_SECONDS",
                    0.0,
                )
            ),
        )
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self._sync_trade_protection(trade)
            except Exception as exc:
                last_exc = exc
                trade.protection_sync_failures += 1
                trade.last_protection_sync_error_at = _utc_now()
                trade.last_exchange_sync_error = str(exc)
                self._log_event(
                    logging.WARNING,
                    "live_trading.exchange_protection_sync_attempt_failed",
                    attempt=attempt,
                    attempts=attempts,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._protection_log_fields(trade),
                    **self._trade_log_fields(trade),
                )
                if await self._exchange_trade_has_required_protection(trade):
                    if trade.message_history:
                        trade.message_history[-1].notes.append(f"protection_sync_degraded={exc}")
                    return
                if attempt >= attempts:
                    break
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
            else:
                trade.protection_sync_failures = 0
                trade.consecutive_protection_failures = 0
                trade.last_protection_sync_error_at = None
                trade.protection_retry_at = None
                trade.last_exchange_sync_error = None
                self._clear_protection_health_issue(trade)
                self._log_event(
                    logging.INFO,
                    "live_trading.exchange_protection_sync_completed",
                    attempts_used=attempt,
                    **self._trade_log_fields(trade),
                )
                return
        assert last_exc is not None
        raise last_exc

    def _schedule_protection_retry(self, trade: LiveTrade, error: Exception) -> None:
        trade.consecutive_protection_failures += 1
        base_seconds = max(
            1,
            int(getattr(self.settings, "LIVE_TRADING_PROTECTION_CIRCUIT_BASE_SECONDS", 60)),
        )
        max_seconds = max(
            base_seconds,
            int(getattr(self.settings, "LIVE_TRADING_PROTECTION_CIRCUIT_MAX_SECONDS", 1800)),
        )
        delay_seconds = min(
            max_seconds,
            base_seconds * (2 ** min(trade.consecutive_protection_failures - 1, 10)),
        )
        trade.protection_retry_at = _utc_now() + timedelta(seconds=delay_seconds)
        issue = f"protection unavailable for {trade.trade_id}: {error}"
        self._account_coordinator.block_trade_bucket(
            trade,
            trading_mode=self.session.trading_mode,
            reason=issue,
        )
        self._set_session_health_issue(issue, critical=True)
        self._persist_trade_runtime_state(trade)
        self._log_event(
            logging.CRITICAL,
            "live_trading.protection_circuit_opened",
            retry_at=trade.protection_retry_at.isoformat(),
            retry_delay_seconds=delay_seconds,
            consecutive_failures=trade.consecutive_protection_failures,
            **self._trade_log_fields(trade),
        )

    def _protection_retry_is_deferred(self, trade: LiveTrade) -> bool:
        return bool(trade.protection_retry_at and trade.protection_retry_at > _utc_now())

    def _clear_protection_health_issue(self, trade: LiveTrade) -> None:
        prefix = f"protection unavailable for {trade.trade_id}:"
        self._clear_trade_health_issue(trade, prefix=prefix)

    def _clear_trade_health_issue(self, trade: LiveTrade, *, prefix: str) -> None:
        self.session.health_issues = [
            item for item in self.session.health_issues if not item.startswith(prefix)
        ]
        self._account_coordinator.clear_trade_bucket_block(
            trade,
            trading_mode=self.session.trading_mode,
            reason_prefix=prefix,
        )
        if not self.session.health_issues:
            self.session.health_status = "healthy"
            self.session.new_entries_blocked = False
            self.session.last_error = None
        self.session.last_update_at = _utc_now()
        self.store.save_session(self.session)

    async def _enforce_trade_protection_or_flatten(
        self,
        *,
        trade: LiveTrade,
        reason: str,
    ) -> bool:
        """Repair complete exchange protection or flatten the position."""
        try:
            await self._ensure_trade_protection_after_open(trade)
        except Exception as protection_exc:
            trade.last_exchange_sync_error = str(protection_exc)
            self._log_event(
                logging.ERROR,
                "live_trading.exchange_protection_enforcement_failed",
                enforcement_reason=reason,
                error_type=type(protection_exc).__name__,
                error=str(protection_exc),
                **self._protection_log_fields(trade),
                **self._trade_log_fields(trade),
            )
            try:
                await self._execute_exchange_close(
                    trade=trade,
                    fraction=Decimal("1"),
                    reason=f"{reason}_fail_closed_flatten",
                )
            except Exception as close_exc:
                trade.last_exchange_sync_error = (
                    f"protection_error={protection_exc}; close_error={close_exc}"
                )
                self._persist_trade_runtime_state(trade)
                self._log_event(
                    logging.CRITICAL,
                    "live_trading.unprotected_position_emergency_close_failed",
                    enforcement_reason=reason,
                    protection_error_type=type(protection_exc).__name__,
                    protection_error=str(protection_exc),
                    close_error_type=type(close_exc).__name__,
                    close_error=str(close_exc),
                    **self._protection_log_fields(trade),
                    **self._trade_log_fields(trade),
                )
                self._schedule_protection_retry(trade, protection_exc)
                return False
            if trade.is_open:
                residual_error = RuntimeError(
                    "Fail-closed flatten left a residual open position after "
                    f"{reason}: {protection_exc}"
                )
                trade.last_exchange_sync_error = str(residual_error)
                self._persist_trade_runtime_state(trade)
                self._log_event(
                    logging.CRITICAL,
                    "live_trading.unprotected_position_residual_after_flatten",
                    enforcement_reason=reason,
                    **self._protection_log_fields(trade),
                    **self._trade_log_fields(trade),
                )
                self._schedule_protection_retry(trade, residual_error)
                return False
            if trade.signal_id in self._open_trades:
                self._finalize_closed_trade(trade)
                context = self._contexts.get(trade.channel_id)
                if context is not None:
                    self._mark_signal_terminal(
                        context=context,
                        signal_id=trade.signal_id,
                        status=SignalStatus.CLOSED,
                        trade=trade,
                    )
            self._log_event(
                logging.ERROR,
                "live_trading.unprotected_position_flattened",
                enforcement_reason=reason,
                protection_error_type=type(protection_exc).__name__,
                protection_error=str(protection_exc),
                **self._trade_log_fields(trade),
            )
            return False
        return True

    async def _exchange_trade_has_required_protection(self, trade: LiveTrade) -> bool:
        if self._futures_client is None or not trade.is_open:
            return False
        if trade.required_tp_order_count <= 0:
            try:
                spec = await self._futures_client.get_contract_spec(trade.symbol)
            except Exception:
                return False
            if spec is None:
                return False
            trade.required_tp_order_count = len(self._exchange_take_profit_orders(trade, spec=spec))
            if trade.required_tp_order_count <= 0:
                return False
        try:
            await self._refresh_trade_protection_ids(trade)
        except Exception:
            return False
        return self._tracked_trade_has_required_protection(trade)

    async def _restore_previous_trade_protection(
        self,
        *,
        trade: LiveTrade,
        previous_stop_loss: Decimal | None,
        previous_take_profits: list[Decimal],
        original_error: Exception,
        attribution: MessageAttribution | None,
        sync_context: str,
    ) -> bool:
        if not self._uses_exchange_execution():
            return False
        trade.stop_loss = previous_stop_loss
        trade.take_profits = list(previous_take_profits)
        if previous_stop_loss is None and not previous_take_profits:
            return False
        restored = await self._enforce_trade_protection_or_flatten(
            trade=trade,
            reason=f"{sync_context}_restore_previous_protection",
        )
        if not restored:
            restore_error = trade.last_exchange_sync_error or "fail-closed flatten executed"
            if attribution is not None:
                attribution.notes.append(
                    f"exchange_previous_protection_restore_failed={restore_error}"
                )
            self._log_event(
                logging.ERROR,
                "live_trading.exchange_protection_restore_failed",
                sync_context=sync_context,
                original_error_type=type(original_error).__name__,
                original_error=str(original_error),
                restore_error=restore_error,
                restored_stop_loss=self._decimal_text(previous_stop_loss),
                restored_take_profit_count=len(previous_take_profits),
                **self._protection_log_fields(trade),
                **self._trade_log_fields(trade),
            )
            return False
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_restored_after_failure",
            sync_context=sync_context,
            original_error_type=type(original_error).__name__,
            original_error=str(original_error),
            restored_stop_loss=self._decimal_text(previous_stop_loss),
            restored_take_profit_count=len(previous_take_profits),
            **self._protection_log_fields(trade),
            **self._trade_log_fields(trade),
        )
        return True

    def _apply_exchange_risk_limit_to_open_trade(self, trade: LiveTrade, spec: Any) -> None:
        max_allowed_leverage = spec.max_allowed_leverage(
            quantity=trade.quantity,
            entry_price=trade.entry_price,
        )
        if max_allowed_leverage is None or trade.leverage <= max_allowed_leverage:
            return
        previous_leverage = trade.leverage
        previous_quantity = trade.quantity
        trade.leverage = max_allowed_leverage
        if trade.margin > 0 and trade.entry_price > 0:
            target_quantity = (
                trade.margin * Decimal(str(trade.leverage)) / trade.entry_price
            ).quantize(Decimal("0.00000001"))
            exchange_quantity = _to_exchange_contract_quantity(target_quantity, spec)
            normalized_quantity = _from_exchange_contract_quantity(exchange_quantity, spec)
            if normalized_quantity <= 0:
                raise ValueError(
                    f"Exchange risk-limit clamp rounded quantity to zero for {trade.symbol}"
                )
            trade.quantity = normalized_quantity
            trade.remaining_quantity = min(trade.remaining_quantity, normalized_quantity)
        trade.margin = (trade.entry_price * trade.quantity / Decimal(str(trade.leverage))).quantize(
            Decimal("0.00000001")
        )
        clamp_note = (
            "exchange_leverage_clamped="
            f"{previous_leverage}x->{trade.leverage}x "
            f"qty={previous_quantity}->{trade.quantity}"
        )
        if trade.message_history:
            trade.message_history[-1].notes.append(clamp_note)

    def _ensure_exchange_quantity_supports_take_profit_ladder(
        self,
        trade: LiveTrade,
        spec: Any,
    ) -> None:
        pending_targets = trade.take_profits[trade.targets_hit :]
        if not pending_targets:
            raise ValueError("Position has no take-profit targets")
        original_quantity = trade.quantity
        plan = self._plan_exchange_take_profit_ladder_for_current_quantity(
            trade=trade,
            spec=spec,
        )
        if plan is None or plan.target_count <= 0:
            raise ValueError(
                "Position cannot support any take-profit order on the exchange "
                "at its risk-sized quantity"
            )
        self._apply_exchange_take_profit_ladder_plan(
            trade=trade,
            plan=plan,
            reason="pre_entry_capacity",
        )
        trade.required_tp_order_count = plan.target_count
        self._log_event(
            logging.INFO,
            "live_trading.exchange_tp_capacity_planned",
            available_target_count=plan.original_count,
            selected_target_count=plan.target_count,
            fixed_position_quantity=str(trade.quantity),
            minimum_contract_quantity=str(self._minimum_exchange_contract_quantity(spec)),
            contract_step_size=str(
                self._coerce_decimal(getattr(spec, "contract_step_size", Decimal("0")))
            ),
            **self._trade_log_fields(trade),
        )
        if trade.quantity != original_quantity:
            raise AssertionError("TP planning must never change entry quantity")

    def _plan_exchange_take_profit_ladder_for_current_quantity(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
    ) -> _TakeProfitLadderPlan | None:
        return self._plan_exchange_take_profit_ladder(
            trade=trade,
            spec=spec,
        )

    def _plan_exchange_take_profit_ladder(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
    ) -> _TakeProfitLadderPlan | None:
        pending_targets = list(trade.take_profits[trade.targets_hit :])
        original_count = len(pending_targets)
        if original_count == 0:
            return None
        supported_count = len(self._exchange_take_profit_orders(trade, spec=spec))
        return _TakeProfitLadderPlan(
            pending_take_profits=pending_targets,
            required_quantity=trade.remaining_quantity.quantize(Decimal("0.00000001")),
            original_count=original_count,
            target_count=supported_count,
        )

    def _apply_exchange_take_profit_ladder_plan(
        self,
        *,
        trade: LiveTrade,
        plan: _TakeProfitLadderPlan,
        reason: str,
    ) -> None:
        if plan.original_count <= 0:
            return
        if trade.message_history and plan.target_count < plan.original_count:
            trade.message_history[-1].notes.append(
                f"exchange_tp_ladder_partial={plan.target_count}/{plan.original_count}"
                f"_due_to_{reason}"
            )

    def _ensure_exchange_quantity_supports_market_entry(
        self,
        trade: LiveTrade,
        spec: Any,
    ) -> None:
        minimum_entry_contracts = self._minimum_exchange_contract_quantity(spec)
        minimum_entry_quantity = _from_exchange_contract_quantity(
            minimum_entry_contracts,
            spec,
        ).quantize(Decimal("0.00000001"))
        if minimum_entry_quantity <= 0 or trade.quantity >= minimum_entry_quantity:
            return
        raise ValueError(
            "Risk-sized position is below the exchange minimum entry quantity; "
            "refusing to increase volume "
            f"(symbol={trade.symbol}, quantity={trade.quantity}, "
            f"minimum_quantity={minimum_entry_quantity})"
        )

    @staticmethod
    def _minimum_exchange_contract_quantity(spec: Any) -> Decimal:
        min_contract_qty = LiveTradingEngine._coerce_decimal(
            getattr(spec, "contract_min_qty", Decimal("0"))
        )
        if min_contract_qty > 0:
            return min_contract_qty
        step = LiveTradingEngine._coerce_decimal(getattr(spec, "contract_step_size", Decimal("0")))
        if step > 0:
            return step
        return Decimal("0")

    async def _ensure_supported_exchange_leverage(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
        use_demo_symbol: bool,
    ) -> None:
        assert self._futures_client is not None
        original_leverage = trade.leverage
        original_quantity = trade.quantity
        original_remaining_quantity = trade.remaining_quantity
        original_margin = trade.margin
        candidates = self._candidate_exchange_leverages(
            requested_leverage=trade.leverage,
            spec=spec,
        )
        last_exc: Exception | None = None
        for candidate in candidates:
            trade.leverage = candidate
            if candidate != original_leverage:
                self._resize_trade_for_target_leverage(
                    trade=trade,
                    spec=spec,
                    target_leverage=candidate,
                    margin_budget=original_margin,
                )
            try:
                await self._futures_client.set_leverage(
                    trade.symbol,
                    trade.leverage,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception as exc:
                if not self._is_target_leverage_error(exc):
                    trade.leverage = original_leverage
                    trade.quantity = original_quantity
                    trade.remaining_quantity = original_remaining_quantity
                    trade.margin = original_margin
                    raise
                last_exc = exc
                self._log_event(
                    logging.INFO,
                    "live_trading.exchange_leverage_candidate_rejected",
                    requested_leverage=original_leverage,
                    candidate_leverage=candidate,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
                continue
            if candidate != original_leverage and trade.message_history:
                trade.message_history[-1].notes.append(
                    "exchange_leverage_fallback="
                    f"{original_leverage}x->{trade.leverage}x "
                    f"qty={original_quantity}->{trade.quantity}"
                )
            self._log_event(
                logging.INFO,
                "live_trading.exchange_leverage_selected",
                requested_leverage=original_leverage,
                selected_leverage=trade.leverage,
                original_quantity=str(original_quantity),
                selected_quantity=str(trade.quantity),
                **self._trade_log_fields(trade),
            )
            return
        trade.leverage = original_leverage
        trade.quantity = original_quantity
        trade.remaining_quantity = original_remaining_quantity
        trade.margin = original_margin
        if last_exc is not None:
            raise last_exc
        raise ValueError(f"No leverage candidate available for {trade.symbol}")

    def _candidate_exchange_leverages(
        self,
        *,
        requested_leverage: int,
        spec: Any,
    ) -> list[int]:
        candidates: list[int] = [requested_leverage]
        tier_levels = [
            limit.max_leverage
            for limit in getattr(spec, "risk_limits", [])
            if limit.max_leverage > 0 and limit.max_leverage < requested_leverage
        ]
        common_levels = [
            level for level in [75, 50, 40, 25, 20, 10, 5, 3, 2, 1] if level < requested_leverage
        ]
        for level in sorted(set(tier_levels + common_levels), reverse=True):
            if level not in candidates:
                candidates.append(level)
        return candidates

    def _resize_trade_for_target_leverage(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
        target_leverage: int,
        margin_budget: Decimal,
    ) -> None:
        if margin_budget <= 0 or trade.entry_price <= 0:
            return
        target_quantity = (
            margin_budget * Decimal(str(target_leverage)) / trade.entry_price
        ).quantize(Decimal("0.00000001"))
        exchange_quantity = _to_exchange_contract_quantity(target_quantity, spec)
        normalized_quantity = _from_exchange_contract_quantity(exchange_quantity, spec)
        if normalized_quantity <= 0:
            raise ValueError(
                f"Exchange leverage fallback rounded quantity to zero for {trade.symbol}"
            )
        trade.quantity = normalized_quantity
        trade.remaining_quantity = min(trade.remaining_quantity, normalized_quantity)
        trade.margin = (
            trade.entry_price * trade.quantity / Decimal(str(target_leverage))
        ).quantize(Decimal("0.00000001"))

    def _is_target_leverage_error(self, exc: Exception) -> bool:
        return "Position size cannot meet target leverage" in str(exc)

    async def _submit_exchange_close_order(
        self,
        trade: LiveTrade,
        fraction: Decimal,
    ) -> tuple[Any, Decimal]:
        close_qty = (trade.remaining_quantity * fraction).quantize(Decimal("0.00000001"))
        return await self._submit_exchange_close_quantity_order(trade, close_qty)

    async def _submit_exchange_close_quantity_order(
        self,
        trade: LiveTrade,
        quantity: Decimal,
    ) -> tuple[Any, Decimal]:
        if self._futures_client is None:
            raise ValueError("Futures client is not configured")
        close_qty = quantity.quantize(Decimal("0.00000001"))
        if close_qty <= 0:
            raise ValueError("Close quantity resolved to zero")
        use_demo_symbol = self._use_demo_exchange_symbol()
        if trade.side == "long":
            order = await self._futures_client.close_long(
                symbol=trade.symbol,
                quantity=close_qty,
                use_demo_symbol=use_demo_symbol,
            )
        else:
            order = await self._futures_client.close_short(
                symbol=trade.symbol,
                quantity=close_qty,
                use_demo_symbol=use_demo_symbol,
            )
        return order, close_qty

    async def _execute_exchange_close(
        self,
        *,
        trade: LiveTrade,
        fraction: Decimal,
        reason: str,
        message: MessageAttribution | None = None,
    ) -> _ExchangeCloseResult:
        async with self._account_coordinator.execution_guard():
            return await self._execute_exchange_close_unlocked(
                trade=trade,
                fraction=fraction,
                reason=reason,
                message=message,
            )

    async def _execute_exchange_close_unlocked(
        self,
        *,
        trade: LiveTrade,
        fraction: Decimal,
        reason: str,
        message: MessageAttribution | None = None,
    ) -> _ExchangeCloseResult:
        assert self._futures_client is not None
        self._log_event(
            logging.INFO,
            "live_trading.exchange_close_started",
            close_fraction=str(fraction),
            reason=reason,
            **self._trade_log_fields(trade),
        )
        if trade.entry_order_plan:
            if reason.startswith("tp") and reason.endswith("_hit"):
                await self._apply_range_entry_target_cancellation_policy(
                    trade=trade,
                    target_hits=trade.targets_hit + 1,
                    reason=f"exit_started:{reason}",
                )
            else:
                await self._cancel_range_entry_orders(
                    trade=trade,
                    reason=f"exit_started:{reason}",
                    reconcile_fills=True,
                    ensure_protection=False,
                )
        owns_entire_exchange_bucket = not self._account_coordinator.has_other_open_legs(
            trade,
            trading_mode=self.session.trading_mode,
        )
        if fraction >= Decimal("1") and owns_entire_exchange_bucket:
            await self._cancel_unowned_take_profit_reservations_for_full_close(trade)
        await self._cancel_existing_trade_protection(
            trade,
            cancel_take_profits=True,
            cancel_stop_loss=False,
        )
        order, requested_quantity = await self._submit_exchange_close_order(trade, fraction)
        self._account_coordinator.mark_order_owner(
            order.order_id,
            trade,
            trading_mode=self.session.trading_mode,
        )
        confirmed_order, fills = await self._futures_client.wait_for_order_fill(
            symbol=trade.symbol,
            order_id=order.order_id,
            use_demo_symbol=self._use_demo_exchange_symbol(),
            timeout_seconds=float(self.settings.LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS),
        )
        if confirmed_order is None:
            raise ValueError(f"Toobit did not return close order {order.order_id} in history")
        status = confirmed_order.status.upper()
        if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            raise ValueError(
                "Toobit close order "
                f"{confirmed_order.order_id} finished with status "
                f"{confirmed_order.status}"
            )
        executed_contract_qty = (
            sum((fill.qty for fill in fills), Decimal("0"))
            if fills
            else confirmed_order.executed_qty
        )
        if executed_contract_qty <= 0:
            raise ValueError(
                f"Toobit close order {confirmed_order.order_id} did not fill any quantity"
            )
        if not fills:
            raise ValueError(
                f"Toobit close order {confirmed_order.order_id} filled but returned no trade fills"
            )
        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(f"Contract spec unavailable for {trade.symbol}")
        executed_quantity = _from_exchange_contract_quantity(executed_contract_qty, spec)
        close_order_ids = [confirmed_order.order_id]
        realized_pnl = sum((fill.realized_pnl for fill in fills), Decimal("0"))
        fees = sum((fill.commission for fill in fills), Decimal("0"))
        avg_price = confirmed_order.avg_price
        if avg_price <= 0 and fills:
            weighted_notional = sum((fill.price * fill.qty for fill in fills), Decimal("0"))
            weighted_qty = sum((fill.qty for fill in fills), Decimal("0"))
            if weighted_qty > 0:
                avg_price = weighted_notional / weighted_qty
        weighted_close_notional = (
            avg_price * executed_quantity
            if avg_price > 0 and executed_quantity > 0
            else Decimal("0")
        )
        weighted_close_qty = (
            executed_quantity if avg_price > 0 and executed_quantity > 0 else Decimal("0")
        )
        processed_close_fills = list(fills)
        exchange_position: Any | None = None
        exchange_remaining_quantity: Decimal | None = None
        if fraction >= Decimal("1") and owns_entire_exchange_bucket:
            (
                exchange_position,
                exchange_remaining_quantity,
            ) = await self._fetch_trade_exchange_position_quantity(
                trade=trade,
                spec=spec,
            )
            reconcile_attempts = max(
                1,
                int(getattr(self.settings, "LIVE_TRADING_CLOSE_RECONCILE_ATTEMPTS", 3)),
            )
            attempt = 1
            while (
                exchange_remaining_quantity is not None
                and exchange_remaining_quantity > Decimal("0")
                and attempt < reconcile_attempts
            ):
                residual_order, _ = await self._submit_exchange_close_quantity_order(
                    trade,
                    exchange_remaining_quantity,
                )
                self._account_coordinator.mark_order_owner(
                    residual_order.order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
                (
                    residual_confirmed_order,
                    residual_fills,
                ) = await self._futures_client.wait_for_order_fill(
                    symbol=trade.symbol,
                    order_id=residual_order.order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                    timeout_seconds=float(self.settings.LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS),
                )
                if residual_confirmed_order is None:
                    raise ValueError(
                        f"Toobit did not return close order {residual_order.order_id} in history"
                    )
                residual_status = residual_confirmed_order.status.upper()
                if residual_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    raise ValueError(
                        "Toobit close order "
                        f"{residual_confirmed_order.order_id} finished with status "
                        f"{residual_confirmed_order.status}"
                    )
                residual_contract_qty = (
                    sum((fill.qty for fill in residual_fills), Decimal("0"))
                    if residual_fills
                    else residual_confirmed_order.executed_qty
                )
                if residual_contract_qty <= 0:
                    raise ValueError(
                        "Toobit residual close order "
                        f"{residual_confirmed_order.order_id} did not fill any quantity"
                    )
                if not residual_fills:
                    raise ValueError(
                        "Toobit residual close order "
                        f"{residual_confirmed_order.order_id} filled but returned no trade fills"
                    )
                residual_executed_quantity = _from_exchange_contract_quantity(
                    residual_contract_qty,
                    spec,
                )
                residual_realized_pnl = sum(
                    (fill.realized_pnl for fill in residual_fills),
                    Decimal("0"),
                )
                residual_fees = sum((fill.commission for fill in residual_fills), Decimal("0"))
                processed_close_fills.extend(residual_fills)
                residual_avg_price = residual_confirmed_order.avg_price
                if residual_avg_price <= 0:
                    residual_weighted_notional = sum(
                        (fill.price * fill.qty for fill in residual_fills),
                        Decimal("0"),
                    )
                    if residual_contract_qty > 0:
                        residual_avg_price = residual_weighted_notional / residual_contract_qty
                executed_quantity += residual_executed_quantity
                realized_pnl += residual_realized_pnl
                fees += residual_fees
                close_order_ids.append(residual_confirmed_order.order_id)
                if residual_avg_price > 0 and residual_executed_quantity > 0:
                    weighted_close_notional += residual_avg_price * residual_executed_quantity
                    weighted_close_qty += residual_executed_quantity
                (
                    exchange_position,
                    exchange_remaining_quantity,
                ) = await self._fetch_trade_exchange_position_quantity(
                    trade=trade,
                    spec=spec,
                )
                attempt += 1
            if exchange_remaining_quantity is not None:
                executed_quantity = max(
                    Decimal("0"),
                    trade.remaining_quantity - exchange_remaining_quantity,
                ).quantize(Decimal("0.00000001"))
            if weighted_close_qty > 0:
                avg_price = (weighted_close_notional / weighted_close_qty).quantize(
                    Decimal("0.00000001")
                )
        self._apply_exchange_close_result(
            trade=trade,
            requested_quantity=requested_quantity,
            executed_quantity=executed_quantity,
            avg_price=avg_price,
            realized_pnl=realized_pnl,
            fees=fees,
            reason=reason,
            order_id=",".join(close_order_ids),
            message=message,
        )
        self._record_processed_exchange_fills(trade, processed_close_fills)
        trade.exchange_position = exchange_position
        if not trade.is_open and trade.sl_order_id is not None:
            try:
                await self._cancel_existing_trade_protection(
                    trade,
                    cancel_take_profits=False,
                    cancel_stop_loss=True,
                )
            except Exception as exc:
                self._log_event(
                    logging.WARNING,
                    "live_trading.exchange_stop_cleanup_after_close_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
        try:
            await self._refresh_account()
        except Exception as exc:
            self._log_event(
                logging.DEBUG,
                "live_trading.account_refresh_after_close_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
        if trade.is_open:
            try:
                await self._ensure_trade_protection_after_open(trade)
            except Exception as exc:
                trade.last_exchange_sync_error = str(exc)
                self._log_event(
                    logging.ERROR,
                    "live_trading.exchange_protection_refresh_after_close_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
                raise
        self._log_event(
            logging.INFO,
            "live_trading.exchange_close_completed",
            reason=reason,
            requested_quantity=str(requested_quantity),
            executed_quantity=str(executed_quantity),
            realized_pnl=str(realized_pnl),
            fees=str(fees),
            average_price=str(avg_price),
            order_id=",".join(close_order_ids),
            **self._trade_log_fields(trade),
        )
        return _ExchangeCloseResult(
            order_id=",".join(close_order_ids),
            status=confirmed_order.status,
            avg_price=avg_price,
            executed_quantity=executed_quantity,
            realized_pnl=realized_pnl,
            fees=fees,
        )

    async def _cancel_unowned_take_profit_reservations_for_full_close(
        self,
        trade: LiveTrade,
    ) -> int:
        """Release stale Triak TP orders before flattening an exclusive bucket.

        Toobit excludes quantity reserved by regular close orders from a market
        close. A TP left by a closed logical trade can therefore make a full
        close fill only partially. Only Triak-prefixed, unowned close-limit
        orders are eligible; manual orders and orders owned by another active
        logical trade remain untouched.
        """

        if self._futures_client is None:
            return 0
        close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        current_trade_prefix = f"triak_tp_{trade.trade_id}_"
        open_orders = await self._futures_client.get_open_orders(
            trade.symbol,
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )
        canceled = 0
        for order in open_orders:
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            order_id = str(getattr(order, "order_id", "") or "")
            if (
                str(getattr(order, "side", "")).upper() != close_side
                or str(getattr(order, "order_type", "")).upper() != "LIMIT"
                or not client_order_id.startswith("triak_tp_")
                or client_order_id.startswith(current_trade_prefix)
                or not self._account_coordinator.order_is_available(
                    order_id,
                    trade.trade_id,
                )
            ):
                continue
            await self._futures_client.cancel_order(
                symbol=trade.symbol,
                order_id=order_id,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            canceled += 1
            self._log_event(
                logging.WARNING,
                "live_trading.orphan_take_profit_reservation_canceled",
                orphan_order_id=order_id,
                orphan_client_order_id=client_order_id,
                orphan_quantity=str(getattr(order, "orig_qty", Decimal("0"))),
                orphan_price=str(getattr(order, "price", Decimal("0"))),
                **self._trade_log_fields(trade),
            )
        if canceled:
            self._log_event(
                logging.WARNING,
                "live_trading.orphan_take_profit_cleanup_completed",
                canceled_order_count=canceled,
                **self._trade_log_fields(trade),
            )
        return canceled

    def _apply_exchange_close_result(
        self,
        *,
        trade: LiveTrade,
        requested_quantity: Decimal,
        executed_quantity: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
        fees: Decimal,
        reason: str,
        order_id: str,
        message: MessageAttribution | None,
        quantity_already_applied: Decimal = Decimal("0"),
    ) -> None:
        unapplied_quantity = max(
            Decimal("0"),
            executed_quantity - quantity_already_applied,
        )
        closed_quantity = min(trade.remaining_quantity, unapplied_quantity)
        trade.realized_pnl += realized_pnl
        trade.fees += fees
        trade.remaining_quantity = max(Decimal("0"), trade.remaining_quantity - closed_quantity)
        if avg_price > 0:
            trade.exit_price = avg_price
            trade.mark_price = avg_price
        if trade.pending_fill_audit_quantity <= Decimal("0"):
            trade.last_exchange_sync_error = None
        attribution = message or MessageAttribution(
            message_id=0,
            channel_id=trade.channel_id,
            channel_label=trade.channel_label,
            message_preview=f"exchange close {reason}",
            message_date=_utc_now(),
            action="closed" if trade.remaining_quantity <= 0 else "partial_close",
        )
        attribution.action = "closed" if trade.remaining_quantity <= 0 else "partial_close"
        attribution.notes.append(
            f"exchange_close order={order_id} requested_qty={requested_quantity} "
            f"executed_qty={executed_quantity} already_applied_qty={quantity_already_applied} "
            f"remaining_reduction={closed_quantity} avg_price={avg_price} "
            f"realized_pnl={realized_pnl} fees={fees}"
        )
        trade.add_attribution(attribution)
        self._book_trade_realized_totals(trade)
        if trade.remaining_quantity <= Decimal("0.000000001"):
            trade.remaining_quantity = Decimal("0")
            trade.status = "closed"
            trade.unrealized_pnl = Decimal("0")
            trade.close_reason = reason
            trade.closed_at = _utc_now()
            self._account_coordinator.unregister_trade(trade.trade_id)
        else:
            trade.status = "partial_close"
            self._register_account_trade(trade)

    def _book_trade_realized_totals(self, trade: LiveTrade) -> None:
        pnl_delta = trade.realized_pnl - trade.realized_pnl_booked
        fee_delta = trade.fees - trade.fees_booked
        if pnl_delta:
            self.session.total_realized_pnl += pnl_delta
            trade.realized_pnl_booked = trade.realized_pnl
        if fee_delta:
            self.session.total_fees += fee_delta
            trade.fees_booked = trade.fees

    def _consume_pending_fill_audit(
        self,
        *,
        trade: LiveTrade,
        executed_quantity: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return accountable, already-applied quantity and the fill booking ratio."""

        accountable_quantity = trade.remaining_quantity + trade.pending_fill_audit_quantity
        applied_quantity = min(accountable_quantity, max(Decimal("0"), executed_quantity))
        quantity_already_applied = min(
            trade.pending_fill_audit_quantity,
            applied_quantity,
        )
        trade.pending_fill_audit_quantity = max(
            Decimal("0"),
            trade.pending_fill_audit_quantity - quantity_already_applied,
        )
        applied_ratio = (
            applied_quantity / executed_quantity
            if executed_quantity > Decimal("0")
            else Decimal("0")
        )
        return applied_quantity, quantity_already_applied, applied_ratio

    def _apply_strategy_after_exchange_target_fill(
        self,
        *,
        trade: LiveTrade,
        target_index: int,
        attribution: MessageAttribution,
    ) -> None:
        if self._strategy is None:
            trade.targets_hit = max(trade.targets_hit, target_index + 1)
            return
        trade.targets_hit = max(trade.targets_hit, target_index + 1)
        previous_stop_loss = trade.stop_loss
        if self._reconcile_strategy_stop_loss_after_targets(trade):
            attribution.notes.append(f"next_stop_loss={trade.stop_loss}")
        elif previous_stop_loss == trade.stop_loss:
            attribution.notes.append(f"next_stop_loss_unchanged={trade.stop_loss}")

    def _reconcile_strategy_stop_loss_after_targets(self, trade: LiveTrade) -> bool:
        """Replay completed target actions and retain the most protective stop."""

        if self._strategy is None or trade.targets_hit <= 0 or not trade.take_profits:
            return False
        completed_targets = min(trade.targets_hit, len(trade.take_profits))
        last_target_index = completed_targets - 1
        action = self._strategy.get_target_hit_action(
            targets_hit_so_far=last_target_index,
            remaining_targets_including_this=len(trade.take_profits) - last_target_index,
            entry_price=trade.entry_price,
            take_profits=trade.take_profits,
        )
        new_stop_loss = getattr(action, "new_stop_loss", None)
        desired_stop = (
            new_stop_loss if isinstance(new_stop_loss, Decimal) else trade.entry_price
        )
        current_stop = trade.stop_loss
        if current_stop is not None:
            desired_stop = (
                max(current_stop, desired_stop)
                if trade.side == TradeSide.LONG.value
                else min(current_stop, desired_stop)
            )
        if desired_stop == current_stop:
            return False
        trade.stop_loss = desired_stop
        if trade.message_history:
            trade.message_history[-1].notes.append(
                "strategy_stop_reconciled_after_targets="
                f"{trade.targets_hit}:{desired_stop}"
            )
        self._log_event(
            logging.INFO,
            "live_trading.strategy_stop_reconciled_after_targets",
            previous_stop_loss=self._decimal_text(current_stop),
            reconciled_stop_loss=self._decimal_text(desired_stop),
            **self._trade_log_fields(trade),
        )
        return True

    async def _fetch_trade_exchange_position_quantity(
        self,
        *,
        trade: LiveTrade,
        spec: Any,
    ) -> tuple[Any | None, Decimal | None]:
        if self._futures_client is None:
            return None, None
        positions = await self._futures_client.get_open_positions(
            trade.symbol,
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )
        exchange_position = self._find_matching_exchange_position(
            trade=trade,
            positions=positions,
        )
        if exchange_position is None:
            return None, Decimal("0")
        contract_quantity = getattr(
            exchange_position,
            "quantity",
            getattr(exchange_position, "position", Decimal("0")),
        )
        return (
            exchange_position,
            _from_exchange_contract_quantity(contract_quantity, spec).quantize(
                Decimal("0.00000001")
            ),
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _get_mark_price(self, symbol: str) -> Decimal | None:
        if self._futures_client is None:
            return None
        try:
            return await self._futures_client.get_mark_price(
                symbol,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        except Exception:
            return None

    @staticmethod
    def _trade_position_side(trade: LiveTrade) -> str:
        return "LONG" if trade.side == "long" else "SHORT"

    def _find_matching_exchange_position(
        self,
        *,
        trade: LiveTrade,
        positions: list[Any],
    ) -> Any | None:
        expected_symbol = canonical_market_symbol(trade.symbol)
        expected_side = self._trade_position_side(trade)
        for position in positions:
            position_symbol = canonical_market_symbol(
                getattr(position, "symbol_internal", None) or getattr(position, "symbol", None)
            )
            position_side = str(getattr(position, "side", "")).upper()
            quantity = getattr(position, "quantity", getattr(position, "position", Decimal("0")))
            if position_symbol != expected_symbol:
                continue
            if position_side != expected_side:
                continue
            if quantity <= Decimal("0"):
                continue
            return position
        return None

    def _allocate_exchange_position_to_trade(
        self,
        trade: LiveTrade,
        position: LiveExchangePositionSnapshot | None,
    ) -> LiveExchangePositionSnapshot | None:
        """Allocate an aggregate exchange bucket to one logical channel leg."""

        if position is None:
            return None
        logical_total = self._account_coordinator.bucket_logical_quantity(
            trading_mode=self.session.trading_mode,
            symbol=trade.symbol,
            side=trade.side,
        )
        if logical_total <= Decimal("0") or trade.remaining_quantity <= Decimal("0"):
            return position
        ratio = min(
            Decimal("1"),
            max(Decimal("0"), trade.remaining_quantity / logical_total),
        )
        return position.model_copy(
            update={
                "quantity": position.quantity * ratio,
                "available": position.available * ratio,
                "margin": position.margin * ratio,
                "unrealized_pnl": position.unrealized_pnl * ratio,
                "realized_pnl": position.realized_pnl * ratio,
            }
        )

    def _reconcile_trade_quantity_to_exchange(
        self,
        *,
        trade: LiveTrade,
        exchange_quantity: Decimal,
    ) -> bool:
        """Clamp execution state to an authoritative, smaller exchange allocation."""

        normalized = max(exchange_quantity, Decimal("0")).quantize(Decimal("0.00000001"))
        tolerance = Decimal("0.00000001")
        if normalized <= Decimal("0") or normalized + tolerance >= trade.remaining_quantity:
            return False
        previous = trade.remaining_quantity
        trade.remaining_quantity = normalized
        trade.pending_fill_audit_quantity += previous - normalized
        trade.status = "partial_close"
        trade.updated_at = _utc_now()
        issue = (
            f"exchange quantity drift for {trade.trade_id} requires fill audit: "
            f"local={previous}, exchange={normalized}, "
            f"pending={trade.pending_fill_audit_quantity}"
        )
        trade.last_exchange_sync_error = issue
        attribution = MessageAttribution(
            message_id=0,
            channel_id=trade.channel_id,
            channel_label=trade.channel_label,
            message_preview="exchange quantity drift reconciliation",
            message_date=_utc_now(),
            action="partial_close",
            notes=[
                f"exchange_quantity_authoritative={previous}->{normalized}",
                "realized_pnl_pending_fill_audit",
            ],
        )
        trade.add_attribution(attribution)
        self._register_account_trade(trade)
        self._account_coordinator.block_trade_bucket(
            trade,
            trading_mode=self.session.trading_mode,
            reason=issue,
        )
        self._set_session_health_issue(issue, critical=True)
        self._log_event(
            logging.CRITICAL,
            "live_trading.exchange_quantity_drift_reconciled",
            previous_remaining_quantity=str(previous),
            authoritative_exchange_quantity=str(normalized),
            **self._trade_log_fields(trade),
        )
        return True

    def _set_session_health_issue(self, issue: str, *, critical: bool) -> None:
        if issue not in self.session.health_issues:
            self.session.health_issues.append(issue)
        self.session.health_status = "critical" if critical else "degraded"
        self.session.new_entries_blocked = self.session.new_entries_blocked or critical
        self.session.last_error = issue
        if issue not in self.session.errors:
            self.session.errors.append(issue)
        self.session.last_update_at = _utc_now()
        self.store.save_session(self.session)

    async def _ensure_trade_still_open_on_exchange(
        self,
        *,
        context: ChannelContext,
        trade: LiveTrade,
        reason: str,
    ) -> bool:
        if trade.is_waiting_entry:
            return True
        if self._futures_client is None:
            return True
        try:
            positions = await self._futures_client.get_open_positions(
                trade.symbol,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        except Exception as exc:
            trade.last_exchange_sync_error = str(exc)
            return True
        if self._find_matching_exchange_position(trade=trade, positions=positions) is not None:
            self._clear_exchange_position_miss_state(trade)
            return True
        if not self._should_confirm_exchange_position_missing(trade, reason=reason):
            self.store.save_trade(trade)
            self._emit_trade_update(trade)
            return True
        await self._resolve_confirmed_missing_exchange_position(
            context=context,
            trade=trade,
            reason=reason,
        )
        return False

    async def _resolve_confirmed_missing_exchange_position(
        self,
        *,
        context: ChannelContext | None,
        trade: LiveTrade,
        reason: str,
        symbol_user_trades: list[Any] | None = None,
        history_orders: list[Any] | None = None,
    ) -> None:
        """Recover close fills when possible, otherwise trust confirmed zero exposure."""

        if self._futures_client is None or not trade.is_open:
            return
        recovered_user_trades = symbol_user_trades
        recovered_history_orders = history_orders
        if recovered_user_trades is None:
            try:
                recovered_user_trades = await self._futures_client.get_user_trades(
                    trade.symbol,
                    limit=100,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                recovered_user_trades = []
                self._log_event(
                    logging.WARNING,
                    "live_trading.missing_position_user_trades_query_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
        if recovered_history_orders is None:
            try:
                recovered_history_orders = await self._futures_client.get_order_history(
                    trade.symbol,
                    limit=100,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                recovered_history_orders = []
                self._log_event(
                    logging.WARNING,
                    "live_trading.missing_position_order_history_query_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
        try:
            await self._reconcile_missing_exchange_position_fills(
                trade=trade,
                symbol_user_trades=recovered_user_trades,
                history_orders=recovered_history_orders,
            )
        except Exception as exc:
            self._log_event(
                logging.WARNING,
                "live_trading.missing_position_fill_recovery_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
        if not trade.is_open or trade.signal_id not in self._open_trades:
            return
        self._mark_trade_closed_on_exchange(
            context=context,
            trade=trade,
            reason=f"confirmed_exchange_position_missing:{reason}",
        )

    def _mark_trade_reconciliation_required(self, *, trade: LiveTrade, reason: str) -> None:
        issue = (
            f"exchange reconciliation required for {trade.trade_id}: {reason}; "
            "exchange position is missing and no complete owned close-fill history was available"
        )
        trade.last_exchange_sync_error = issue
        trade.updated_at = _utc_now()
        self._account_coordinator.block_trade_bucket(
            trade,
            trading_mode=self.session.trading_mode,
            reason=issue,
        )
        self._set_session_health_issue(issue, critical=True)
        self.store.save_trade(trade)
        self._emit_trade_update(trade)
        self._log_event(
            logging.CRITICAL,
            "live_trading.exchange_position_reconciliation_required",
            reason=reason,
            **self._trade_log_fields(trade),
        )

    def _clear_exchange_position_miss_state(
        self,
        trade: LiveTrade,
        *,
        recovered: bool = True,
    ) -> None:
        if recovered:
            reconciliation_prefix = (
                f"exchange reconciliation required for {trade.trade_id}:"
            )
            if any(
                issue.startswith(reconciliation_prefix)
                for issue in self.session.health_issues
            ):
                self._clear_trade_health_issue(
                    trade,
                    prefix=reconciliation_prefix,
                )
        missing_since = trade.exchange_position_missing_since
        confirmations = trade.exchange_position_missing_confirmations
        if recovered and (missing_since is not None or confirmations > 0):
            elapsed_seconds = (
                max(0, int((_utc_now() - missing_since).total_seconds()))
                if missing_since is not None
                else 0
            )
            self._log_event(
                logging.INFO,
                "live_trading.exchange_position_missing_recovered",
                confirmations=confirmations,
                elapsed_seconds=elapsed_seconds,
                **self._trade_log_fields(trade),
            )
        trade.exchange_position_missing_since = None
        trade.exchange_position_missing_confirmations = 0
        if self._is_pending_exchange_position_miss_error(trade.last_exchange_sync_error):
            trade.last_exchange_sync_error = None

    @staticmethod
    def _is_pending_exchange_position_miss_error(error: str | None) -> bool:
        return bool(error and "pending_confirmation" in error)

    def _should_confirm_exchange_position_missing(
        self,
        trade: LiveTrade,
        *,
        reason: str,
    ) -> bool:
        now = _utc_now()
        if trade.exchange_position_missing_since is None:
            trade.exchange_position_missing_since = now
            trade.exchange_position_missing_confirmations = 1
        else:
            trade.exchange_position_missing_confirmations += 1
        required_confirmations = max(
            1,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_EXCHANGE_POSITION_MISS_CONFIRMATIONS",
                    1,
                )
            ),
        )
        grace_seconds = max(
            0,
            int(
                getattr(
                    self.settings,
                    "LIVE_TRADING_EXCHANGE_POSITION_MISS_GRACE_SECONDS",
                    0,
                )
            ),
        )
        elapsed_seconds = int((now - trade.exchange_position_missing_since).total_seconds())
        pending_state = (
            f"{reason}: pending_confirmation "
            f"{trade.exchange_position_missing_confirmations}/{required_confirmations} "
            f"elapsed={elapsed_seconds}s"
        )
        self._log_event(
            logging.INFO,
            "live_trading.exchange_position_missing_pending",
            reason=reason,
            confirmations=trade.exchange_position_missing_confirmations,
            required_confirmations=required_confirmations,
            elapsed_seconds=elapsed_seconds,
            grace_seconds=grace_seconds,
            pending_state=pending_state,
            **self._trade_log_fields(trade),
        )
        trade.last_exchange_sync_error = pending_state
        if trade.exchange_position_missing_confirmations < required_confirmations:
            return False
        return elapsed_seconds >= grace_seconds

    def _mark_trade_closed_on_exchange(
        self,
        *,
        context: ChannelContext | None,
        trade: LiveTrade,
        reason: str,
    ) -> None:
        if trade.signal_id not in self._open_trades:
            return
        unaccounted_quantity = max(Decimal("0"), trade.remaining_quantity)
        trade.status = "closed"
        trade.close_reason = reason
        trade.closed_at = _utc_now()
        trade.remaining_quantity = Decimal("0")
        trade.pending_fill_audit_quantity += unaccounted_quantity
        trade.unrealized_pnl = Decimal("0")
        trade.exchange_position = None
        trade.sl_order_id = None
        trade.tp_order_ids = []
        trade.tp_order_plan = []
        trade.required_tp_order_count = 0
        self._clear_exchange_position_miss_state(trade, recovered=False)
        trade.last_exchange_sync_error = None
        trade.add_attribution(
            MessageAttribution(
                message_id=0,
                channel_id=trade.channel_id,
                channel_label=trade.channel_label,
                message_preview="exchange position missing during sync",
                message_date=_utc_now(),
                action="closed",
                notes=[
                    reason,
                    "exchange_zero_exposure_authoritative",
                    "close_fill_history_unavailable",
                    f"unaccounted_close_quantity={unaccounted_quantity}",
                ],
            )
        )
        for prefix in (
            f"protection unavailable for {trade.trade_id}:",
            f"exchange quantity drift for {trade.trade_id}",
            f"exchange reconciliation required for {trade.trade_id}:",
            f"range_entry_cancel_unconfirmed:{trade.trade_id}:",
        ):
            self._clear_trade_health_issue(trade, prefix=prefix)
        self._open_trades.pop(trade.signal_id, None)
        self._account_coordinator.unregister_trade(trade.trade_id)
        self.session.open_positions_count = max(0, self.session.open_positions_count - 1)
        self.session.closed_trades_count += 1
        self.store.save_trade(trade)
        self.store.save_session(self.session)
        self._emit_trade_update(trade)
        if context is not None:
            self._mark_signal_terminal(
                context=context,
                signal_id=trade.signal_id,
                status=SignalStatus.CLOSED,
                trade=trade,
            )
        self._log_event(
            logging.WARNING,
            "live_trading.trade_auto_closed_from_confirmed_zero_position",
            reason=reason,
            close_fill_history_available=False,
            unaccounted_close_quantity=str(unaccounted_quantity),
            **self._trade_log_fields(trade),
        )

    async def _sync_trade_protection(
        self,
        trade: LiveTrade,
        *,
        refresh_take_profits: bool = True,
        refresh_stop_loss: bool = True,
    ) -> None:
        async with self._account_coordinator.execution_guard():
            await self._sync_trade_protection_unlocked(
                trade,
                refresh_take_profits=refresh_take_profits,
                refresh_stop_loss=refresh_stop_loss,
            )

    async def _sync_trade_protection_unlocked(
        self,
        trade: LiveTrade,
        *,
        refresh_take_profits: bool = True,
        refresh_stop_loss: bool = True,
    ) -> None:
        if self._futures_client is None or not trade.is_open:
            return
        if self._reconcile_strategy_stop_loss_after_targets(trade):
            refresh_stop_loss = True
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_sync_started",
            refresh_take_profits=refresh_take_profits,
            refresh_stop_loss=refresh_stop_loss,
            **self._protection_log_fields(trade),
            **self._trade_log_fields(trade),
        )
        stop_restored = self._restore_default_stop_loss_if_missing(trade)
        targets_restored = self._restore_default_take_profits_if_missing(trade)
        if stop_restored:
            refresh_stop_loss = True
        if targets_restored:
            refresh_take_profits = True
        stop_loss, normalized_take_profits = await self._normalize_trade_protection_levels(trade)
        if stop_loss is not None:
            assert self._pm is not None
            normalized_stop_risk = self._pm.clamp_trade_stop_loss(
                trade=trade,
                stop_loss=stop_loss,
            )
            if normalized_stop_risk.was_capped:
                self._log_event(
                    logging.ERROR,
                    "live_trading.exchange_stop_loss_risk_rejected",
                    requested_stop_loss=self._decimal_text(stop_loss),
                    would_be_capped_stop_loss=self._decimal_text(normalized_stop_risk.stop_loss),
                    risk_budget=self._decimal_text(normalized_stop_risk.risk_budget),
                    risk_amount=self._decimal_text(normalized_stop_risk.risk_amount),
                    **self._trade_log_fields(trade),
                )
                raise ValueError(
                    "Stop loss exceeds the position risk budget; refusing to move "
                    f"the requested stop ({stop_loss}->{normalized_stop_risk.stop_loss})"
                )
        trade.stop_loss = stop_loss
        trade.take_profits = normalized_take_profits
        pending_take_profits = trade.take_profits[trade.targets_hit :]
        if stop_loss is None:
            raise ValueError(
                f"No valid stop loss available for {trade.symbol}; refusing protection sync"
            )
        if not pending_take_profits:
            raise ValueError(
                f"No valid take profits available for {trade.symbol}; refusing protection sync"
            )
        tp_orders: list[tuple[int, Decimal, Decimal]] = []
        if refresh_take_profits:
            spec: Any | None = None
            if self._futures_client is not None:
                try:
                    spec = await self._futures_client.get_contract_spec(trade.symbol)
                except Exception as exc:
                    log.debug(
                        "Failed to fetch contract spec for TP ladder normalization %s: %s",
                        trade.symbol,
                        exc,
                    )
            if spec is not None:
                plan = self._plan_exchange_take_profit_ladder_for_current_quantity(
                    trade=trade,
                    spec=spec,
                )
                if plan is not None:
                    self._apply_exchange_take_profit_ladder_plan(
                        trade=trade,
                        plan=plan,
                        reason="contract_size",
                    )
            tp_orders = self._exchange_take_profit_orders(trade, spec=spec)
            if not tp_orders:
                raise ValueError(
                    f"No executable take-profit orders available for {trade.symbol}; "
                    "existing protection was left untouched"
                )
            trade.required_tp_order_count = len(tp_orders)
        await self._refresh_trade_protection_ids(trade)
        if refresh_take_profits and (trade.tp_order_ids or trade.tp_order_plan):
            await self._cancel_existing_trade_protection(
                trade,
                cancel_take_profits=True,
                cancel_stop_loss=False,
            )
        close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        previous_stop = (
            trade.sl_order_id,
            trade.sl_order_client_id,
            trade.sl_order_api_version,
        )
        if refresh_stop_loss and previous_stop[0] is None:
            side = "LONG" if trade.side == "long" else "SHORT"
            await self._submit_exchange_stop_loss(
                trade=trade,
                side=side,
                stop_loss=stop_loss,
            )
        if refresh_take_profits:
            trade.tp_order_ids = []
            trade.tp_order_plan = []
            for target_index, tp_price, tp_quantity in tp_orders:
                client_order_id = self._make_tp_client_order_id(trade, target_index)
                order = await self._futures_client.place_order(
                    symbol=trade.symbol,
                    side=close_side,
                    order_type="LIMIT",
                    quantity=tp_quantity,
                    price=tp_price,
                    client_order_id=client_order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
                trade.tp_order_ids.append(order.order_id)
                self._account_coordinator.mark_order_owner(
                    order.order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
                trade.tp_order_plan.append(
                    LiveTakeProfitOrderPlan(
                        target_index=target_index,
                        price=tp_price,
                        quantity=tp_quantity,
                        order_id=order.order_id,
                        client_order_id=client_order_id,
                    )
                )
        if refresh_stop_loss and previous_stop[0] is not None:
            side = "LONG" if trade.side == "long" else "SHORT"
            await self._submit_exchange_stop_loss(
                trade=trade,
                side=side,
                stop_loss=stop_loss,
            )
            if previous_stop[2] != "v2" and not previous_stop[1]:
                # The v1 endpoint can replace the exchange-side order with a new id.
                # Preserve the old id until submission succeeds, then rediscover it.
                trade.sl_order_id = None
                trade.sl_order_api_version = None
            if previous_stop[2] == "v2" or previous_stop[1]:
                await self._cancel_specific_stop_order(
                    trade=trade,
                    order_id=previous_stop[0],
                    client_order_id=previous_stop[1],
                    api_version=previous_stop[2],
                )
        await self._refresh_trade_protection_ids(trade)
        if not self._tracked_trade_has_required_protection(trade):
            raise ValueError(
                "Exchange protection verification failed after sync "
                f"(sl_present={trade.sl_order_id is not None}, "
                f"tp_orders={len(trade.tp_order_ids)}, "
                f"required_tp_orders={trade.required_tp_order_count})"
            )
        self._persist_trade_runtime_state(trade)
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_sync_persisted",
            normalized_stop_loss=self._decimal_text(stop_loss),
            normalized_take_profit_count=len(normalized_take_profits),
            synced_tp_order_count=len(trade.tp_order_ids),
            **self._protection_log_fields(trade),
            **self._trade_log_fields(trade),
        )

    def _restore_default_stop_loss_if_missing(self, trade: LiveTrade) -> bool:
        if trade.stop_loss is not None and trade.stop_loss > Decimal("0"):
            return False
        if self._pm is None or self._strategy is None:
            raise ValueError(
                "Cannot restore missing stop loss without position manager and strategy"
            )
        default_stop = self._pm.build_default_stop_loss(
            trade=trade,
            strategy=self._strategy,
        )
        if default_stop is None:
            raise ValueError(
                f"No valid stop loss available for {trade.symbol}; refusing protection sync"
            )
        trade.stop_loss = default_stop
        if trade.message_history:
            trade.message_history[-1].notes.append(f"default_stop_loss_restored={default_stop}")
        self._log_event(
            logging.WARNING,
            "live_trading.default_stop_loss_restored",
            restored_stop_loss=str(default_stop),
            **self._trade_log_fields(trade),
        )
        return True

    def _restore_default_take_profits_if_missing(self, trade: LiveTrade) -> bool:
        """Restore strategy targets before cancelling or replacing exchange protection."""
        if trade.take_profits[trade.targets_hit :]:
            return False
        if self._pm is None or self._strategy is None:
            raise ValueError(
                "Cannot restore missing take profits without position manager and strategy"
            )
        defaults = self._pm.build_default_take_profits(trade=trade, strategy=self._strategy)
        if not defaults:
            raise ValueError(
                f"No valid take profits available for {trade.symbol}; refusing protection sync"
            )
        trade.take_profits = trade.take_profits[: trade.targets_hit] + defaults
        if trade.message_history:
            trade.message_history[-1].notes.append(
                "default_take_profits_restored=" + ",".join(str(item) for item in defaults)
            )
        self._log_event(
            logging.WARNING,
            "live_trading.default_take_profits_restored",
            restored_take_profits=[str(item) for item in defaults],
            **self._trade_log_fields(trade),
        )
        return True

    async def _submit_exchange_stop_loss(
        self,
        *,
        trade: LiveTrade,
        side: str,
        stop_loss: Decimal,
    ) -> None:
        assert self._futures_client is not None
        if self._use_owned_v2_stop_orders():
            client_order_id = f"triak_sl_{trade.trade_id}_{uuid.uuid4().hex[:10]}"
            order = await self._futures_client.place_owned_stop_market(
                symbol=trade.symbol,
                position_side=side,
                stop_price=stop_loss,
                quantity=trade.remaining_quantity,
                client_order_id=client_order_id,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            if not order.order_id:
                raise ValueError("Toobit v2 stop order did not return an order id")
            trade.sl_order_id = order.order_id
            trade.sl_order_client_id = order.client_order_id or client_order_id
            trade.sl_order_api_version = "v2"
            self._account_coordinator.mark_order_owner(
                trade.sl_order_id,
                trade,
                trading_mode=self.session.trading_mode,
            )
            return

        trade.sl_order_client_id = None
        trade.sl_order_api_version = "v1"
        try:
            await self._futures_client.set_trading_stop(
                symbol=trade.symbol,
                side=side,
                stop_loss=stop_loss,
                sl_quantity=trade.remaining_quantity,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        except Exception as exc:
            if not self._should_retry_stop_loss_without_quantity(exc):
                raise
            self._log_event(
                logging.WARNING,
                "live_trading.exchange_stop_loss_retry_without_quantity",
                stop_order_side=side,
                requested_stop_loss=str(stop_loss),
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
            if trade.message_history:
                trade.message_history[-1].notes.append(
                    "exchange_sl_size_fallback=omit_quantity_after_position_limit"
                )
            await self._futures_client.set_trading_stop(
                symbol=trade.symbol,
                side=side,
                stop_loss=stop_loss,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )

    @staticmethod
    def _should_retry_stop_loss_without_quantity(exc: Exception) -> bool:
        if isinstance(exc, ToobitAPIError):
            message = str(exc).lower()
            if "position limit" in message and "stopprofitloss" in message:
                return True
        message = str(exc).lower()
        return "stopprofitloss order position limit" in message

    def _use_owned_v2_stop_orders(self) -> bool:
        return (
            getattr(
                self.settings,
                "LIVE_TRADING_USE_OWNED_V2_STOP_ORDERS",
                False,
            )
            is True
        )

    def _make_tp_client_order_id(self, trade: LiveTrade, target_index: int) -> str:
        return f"triak_tp_{trade.trade_id}_{target_index + 1}_{uuid.uuid4().hex[:10]}"

    @staticmethod
    def _extract_tp_target_index(trade: LiveTrade, client_order_id: str | None) -> int | None:
        if not client_order_id:
            return None
        prefix = f"triak_tp_{trade.trade_id}_"
        if not client_order_id.startswith(prefix):
            return None
        suffix = client_order_id[len(prefix) :]
        target_token = suffix.split("_", 1)[0]
        try:
            target_index = int(target_token) - 1
        except ValueError:
            return None
        return target_index if target_index >= 0 else None

    async def _normalize_trade_protection_levels(
        self,
        trade: LiveTrade,
    ) -> tuple[Decimal | None, list[Decimal]]:
        if self._futures_client is None:
            return trade.stop_loss, list(trade.take_profits)
        side = "LONG" if trade.side == "long" else "SHORT"
        return await self._futures_client.normalize_trade_protection(
            symbol=trade.symbol,
            side=side,
            stop_loss=trade.stop_loss,
            take_profits=list(trade.take_profits),
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )

    def _persist_trade_runtime_state(self, trade: LiveTrade) -> None:
        context = self._contexts.get(trade.channel_id)
        if context is not None:
            state = context.get_signal(trade.signal_id)
            if state is not None:
                self._sync_signal_snapshot(context=context, state=state, trade=trade)
        self.store.save_trade(trade)
        self._emit_trade_update(trade)

    async def _cancel_specific_stop_order(
        self,
        *,
        trade: LiveTrade,
        order_id: str,
        client_order_id: str | None,
        api_version: str | None,
    ) -> None:
        """Cancel an obsolete stop only after its replacement is confirmed."""

        assert self._futures_client is not None
        try:
            if api_version == "v2" or client_order_id:
                await self._futures_client.cancel_algo_order(
                    order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            else:
                await self._futures_client.cancel_order(
                    symbol=trade.symbol,
                    order_id=order_id,
                    order_type="STOP",
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
        except Exception as exc:
            self._log_event(
                logging.WARNING,
                "live_trading.exchange_obsolete_stop_cancel_failed",
                obsolete_stop_order_id=order_id,
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
            return
        self._account_coordinator.release_order_owner(order_id, trade.trade_id)

    async def _cancel_existing_trade_protection(
        self,
        trade: LiveTrade,
        *,
        cancel_take_profits: bool = True,
        cancel_stop_loss: bool = True,
    ) -> None:
        if self._futures_client is None:
            return
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_cancel_started",
            cancel_take_profits=cancel_take_profits,
            cancel_stop_loss=cancel_stop_loss,
            **self._trade_log_fields(trade),
        )
        tp_order_prefix = f"triak_tp_{trade.trade_id}_"
        target_close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        cancellation_errors: list[str] = []
        if cancel_take_profits:
            tp_order_ids = set(trade.tp_order_ids)
            tp_order_ids.update(
                item.order_id for item in trade.tp_order_plan if item.order_id is not None
            )
            try:
                regular_orders = await self._futures_client.get_open_orders(
                    trade.symbol,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                regular_orders = []
                self._log_event(
                    logging.DEBUG,
                    "live_trading.exchange_tp_query_before_cancel_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
            for order in regular_orders:
                if order.side.upper() != target_close_side:
                    continue
                if order.order_type.upper() != "LIMIT":
                    continue
                if not order.client_order_id.startswith(tp_order_prefix):
                    continue
                tp_order_ids.add(order.order_id)
            for order_id in sorted(tp_order_ids):
                try:
                    await self._futures_client.cancel_order(
                        symbol=trade.symbol,
                        order_id=order_id,
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                    )
                except Exception as exc:
                    cancellation_errors.append(f"tp:{order_id}:{exc}")
                    self._log_event(
                        logging.DEBUG,
                        "live_trading.exchange_tp_cancel_failed",
                        order_id=order_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **self._trade_log_fields(trade),
                    )
            trade.tp_order_ids = []
            trade.tp_order_plan = []
        if cancel_stop_loss:
            stop_order_ids = {trade.sl_order_id} if trade.sl_order_id else set()
            if trade.sl_order_api_version == "v2" or trade.sl_order_client_id:
                try:
                    algo_orders = await self._futures_client.get_open_algo_orders(
                        trade.symbol,
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                    )
                except Exception as exc:
                    algo_orders = []
                    self._log_event(
                        logging.DEBUG,
                        "live_trading.exchange_owned_stop_query_before_cancel_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **self._trade_log_fields(trade),
                    )
                expected_client_id = trade.sl_order_client_id
                expected_prefix = f"triak_sl_{trade.trade_id}_"
                for order in algo_orders:
                    if expected_client_id and order.client_order_id == expected_client_id:
                        stop_order_ids.add(order.order_id)
                    elif order.client_order_id.startswith(expected_prefix):
                        stop_order_ids.add(order.order_id)
                for order_id in sorted(item for item in stop_order_ids if item):
                    try:
                        await self._futures_client.cancel_algo_order(
                            order_id,
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                    except Exception as exc:
                        cancellation_errors.append(f"sl:{order_id}:{exc}")
                        self._log_event(
                            logging.DEBUG,
                            "live_trading.exchange_owned_stop_cancel_failed",
                            order_id=order_id,
                            error_type=type(exc).__name__,
                            error=str(exc),
                            **self._trade_log_fields(trade),
                        )
            elif trade.sl_order_id:
                try:
                    await self._futures_client.cancel_order(
                        symbol=trade.symbol,
                        order_id=trade.sl_order_id,
                        order_type="STOP",
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                    )
                except Exception as exc:
                    cancellation_errors.append(f"sl:{trade.sl_order_id}:{exc}")
                    self._log_event(
                        logging.DEBUG,
                        "live_trading.exchange_legacy_stop_cancel_failed",
                        order_id=trade.sl_order_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **self._trade_log_fields(trade),
                    )
            elif not self._use_owned_v2_stop_orders():
                # Compatibility-only path for explicitly disabled v2 owned
                # stops. Production defaults to v2 and never enters this broad
                # legacy discovery path.
                orders = await self._futures_client.get_open_orders(
                    trade.symbol,
                    order_type="STOP_PROFIT_LOSS",
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
                target_order_prefix = (
                    "STOP_LONG_" if trade.side == "long" else "STOP_SHORT_"
                )
                for order in orders:
                    if order.side.upper() != target_close_side:
                        continue
                    if not order.order_type.upper().startswith(target_order_prefix):
                        continue
                    try:
                        await self._futures_client.cancel_order(
                            symbol=trade.symbol,
                            order_id=order.order_id,
                            order_type="STOP",
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                    except Exception as exc:
                        cancellation_errors.append(f"sl:{order.order_id}:{exc}")
            trade.sl_order_id = None
            trade.sl_order_client_id = None
            trade.sl_order_api_version = None
        if cancellation_errors:
            raise ValueError(
                "Exchange protection cancellation failed: " + "; ".join(cancellation_errors)
            )
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_cancel_completed",
            **self._trade_log_fields(trade),
        )

    async def _refresh_trade_protection_ids(self, trade: LiveTrade) -> None:
        if self._futures_client is None:
            return
        tracked_sl_order_id = trade.sl_order_id
        tracked_sl_client_id = trade.sl_order_client_id
        tracked_sl_api_version = trade.sl_order_api_version
        tracked_tp_order_plan = list(trade.tp_order_plan)
        regular_open_orders = await self._futures_client.get_open_orders(
            trade.symbol,
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )
        owned_algo_orders: list[Any] = []
        if self._use_owned_v2_stop_orders() or trade.sl_order_api_version == "v2":
            owned_algo_orders = await self._futures_client.get_open_algo_orders(
                trade.symbol,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        legacy_open_orders: list[Any] = []
        if trade.sl_order_api_version != "v2" and not trade.sl_order_client_id:
            legacy_open_orders = await self._futures_client.get_open_orders(
                trade.symbol,
                order_type="STOP_PROFIT_LOSS",
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        tp_order_ids = [
            order.order_id
            for order in regular_open_orders
            if order.side.upper() == ("SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE")
            and order.order_type.upper() == "LIMIT"
            and order.client_order_id.startswith(f"triak_tp_{trade.trade_id}_")
        ]
        tracked_by_order_id = {
            item.order_id: item for item in trade.tp_order_plan if item.order_id is not None
        }
        tracked_by_client_order_id = {
            item.client_order_id: item
            for item in trade.tp_order_plan
            if item.client_order_id is not None
        }
        tp_order_plan: list[LiveTakeProfitOrderPlan] = []
        for order in regular_open_orders:
            if order.side.upper() != ("SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"):
                continue
            if order.order_type.upper() != "LIMIT":
                continue
            if not order.client_order_id.startswith(f"triak_tp_{trade.trade_id}_"):
                continue
            existing = tracked_by_order_id.get(order.order_id) or tracked_by_client_order_id.get(
                order.client_order_id
            )
            target_index = (
                existing.target_index
                if existing is not None
                else self._extract_tp_target_index(trade, order.client_order_id)
            )
            if target_index is None:
                target_index = trade.targets_hit + len(tp_order_plan)
            tp_order_plan.append(
                LiveTakeProfitOrderPlan(
                    target_index=target_index,
                    price=(
                        existing.price
                        if existing is not None
                        else getattr(order, "price", Decimal("0"))
                    ),
                    quantity=existing.quantity if existing is not None else Decimal("0"),
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                )
            )
            self._account_coordinator.mark_order_owner(
                order.order_id,
                trade,
                trading_mode=self.session.trading_mode,
            )
        discovered_tp_ids = {
            item.order_id for item in tp_order_plan if item.order_id is not None
        }
        terminal_order_statuses = {
            "ORDER_CANCELED",
            "CANCELED",
            "CANCELLED",
            "ORDER_REJECTED",
            "REJECTED",
            "ORDER_FAILED",
            "FAILED",
            "EXPIRED",
            "ORDER_FILLED",
            "FILLED",
        }
        for tracked_tp_plan in tracked_tp_order_plan:
            if (
                tracked_tp_plan.order_id is None
                or tracked_tp_plan.order_id in discovered_tp_ids
            ):
                continue
            try:
                confirmed_tp = await self._futures_client.get_order(
                    symbol=trade.symbol,
                    order_id=tracked_tp_plan.order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception:
                continue
            if confirmed_tp.status.upper() in terminal_order_statuses:
                continue
            tp_order_plan.append(tracked_tp_plan)
            discovered_tp_ids.add(tracked_tp_plan.order_id)
            self._account_coordinator.mark_order_owner(
                tracked_tp_plan.order_id,
                trade,
                trading_mode=self.session.trading_mode,
            )
        tp_order_plan.sort(key=lambda item: item.target_index)
        sl_order_id: str | None = None
        sl_order_client_id: str | None = None
        expected_sl_prefix = f"triak_sl_{trade.trade_id}_"
        for order in owned_algo_orders:
            if not (
                order.client_order_id.startswith(expected_sl_prefix)
                or (
                    trade.sl_order_client_id
                    and order.client_order_id == trade.sl_order_client_id
                )
                or (trade.sl_order_id and order.order_id == trade.sl_order_id)
            ):
                continue
            sl_order_id = order.order_id
            sl_order_client_id = order.client_order_id or trade.sl_order_client_id
            self._account_coordinator.mark_order_owner(
                order.order_id,
                trade,
                trading_mode=self.session.trading_mode,
            )
            break
        if sl_order_id is None and trade.sl_order_id:
            for order in legacy_open_orders:
                if order.order_id != trade.sl_order_id:
                    continue
                sl_order_id = order.order_id
                self._account_coordinator.mark_order_owner(
                    order.order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
                break
        if (
            sl_order_id is None
            and not self._use_owned_v2_stop_orders()
            and tracked_sl_order_id is None
        ):
            target_order_prefix = (
                "STOP_LONG_" if trade.side == "long" else "STOP_SHORT_"
            )
            target_close_side = (
                "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
            )
            for order in legacy_open_orders:
                order_type = order.order_type.upper()
                if order.stop_price <= 0 or "STOP_" not in order_type:
                    continue
                if order.side.upper() != target_close_side:
                    continue
                if not order_type.startswith(target_order_prefix):
                    continue
                if "LOSS" in order_type:
                    sl_order_id = order.order_id
                    self._account_coordinator.mark_order_owner(
                        order.order_id,
                        trade,
                        trading_mode=self.session.trading_mode,
                    )
        if (
            sl_order_id is None
            and tracked_sl_order_id
            and tracked_sl_api_version == "v2"
        ):
            try:
                tracked_order = await self._futures_client.get_algo_order(
                    order_id=tracked_sl_order_id,
                    orig_client_order_id=tracked_sl_client_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception:
                tracked_order = None
            if tracked_order is not None and tracked_order.status.upper() not in {
                "ORDER_CANCELED",
                "CANCELED",
                "CANCELLED",
                "ORDER_REJECTED",
                "REJECTED",
                "ORDER_FAILED",
                "FAILED",
                "EXPIRED",
            }:
                sl_order_id = tracked_order.order_id or tracked_sl_order_id
                sl_order_client_id = (
                    tracked_order.client_order_id or tracked_sl_client_id
                )
                self._account_coordinator.mark_order_owner(
                    sl_order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
        trade.tp_order_ids = (
            [item.order_id for item in tp_order_plan if item.order_id is not None]
            if tp_order_plan
            else tp_order_ids
        )
        trade.tp_order_plan = tp_order_plan
        trade.sl_order_id = sl_order_id
        trade.sl_order_client_id = sl_order_client_id
        trade.sl_order_api_version = (
            ("v2" if sl_order_client_id is not None else "v1")
            if sl_order_id is not None
            else None
        )

    def _trade_has_exchange_protection(self, trade: LiveTrade) -> bool:
        return bool(trade.sl_order_id and (trade.tp_order_ids or trade.tp_order_plan))

    @staticmethod
    def _tracked_trade_has_required_protection(trade: LiveTrade) -> bool:
        if trade.stop_loss is None or trade.stop_loss <= Decimal("0"):
            return False
        if not trade.take_profits[trade.targets_hit :]:
            return False
        if trade.sl_order_id is None:
            return False
        required_tp_orders = max(1, trade.required_tp_order_count)
        tracked_order_ids = {item for item in trade.tp_order_ids if item}
        tracked_order_ids.update(
            item.order_id for item in trade.tp_order_plan if item.order_id is not None
        )
        return len(tracked_order_ids) >= required_tp_orders

    def _detach_trade_protection_order(self, trade: LiveTrade, order_id: str) -> None:
        if trade.sl_order_id == order_id:
            trade.sl_order_id = None
            trade.sl_order_client_id = None
            trade.sl_order_api_version = None
        if trade.tp_order_ids:
            trade.tp_order_ids = [item for item in trade.tp_order_ids if item != order_id]
        if trade.tp_order_plan:
            trade.tp_order_plan = [
                item for item in trade.tp_order_plan if item.order_id != order_id
            ]

    async def _reconcile_exchange_trade_protection(
        self,
        *,
        trade: LiveTrade,
        open_regular_orders: list[Any],
        open_protection_orders: list[Any],
        symbol_user_trades: list[Any],
        allow_repair: bool = True,
    ) -> None:
        if self._futures_client is None:
            return
        if allow_repair:
            try:
                defaults_restored = self._restore_default_take_profits_if_missing(trade)
            except ValueError as exc:
                trade.last_exchange_sync_error = str(exc)
                self._log_event(
                    logging.ERROR,
                    "live_trading.default_take_profits_restore_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
                await self._enforce_trade_protection_or_flatten(
                    trade=trade,
                    reason="default_take_profits_unavailable",
                )
                return
            if defaults_restored:
                await self._enforce_trade_protection_or_flatten(
                    trade=trade,
                    reason="default_take_profits_restore",
                )
                return
        open_regular_ids = {order.order_id for order in open_regular_orders}
        open_stop_ids = {order.order_id for order in open_protection_orders}
        tracked_tp_orders = list(trade.tp_order_plan)
        if not tracked_tp_orders:
            tracked_tp_orders = [
                LiveTakeProfitOrderPlan(
                    target_index=trade.targets_hit + idx,
                    order_id=order_id,
                )
                for idx, order_id in enumerate(trade.tp_order_ids)
            ]
        filled_tp_orders: list[tuple[int, Any]] = []
        for tracked_order in tracked_tp_orders:
            order_id = tracked_order.order_id
            if order_id is None:
                continue
            if order_id in open_regular_ids:
                continue
            try:
                order = await self._futures_client.get_order(
                    symbol=trade.symbol,
                    order_id=order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                trade.last_exchange_sync_error = str(exc)
                log.debug(
                    "Failed to query TP order %s for %s: %s",
                    order_id,
                    trade.trade_id,
                    exc,
                )
                continue
            status = order.status.upper()
            if status in {
                "ORDER_CANCELED",
                "CANCELED",
                "CANCELLED",
                "ORDER_REJECTED",
                "REJECTED",
                "ORDER_FAILED",
                "FAILED",
                "EXPIRED",
            }:
                self._detach_trade_protection_order(trade, order.order_id)
                continue
            if status not in {"ORDER_FILLED", "FILLED"}:
                continue
            filled_tp_orders.append((tracked_order.target_index, order))

        if filled_tp_orders:
            for target_index, order in sorted(filled_tp_orders, key=lambda item: item[0]):
                await self._apply_exchange_protection_fill(
                    trade=trade,
                    protection_order=order,
                    symbol_user_trades=symbol_user_trades,
                    target_index_override=target_index,
                    sync_protection_after_fill=False,
                )
                if not trade.is_open:
                    return
            if not allow_repair:
                return
            await self._enforce_trade_protection_or_flatten(
                trade=trade,
                reason="multiple_take_profit_fills",
            )
            return

        if trade.sl_order_id is not None:
            for order_id in [trade.sl_order_id]:
                if order_id in open_stop_ids:
                    continue
                try:
                    if trade.sl_order_api_version == "v2":
                        order = await self._futures_client.get_algo_order(
                            order_id=order_id,
                            orig_client_order_id=trade.sl_order_client_id,
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                    else:
                        order = await self._futures_client.get_order(
                            symbol=trade.symbol,
                            order_id=order_id,
                            order_type="STOP",
                            use_demo_symbol=self._use_demo_exchange_symbol(),
                        )
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    log.debug(
                        "Failed to query protection order %s for %s: %s",
                        order_id,
                        trade.trade_id,
                        exc,
                    )
                    continue
                status = order.status.upper()
                if status in {
                    "ORDER_CANCELED",
                    "CANCELED",
                    "CANCELLED",
                    "ORDER_REJECTED",
                    "REJECTED",
                    "ORDER_FAILED",
                    "FAILED",
                    "EXPIRED",
                }:
                    self._detach_trade_protection_order(trade, order.order_id)
                    continue
                if status not in {"ORDER_FILLED", "FILLED"}:
                    continue
                await self._apply_exchange_protection_fill(
                    trade=trade,
                    protection_order=order,
                    symbol_user_trades=symbol_user_trades,
                )
                return
        if not trade.is_open or self._futures_client is None:
            return
        if not allow_repair:
            return
        if self._protection_retry_is_deferred(trade):
            self._log_event(
                logging.DEBUG,
                "live_trading.exchange_protection_repair_deferred",
                retry_at=(
                    trade.protection_retry_at.isoformat()
                    if trade.protection_retry_at is not None
                    else None
                ),
                **self._trade_log_fields(trade),
            )
            return
        if await self._exchange_trade_has_required_protection(trade):
            return
        self._log_event(
            logging.WARNING,
            "live_trading.exchange_protection_gap_detected",
            **self._protection_log_fields(trade),
            **self._trade_log_fields(trade),
        )
        repaired = await self._enforce_trade_protection_or_flatten(
            trade=trade,
            reason="exchange_protection_gap",
        )
        if not repaired:
            return
        self._log_event(
            logging.INFO,
            "live_trading.exchange_protection_gap_repaired",
            **self._protection_log_fields(trade),
            **self._trade_log_fields(trade),
        )

    def _exchange_take_profit_orders(
        self,
        trade: LiveTrade,
        spec: Any | None = None,
    ) -> list[tuple[int, Decimal, Decimal]]:
        desired_orders = self._desired_exchange_take_profit_orders(trade)
        if not desired_orders or spec is None:
            return desired_orders
        return self._normalize_take_profit_orders_for_contract_spec(
            trade=trade,
            desired_orders=desired_orders,
            spec=spec,
        )

    def _desired_exchange_take_profit_orders(
        self,
        trade: LiveTrade,
    ) -> list[tuple[int, Decimal, Decimal]]:
        if self._strategy is None:
            return []
        pending_take_profits = trade.take_profits[trade.targets_hit :]
        if not pending_take_profits:
            return []
        remaining_quantity = trade.remaining_quantity
        orders: list[tuple[int, Decimal, Decimal]] = []
        for local_index, tp_price in enumerate(pending_take_profits):
            absolute_index = trade.targets_hit + local_index
            remaining_targets = len(trade.take_profits) - absolute_index
            action = self._strategy.get_target_hit_action(
                targets_hit_so_far=absolute_index,
                remaining_targets_including_this=remaining_targets,
                entry_price=trade.entry_price,
                take_profits=trade.take_profits,
            )
            if remaining_targets <= 1 or action.close_fraction >= Decimal("1"):
                tp_quantity = remaining_quantity
            else:
                tp_quantity = (remaining_quantity * action.close_fraction).quantize(
                    Decimal("0.00000001")
                )
            if tp_quantity <= 0:
                continue
            orders.append((absolute_index, tp_price, tp_quantity))
            remaining_quantity = max(Decimal("0"), remaining_quantity - tp_quantity)
            if remaining_quantity <= Decimal("0.000000001"):
                break
        return orders

    def _normalize_take_profit_orders_for_contract_spec(
        self,
        *,
        trade: LiveTrade,
        desired_orders: list[tuple[int, Decimal, Decimal]],
        spec: Any,
    ) -> list[tuple[int, Decimal, Decimal]]:
        if not desired_orders:
            return []
        contract_multiplier = self._coerce_decimal(
            getattr(spec, "contract_multiplier", Decimal("0"))
        )
        if contract_multiplier <= 0:
            return []
        step = self._coerce_decimal(getattr(spec, "contract_step_size", Decimal("0")))
        if step <= 0:
            step = Decimal("1")
        min_contract_qty = self._minimum_exchange_contract_quantity(spec)
        total_exchange_qty = self._floor_exchange_contract_quantity(
            trade.remaining_quantity,
            spec,
        )
        if total_exchange_qty <= 0:
            return []

        pending_targets = list(
            enumerate(
                trade.take_profits[trade.targets_hit :],
                start=trade.targets_hit,
            )
        )
        desired_quantity_by_target: dict[int, Decimal] = {}
        for absolute_index, _, quantity in desired_orders:
            desired_quantity_by_target[absolute_index] = quantity / contract_multiplier
        capacity_plan = plan_maximum_take_profit_orders(
            candidates=[
                TakeProfitCandidate(
                    target_index=absolute_index,
                    price=tp_price,
                    preferred_contract_quantity=desired_quantity_by_target.get(
                        absolute_index,
                        Decimal("0"),
                    ),
                )
                for absolute_index, tp_price in pending_targets
            ],
            total_contract_quantity=total_exchange_qty,
            minimum_contract_quantity=min_contract_qty,
            contract_step_size=step,
        )

        normalized_orders: list[tuple[int, Decimal, Decimal]] = []
        for order in capacity_plan.orders:
            asset_qty = _from_exchange_contract_quantity(
                order.contract_quantity,
                spec,
            ).quantize(Decimal("0.00000001"))
            if asset_qty <= 0:
                raise AssertionError("TP planner produced a non-positive asset quantity")
            normalized_orders.append((order.target_index, order.price, asset_qty))
        return normalized_orders

    @staticmethod
    def _floor_exchange_contract_quantity(quantity: Decimal, spec: Any) -> Decimal:
        contract_multiplier = LiveTradingEngine._coerce_decimal(
            getattr(spec, "contract_multiplier", Decimal("0"))
        )
        if quantity <= 0:
            return Decimal("0")
        if contract_multiplier <= 0:
            return quantity
        exchange_qty = quantity / contract_multiplier
        step = LiveTradingEngine._coerce_decimal(getattr(spec, "contract_step_size", Decimal("0")))
        if step > 0:
            exchange_qty = (exchange_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        return max(exchange_qty, Decimal("0"))

    @staticmethod
    def _exchange_quantity_units(quantity: Decimal, step: Decimal) -> int:
        if quantity <= 0 or step <= 0:
            return 0
        return int((quantity / step).to_integral_value(rounding=ROUND_DOWN))

    @staticmethod
    def _exchange_quantity_units_ceiling(quantity: Decimal, step: Decimal) -> int:
        if quantity <= 0 or step <= 0:
            return 0
        return int((quantity / step).to_integral_value(rounding=ROUND_UP))

    @staticmethod
    def _coerce_decimal(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    async def _apply_exchange_protection_fill(
        self,
        *,
        trade: LiveTrade,
        protection_order: Any,
        symbol_user_trades: list[Any],
        target_index_override: int | None = None,
        sync_protection_after_fill: bool = True,
    ) -> None:
        assert self._futures_client is not None
        is_tp = (
            protection_order.order_id in trade.tp_order_ids
            or "PROFIT" in protection_order.order_type.upper()
        )
        current_target_index = (
            target_index_override if target_index_override is not None else trade.targets_hit
        )
        if trade.entry_order_plan:
            if is_tp:
                await self._apply_range_entry_target_cancellation_policy(
                    trade=trade,
                    target_hits=current_target_index + 1,
                    reason=f"exchange_tp{current_target_index + 1}_fill",
                )
            else:
                await self._cancel_range_entry_orders(
                    trade=trade,
                    reason="exchange_stop_loss_fill",
                    reconcile_fills=True,
                    ensure_protection=False,
                )
        executed_order_id = protection_order.executed_order_id or protection_order.order_id
        matching_fills = [
            fill
            for fill in symbol_user_trades
            if fill.order_id in {executed_order_id, protection_order.order_id}
        ]
        fills = self._unprocessed_exchange_fills(trade, matching_fills)
        if not fills:
            if matching_fills:
                self._detach_trade_protection_order(
                    trade,
                    protection_order.order_id,
                )
                return
            raise ValueError(
                "Protection order filled on Toobit but no userTrades were returned for "
                f"{protection_order.order_id}"
            )
        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(f"Contract spec unavailable for {trade.symbol}")
        executed_contract_qty = sum((fill.qty for fill in fills), Decimal("0"))
        executed_quantity = _from_exchange_contract_quantity(executed_contract_qty, spec)
        if executed_quantity <= 0:
            raise ValueError(
                f"Protection order {protection_order.order_id} did not report executed quantity"
            )
        weighted_notional = sum((fill.price * fill.qty for fill in fills), Decimal("0"))
        avg_price = (
            weighted_notional / executed_contract_qty if executed_contract_qty > 0 else Decimal("0")
        )
        (
            applied_quantity,
            quantity_already_applied,
            applied_ratio,
        ) = self._consume_pending_fill_audit(
            trade=trade,
            executed_quantity=executed_quantity,
        )
        realized_pnl = (
            sum((fill.realized_pnl for fill in fills), Decimal("0")) * applied_ratio
        )
        fees = sum((fill.commission for fill in fills), Decimal("0")) * applied_ratio
        reason = f"tp{current_target_index + 1}_hit" if is_tp else "sl_hit"
        attribution = MessageAttribution(
            message_id=0,
            channel_id=trade.channel_id,
            channel_label=trade.channel_label,
            message_preview=("exchange take-profit fill" if is_tp else "exchange stop-loss fill"),
            message_date=_utc_now(),
            action="partial_close" if is_tp else "closed",
            notes=[
                "exchange_protection_fill "
                f"protection_order={protection_order.order_id} "
                f"executed_order={executed_order_id} "
                f"order_type={protection_order.order_type}"
            ],
        )
        self._apply_exchange_close_result(
            trade=trade,
            requested_quantity=applied_quantity,
            executed_quantity=applied_quantity,
            avg_price=avg_price,
            realized_pnl=realized_pnl,
            fees=fees,
            reason=reason,
            order_id=executed_order_id,
            message=attribution,
            quantity_already_applied=quantity_already_applied,
        )
        self._record_processed_exchange_fills(trade, fills)
        self._detach_trade_protection_order(trade, protection_order.order_id)
        if is_tp:
            self._apply_strategy_after_exchange_target_fill(
                trade=trade,
                target_index=current_target_index,
                attribution=attribution,
            )
        if trade.pending_fill_audit_quantity <= Decimal("0"):
            self._clear_trade_health_issue(
                trade,
                prefix=f"exchange quantity drift for {trade.trade_id} requires fill audit:",
            )
        if trade.is_open:
            if not sync_protection_after_fill:
                return
            await self._enforce_trade_protection_or_flatten(
                trade=trade,
                reason="take_profit_fill_rearm",
            )
            return
        trade.sl_order_id = None
        trade.tp_order_ids = []
        trade.tp_order_plan = []
        trade.required_tp_order_count = 0
        self._finalize_closed_trade(trade)
        context = self._contexts.get(trade.channel_id)
        if context is not None:
            self._mark_signal_terminal(
                context=context,
                signal_id=trade.signal_id,
                status=SignalStatus.CLOSED,
                trade=trade,
            )

    def _finalize_closed_trade(self, trade: LiveTrade) -> None:
        self._clear_exchange_position_miss_state(trade, recovered=False)
        for prefix in (
            f"protection unavailable for {trade.trade_id}:",
            f"exchange quantity drift for {trade.trade_id}",
            f"exchange reconciliation required for {trade.trade_id}:",
            f"range_entry_cancel_unconfirmed:{trade.trade_id}:",
        ):
            self._clear_trade_health_issue(trade, prefix=prefix)
        self._book_trade_realized_totals(trade)
        if self.session.trading_mode == "demo" and not self._uses_exchange_execution():
            self.session.paper_balance += trade.realized_pnl + trade.margin
        self.session.open_positions_count = max(0, self.session.open_positions_count - 1)
        self.session.closed_trades_count += 1
        if trade.realized_pnl >= 0:
            self.session.wins += 1
        else:
            self.session.losses += 1
        self._open_trades.pop(trade.signal_id, None)
        self._account_coordinator.unregister_trade(trade.trade_id)
        self.store.save_trade(trade)
        self._emit_trade_update(trade)
        self._log_event(
            logging.INFO,
            "live_trading.trade_closed",
            realized_pnl=str(trade.realized_pnl),
            close_reason=trade.close_reason,
            **self._trade_log_fields(trade),
        )

    def _channel_label(self, channel_id: str) -> str:
        for ch in self.session.channels:
            slug = ch.rsplit("/", 1)[-1].lstrip("@")
            cid = channel_id.lstrip("@").lower()
            if cid == slug.lower() or ch == channel_id or ch.lower() == channel_id.lower():
                if slug.lstrip("-").isdigit():
                    return slug
                return f"@{slug}"
        # If it looks like a URL already, extract slug
        if channel_id.startswith("https://t.me/"):
            return f"@{channel_id.rsplit('/', 1)[-1]}"
        if channel_id.lstrip("-").isdigit():
            return channel_id
        return f"@{channel_id.lstrip('@').lstrip('-')}" if channel_id else "unknown"

    def _channel_input(self, channel_id: str) -> str:
        for ch in self.session.channels:
            slug = ch.rsplit("/", 1)[-1].lstrip("@")
            cid = channel_id.lstrip("@").lower()
            if cid == slug.lower() or ch == channel_id or ch.lower() == channel_id.lower():
                return ch
        return channel_id

    def _push_trace(self, trace: LiveMessageTrace) -> None:
        self._message_traces.append(trace)
        if len(self._message_traces) > 200:
            self._message_traces = self._message_traces[-200:]
        self.store.save_message_trace(self.session.session_id, trace)
        if self.notifier:
            try:
                self.notifier(
                    {
                        "type": "live_message",
                        "message": trace.model_dump(mode="json"),
                    }
                )
            except Exception:
                pass

    def _emit_trace_update(
        self,
        *,
        signal_id: str,
        status: str,
        note: str = "",
        trade_id: str | None = None,
    ) -> None:
        """Update the most recent trace for this signal and re-broadcast."""
        for trace in reversed(self._message_traces):
            if trace.signal_id == signal_id:
                trace.final_status = status
                if note:
                    trace.effect_summary = note
                if trade_id:
                    trace.trade_id = trade_id
                self.store.save_message_trace(self.session.session_id, trace)
                if self.notifier:
                    try:
                        self.notifier(
                            {
                                "type": "live_message",
                                "message": trace.model_dump(mode="json"),
                            }
                        )
                    except Exception:
                        pass
                return

    def _emit_session_update(self) -> None:
        if not self.notifier:
            return
        try:
            self.notifier(
                {
                    "type": "live_session",
                    "session": self.session.model_dump(mode="json"),
                }
            )
        except Exception:
            pass

    def _emit_trade_update(self, trade: LiveTrade) -> None:
        if not self.notifier:
            return
        try:
            self.notifier(
                {
                    "type": "live_trade",
                    "trade": trade.model_dump(mode="json"),
                }
            )
        except Exception:
            pass

    async def _reconcile_pending_entry(
        self,
        *,
        trade: LiveTrade,
        history_orders: list[Any],
        open_orders: list[Any],
        symbol_user_trades: list[Any],
    ) -> bool:
        """Keep a submitted entry pending until Toobit confirms an actual fill."""
        if self._futures_client is None:
            return not trade.is_waiting_entry
        if trade.entry_order_plan:
            spec = await self._futures_client.get_contract_spec(trade.symbol)
            if spec is None:
                trade.last_exchange_sync_error = (
                    f"Contract spec unavailable while reconciling {trade.symbol} range entry"
                )
                return False
            if (
                trade.entry_order_expires_at is not None
                and _utc_now() >= trade.entry_order_expires_at
                and self._range_entry_plan_has_pending_orders(trade)
            ):
                await self._cancel_range_entry_orders(
                    trade=trade,
                    reason="range_entry_ladder_expired",
                    reconcile_fills=True,
                    spec=spec,
                    ensure_protection=True,
                )
                if trade.entry_filled_quantity <= Decimal("0"):
                    await self._finalize_waiting_entry_without_fill(
                        trade=trade,
                        reason="entry_order_expired",
                        signal_status=SignalStatus.EXPIRED,
                    )
                    return False
                trade.entry_order_expires_at = None
                self._persist_trade_runtime_state(trade)
                return True
            return await self._reconcile_range_entry_orders(
                trade=trade,
                history_orders=history_orders,
                open_orders=open_orders,
                symbol_user_trades=symbol_user_trades,
                spec=spec,
            )
        if not trade.is_waiting_entry:
            return not trade.is_waiting_entry
        if not trade.entry_order_id:
            trade.last_exchange_sync_error = "waiting entry has no exchange order id"
            self._log_event(
                logging.ERROR,
                "live_trading.pending_entry_invalid",
                reason=trade.last_exchange_sync_error,
                **self._trade_log_fields(trade),
            )
            return False

        previous_runtime_state = (
            trade.entry_order_status,
            trade.last_exchange_sync_error,
        )
        now = _utc_now()
        entry_expired = (
            trade.entry_order_expires_at is not None and now >= trade.entry_order_expires_at
        )
        candidates = [*open_orders, *history_orders]
        order = next(
            (
                item
                for item in candidates
                if getattr(item, "order_id", None) == trade.entry_order_id
            ),
            None,
        )
        if order is None:
            try:
                order = await self._futures_client.get_order(
                    symbol=trade.symbol,
                    order_id=trade.entry_order_id,
                    order_type=("STOP" if trade.entry_order_type == "STOP" else None),
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                trade.last_exchange_sync_error = (
                    f"pending_entry_query_failed: {type(exc).__name__}: {exc}"
                )
                self._log_event(
                    logging.WARNING,
                    "live_trading.pending_entry_query_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **self._trade_log_fields(trade),
                )
                return False

        status = str(order.status).upper()
        trade.entry_order_status = status
        executed_qty = getattr(order, "executed_qty", Decimal("0"))
        if status in {"PARTIALLY_CANCELED", "PARTIALLY_CANCELLED"}:
            if executed_qty <= Decimal("0"):
                await self._finalize_waiting_entry_without_fill(
                    trade=trade,
                    reason=(
                        "entry_order_expired" if entry_expired else f"entry_order_{status.lower()}"
                    ),
                    signal_status=(
                        SignalStatus.EXPIRED if entry_expired else SignalStatus.CANCELLED
                    ),
                )
                return False
        if status in {
            "CANCELED",
            "CANCELLED",
            "ORDER_CANCELED",
            "REJECTED",
            "ORDER_REJECTED",
            "FAILED",
            "ORDER_FAILED",
            "EXPIRED",
        }:
            await self._finalize_waiting_entry_without_fill(
                trade=trade,
                reason=(
                    "entry_order_expired" if entry_expired else f"entry_order_{status.lower()}"
                ),
                signal_status=(
                    SignalStatus.EXPIRED
                    if status == "EXPIRED" or entry_expired
                    else SignalStatus.CANCELLED
                ),
            )
            return False
        order_has_fill = self._entry_order_has_fill(order)
        if entry_expired and not order_has_fill:
            await self._cancel_waiting_entry(
                trade=trade,
                reason="entry_order_expired",
                signal_status=SignalStatus.EXPIRED,
            )
            return False
        if not order_has_fill:
            trade.last_exchange_sync_error = None
            if previous_runtime_state != (
                trade.entry_order_status,
                trade.last_exchange_sync_error,
            ):
                trade.updated_at = _utc_now()
                self._persist_trade_runtime_state(trade)
            return False

        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            trade.last_exchange_sync_error = (
                f"Contract spec unavailable while activating {trade.symbol}"
            )
            return False
        if not symbol_user_trades:
            try:
                symbol_user_trades = await self._futures_client.get_user_trades(
                    trade.symbol,
                    limit=100,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception:
                symbol_user_trades = []
        fill_order_ids = {trade.entry_order_id}
        executed_order_id = str(getattr(order, "executed_order_id", "") or "")
        if executed_order_id:
            fill_order_ids.add(executed_order_id)
        fills = [
            item for item in symbol_user_trades if getattr(item, "order_id", None) in fill_order_ids
        ]
        average_price = getattr(order, "avg_price", Decimal("0"))
        if not fills and (not average_price or average_price <= Decimal("0")):
            self._mark_trade_reconciliation_required(
                trade=trade,
                reason="partial_entry_fill_details_missing",
            )
            return False
        try:
            await self._activate_filled_entry(
                trade=trade,
                confirmed_order=order,
                fills=fills,
                spec=spec,
            )
        except Exception as exc:
            trade.last_exchange_sync_error = str(exc)
            self._log_event(
                logging.ERROR,
                "live_trading.pending_entry_activation_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
            raise

        context = self._contexts.get(trade.channel_id)
        if context is not None:
            state = context.get_signal(trade.signal_id)
            if state is not None:
                state.status = SignalStatus.OPEN
                state.updated_at = _utc_now()
                self._sync_signal_snapshot(context=context, state=state, trade=trade)
        self._persist_trade_runtime_state(trade)
        self._log_event(
            logging.INFO,
            "live_trading.pending_entry_filled",
            order_id=trade.entry_order_id,
            **self._trade_log_fields(trade),
        )
        return True

    async def _reconcile_range_entry_orders(
        self,
        *,
        trade: LiveTrade,
        history_orders: list[Any],
        open_orders: list[Any],
        symbol_user_trades: list[Any],
        spec: Any,
        ensure_protection: bool = True,
        finalize_unfilled: bool = True,
    ) -> bool:
        """Apply every new range-leg fill exactly once and protect partial exposure."""

        if self._futures_client is None or not trade.entry_order_plan:
            return not trade.is_waiting_entry
        candidates = [*open_orders, *history_orders]
        by_order_id = {
            str(getattr(order, "order_id", "")): order
            for order in candidates
            if getattr(order, "order_id", None)
        }
        terminal = {
            "CANCELED",
            "CANCELLED",
            "ORDER_CANCELED",
            "REJECTED",
            "ORDER_REJECTED",
            "FAILED",
            "ORDER_FAILED",
            "EXPIRED",
            "FILLED",
            "ORDER_FILLED",
            "PARTIALLY_CANCELED",
            "PARTIALLY_CANCELLED",
            "NOT_SUBMITTED",
        }
        if not symbol_user_trades and any(
            self._entry_order_has_fill(order) for order in by_order_id.values()
        ):
            try:
                symbol_user_trades = await self._futures_client.get_user_trades(
                    trade.symbol,
                    limit=100,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception:
                symbol_user_trades = []

        all_terminal = True
        all_orders_resolved = True
        fill_records: list[Any] = []
        for leg in trade.entry_order_plan:
            order = by_order_id.get(leg.order_id or "")
            if order is None and leg.client_order_id:
                order = next(
                    (
                        candidate
                        for candidate in candidates
                        if str(getattr(candidate, "client_order_id", ""))
                        == leg.client_order_id
                    ),
                    None,
                )
            if order is None and not leg.order_id and not leg.client_order_id:
                all_terminal = False
                all_orders_resolved = False
                continue
            if order is None:
                try:
                    order = await self._futures_client.get_order(
                        symbol=trade.symbol,
                        order_id=leg.order_id,
                        orig_client_order_id=(
                            leg.client_order_id if not leg.order_id else None
                        ),
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                    )
                except Exception as exc:
                    all_terminal = False
                    all_orders_resolved = False
                    trade.last_exchange_sync_error = (
                        f"range_entry_query_failed: {type(exc).__name__}: {exc}"
                    )
                    continue
            resolved_order_id = str(getattr(order, "order_id", "") or "")
            if not resolved_order_id:
                all_terminal = False
                all_orders_resolved = False
                continue
            if not leg.order_id:
                leg.order_id = resolved_order_id
                self._account_coordinator.mark_order_owner(
                    resolved_order_id,
                    trade,
                    trading_mode=self.session.trading_mode,
                )
            status = str(getattr(order, "status", "")).upper() or leg.status
            leg.status = status
            if status not in terminal:
                all_terminal = False
            executed_contract_qty = self._coerce_decimal(
                getattr(order, "executed_qty", Decimal("0"))
            )
            matching_fills = [
                fill
                for fill in symbol_user_trades
                if str(getattr(fill, "order_id", "")) == leg.order_id
            ]
            if matching_fills:
                executed_contract_qty = max(
                    executed_contract_qty,
                    sum((fill.qty for fill in matching_fills), Decimal("0")),
                )
                weighted_qty = sum((fill.qty for fill in matching_fills), Decimal("0"))
                if weighted_qty > 0:
                    leg.average_fill_price = (
                        sum((fill.price * fill.qty for fill in matching_fills), Decimal("0"))
                        / weighted_qty
                    )
                fill_records.extend(matching_fills)
            elif executed_contract_qty > 0:
                average_price = self._coerce_decimal(getattr(order, "avg_price", Decimal("0")))
                if average_price <= 0:
                    all_orders_resolved = False
                    self._mark_trade_reconciliation_required(
                        trade=trade,
                        reason=f"range_entry_fill_details_missing:{leg.order_id}",
                    )
                    continue
                leg.average_fill_price = average_price
            executed_quantity = _from_exchange_contract_quantity(
                executed_contract_qty,
                spec,
            ).quantize(Decimal("0.00000001"))
            if executed_quantity > leg.quantity:
                raise ValueError(
                    f"Range entry {leg.order_id} executed above planned quantity"
                )
            leg.executed_quantity = max(leg.executed_quantity, executed_quantity)

        new_entry_filled_quantity = sum(
            (leg.executed_quantity for leg in trade.entry_order_plan),
            Decimal("0"),
        )
        previous_entry_filled_quantity = trade.entry_filled_quantity
        added_quantity = new_entry_filled_quantity - previous_entry_filled_quantity
        if added_quantity < 0:
            raise ValueError("Range entry reconciliation attempted to reduce filled quantity")

        new_fills = self._unprocessed_exchange_fills(trade, fill_records)
        if new_fills:
            trade.fees += sum((fill.commission for fill in new_fills), Decimal("0"))
            self._record_processed_exchange_fills(trade, new_fills)
            self._book_trade_realized_totals(trade)

        weighted_notional = sum(
            (
                leg.executed_quantity * leg.average_fill_price
                for leg in trade.entry_order_plan
                if leg.executed_quantity > 0
            ),
            Decimal("0"),
        )
        aggregate_entry_price = (
            weighted_notional / new_entry_filled_quantity
            if new_entry_filled_quantity > 0 and weighted_notional > 0
            else Decimal("0")
        )
        entry_price_changed = (
            aggregate_entry_price > 0 and aggregate_entry_price != trade.entry_price
        )
        if added_quantity > 0 or entry_price_changed:
            if weighted_notional <= 0:
                self._mark_trade_reconciliation_required(
                    trade=trade,
                    reason="range_entry_weighted_fill_price_missing",
                )
                return False
            first_fill = previous_entry_filled_quantity <= 0
            trade.entry_filled_quantity = new_entry_filled_quantity
            if added_quantity > 0:
                trade.quantity = new_entry_filled_quantity
                if first_fill:
                    trade.remaining_quantity = new_entry_filled_quantity
                else:
                    trade.remaining_quantity += added_quantity
            trade.entry_price = aggregate_entry_price
            trade.margin = (
                trade.entry_price * trade.quantity / Decimal(str(trade.leverage))
            ).quantize(Decimal("0.00000001"))
            fill_time = _utc_now()
            trade.status = "open" if trade.remaining_quantity == trade.quantity else "partial_close"
            trade.entry_filled_at = trade.entry_filled_at or fill_time
            if first_fill:
                trade.opened_at = fill_time
            trade.updated_at = fill_time
            if added_quantity > 0:
                previous_stop_loss = trade.stop_loss
                protection_notes: list[str] = []
                if first_fill:
                    assert self._pm is not None and self._strategy is not None
                    protection_notes = self._pm.rebase_trade_protection_after_entry_fill(
                        trade=trade,
                        strategy=self._strategy,
                    )
                    if previous_stop_loss is not None and trade.stop_loss is not None:
                        if trade.side == "long":
                            trade.stop_loss = max(previous_stop_loss, trade.stop_loss)
                        else:
                            trade.stop_loss = min(previous_stop_loss, trade.stop_loss)
                else:
                    protection_notes.append(
                        "range_entry_fill_preserved_target_progress="
                        f"targets_hit:{trade.targets_hit},stop_loss:{trade.stop_loss}"
                    )
                if trade.message_history:
                    if first_fill:
                        trade.message_history[-1].action = "opened"
                    trade.message_history[-1].notes.extend(protection_notes)
            self._register_account_trade(trade)
            if ensure_protection:
                await self._enforce_trade_protection_or_flatten(
                    trade=trade,
                    reason="range_entry_fill_protection",
                )
            if trade.is_open and added_quantity > 0 and trade.targets_hit > 0:
                await self._apply_range_entry_target_cancellation_policy(
                    trade=trade,
                    target_hits=trade.targets_hit,
                    reason="range_entry_fill_after_target_progress",
                )

        if all_terminal and all_orders_resolved:
            trade.entry_order_expires_at = None
            trade.entry_order_status = (
                "LADDER_FILLED" if new_entry_filled_quantity > 0 else "LADDER_UNFILLED"
            )
        elif new_entry_filled_quantity > 0:
            trade.entry_order_status = "LADDER_PARTIALLY_FILLED"
        else:
            trade.entry_order_status = "LADDER_PENDING"
        trade.last_exchange_sync_error = (
            None if all_orders_resolved else trade.last_exchange_sync_error
        )
        if (
            all_terminal
            and all_orders_resolved
            and new_entry_filled_quantity <= 0
            and finalize_unfilled
            and trade.signal_id in self._open_trades
        ):
            await self._finalize_waiting_entry_without_fill(
                trade=trade,
                reason="entry_order_ladder_unfilled",
                signal_status=SignalStatus.CANCELLED,
            )
            return False
        if added_quantity > 0:
            context = self._contexts.get(trade.channel_id)
            if context is not None:
                state = context.get_signal(trade.signal_id)
                if state is not None:
                    state.status = SignalStatus.OPEN
                    state.updated_at = _utc_now()
                    self._sync_signal_snapshot(context=context, state=state, trade=trade)
        if trade.is_open:
            self._register_account_trade(trade)
        self._persist_trade_runtime_state(trade)
        return new_entry_filled_quantity > 0

    @staticmethod
    def _range_entry_target_cancel_leg_indexes(
        trade: LiveTrade,
        *,
        target_hits: int,
    ) -> set[int]:
        """Return pending ladder legs that must be retired after target progress."""

        by_index = {leg.leg_index: leg for leg in trade.entry_order_plan}
        range_start = by_index.get(0)
        range_midpoint = by_index.get(1)
        if (
            target_hits >= 2
            and range_start is not None
            and range_start.executed_quantity > Decimal("0")
        ):
            return {1, 2}
        if (
            target_hits >= 1
            and range_midpoint is not None
            and range_midpoint.executed_quantity > Decimal("0")
        ):
            return {2}
        return set()

    async def _apply_range_entry_target_cancellation_policy(
        self,
        *,
        trade: LiveTrade,
        target_hits: int,
        reason: str,
    ) -> None:
        leg_indexes = self._range_entry_target_cancel_leg_indexes(
            trade,
            target_hits=target_hits,
        )
        if not leg_indexes:
            self._log_event(
                logging.INFO,
                "live_trading.range_entry_target_policy_kept_pending_orders",
                target_hits=target_hits,
                reason=reason,
                **self._trade_log_fields(trade),
            )
            return
        await self._cancel_range_entry_orders(
            trade=trade,
            reason=reason,
            reconcile_fills=True,
            ensure_protection=True,
            leg_indexes=leg_indexes,
        )
        self._log_event(
            logging.INFO,
            "live_trading.range_entry_target_policy_cancelled_orders",
            target_hits=target_hits,
            cancelled_leg_indexes=sorted(leg_indexes),
            reason=reason,
            **self._trade_log_fields(trade),
        )

    async def _cancel_range_entry_orders(
        self,
        *,
        trade: LiveTrade,
        reason: str,
        reconcile_fills: bool,
        spec: Any | None = None,
        ensure_protection: bool = False,
        leg_indexes: set[int] | None = None,
    ) -> None:
        """Cancel selected live range legs and reconcile fills that raced cancellation."""

        if self._futures_client is None or not trade.entry_order_plan:
            return
        terminal = {
            "CANCELED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "FILLED",
            "ORDER_FILLED",
            "PARTIALLY_CANCELED",
            "PARTIALLY_CANCELLED",
            "NOT_SUBMITTED",
        }
        confirmed_orders: list[Any] = []
        cleanup_errors: list[str] = []
        for leg in trade.entry_order_plan:
            if leg_indexes is not None and leg.leg_index not in leg_indexes:
                continue
            if not leg.order_id and leg.client_order_id:
                try:
                    discovered = await self._futures_client.get_order(
                        symbol=trade.symbol,
                        orig_client_order_id=leg.client_order_id,
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                    )
                except Exception as exc:
                    cleanup_errors.append(
                        f"discover {leg.label}: {type(exc).__name__}: {exc}"
                    )
                    continue
                leg.order_id = str(getattr(discovered, "order_id", "") or "") or None
                if leg.order_id:
                    leg.status = str(getattr(discovered, "status", "")).upper()
                    confirmed_orders.append(discovered)
                    self._account_coordinator.mark_order_owner(
                        leg.order_id,
                        trade,
                        trading_mode=self.session.trading_mode,
                    )
            if not leg.order_id or leg.status.upper() in terminal:
                continue
            try:
                await self._futures_client.cancel_order(
                    symbol=trade.symbol,
                    order_id=leg.order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
                confirmed = await self._futures_client.get_order(
                    symbol=trade.symbol,
                    order_id=leg.order_id,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                cleanup_errors.append(
                    f"cancel {leg.label}: {type(exc).__name__}: {exc}"
                )
                continue
            confirmed_orders.append(confirmed)
            leg.status = str(getattr(confirmed, "status", "")).upper()
            if leg.status not in terminal:
                cleanup_errors.append(
                    f"unconfirmed {leg.label}: order={leg.order_id}, status={leg.status}"
                )
        if reconcile_fills:
            if spec is None:
                spec = await self._futures_client.get_contract_spec(trade.symbol)
            if spec is None:
                raise ValueError(
                    f"Contract spec unavailable after range entry cancellation for {trade.symbol}"
                )
            fills = await self._futures_client.get_user_trades(
                trade.symbol,
                limit=100,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            await self._reconcile_range_entry_orders(
                trade=trade,
                history_orders=confirmed_orders,
                open_orders=[],
                symbol_user_trades=fills,
                spec=spec,
                ensure_protection=ensure_protection,
                finalize_unfilled=False,
            )
        if cleanup_errors:
            error = "; ".join(cleanup_errors)
            self._mark_trade_reconciliation_required(
                trade=trade,
                reason=f"range_entry_cancel_unconfirmed:{reason}:{error}",
            )
            trade.last_exchange_sync_error = error
            self._persist_trade_runtime_state(trade)
            raise ValueError(error)
        self._log_event(
            logging.INFO,
            "live_trading.range_entry_ladder_cancelled",
            reason=reason,
            cancelled_leg_indexes=(
                sorted(leg_indexes) if leg_indexes is not None else "all_pending"
            ),
            **self._trade_log_fields(trade),
        )

    async def _cancel_waiting_entry(
        self,
        *,
        trade: LiveTrade,
        reason: str,
        signal_status: SignalStatus,
    ) -> None:
        order_was_unfilled = await self._cancel_entry_order_and_confirm_unfilled(
            trade=trade,
            reason=reason,
        )
        if not order_was_unfilled:
            await self._execute_exchange_close(
                trade=trade,
                fraction=Decimal("1"),
                reason=f"{reason}_fill_race_flatten",
            )
            self._finalize_closed_trade(trade)
            context = self._contexts.get(trade.channel_id)
            if context is not None:
                self._mark_signal_terminal(
                    context=context,
                    signal_id=trade.signal_id,
                    status=signal_status,
                    trade=trade,
                )
            return
        await self._finalize_waiting_entry_without_fill(
            trade=trade,
            reason=reason,
            signal_status=signal_status,
        )

    async def _cancel_entry_order_and_confirm_unfilled(
        self,
        *,
        trade: LiveTrade,
        reason: str,
    ) -> bool:
        if trade.entry_order_plan:
            await self._cancel_range_entry_orders(
                trade=trade,
                reason=reason,
                reconcile_fills=True,
                ensure_protection=trade.entry_filled_quantity > 0,
            )
            return trade.entry_filled_quantity <= Decimal("0")
        if self._futures_client is None or not trade.entry_order_id:
            return True
        try:
            await self._futures_client.cancel_order(
                symbol=trade.symbol,
                order_id=trade.entry_order_id,
                order_type=("STOP" if trade.entry_order_type == "STOP" else None),
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            order = await self._futures_client.get_order(
                symbol=trade.symbol,
                order_id=trade.entry_order_id,
                order_type=("STOP" if trade.entry_order_type == "STOP" else None),
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        except Exception as exc:
            trade.last_exchange_sync_error = str(exc)
            self._log_event(
                logging.WARNING,
                "live_trading.pending_entry_cancel_failed",
                reason=reason,
                error_type=type(exc).__name__,
                error=str(exc),
                **self._trade_log_fields(trade),
            )
            raise
        trade.entry_order_status = order.status.upper()
        if not self._entry_order_has_fill(order):
            if trade.entry_order_status not in {
                "CANCELED",
                "CANCELLED",
                "ORDER_CANCELED",
                "REJECTED",
                "ORDER_REJECTED",
                "EXPIRED",
            }:
                raise ValueError(
                    "Entry cancellation was not confirmed by Toobit "
                    f"(status={trade.entry_order_status})"
                )
            return True

        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(
                f"Contract spec unavailable after entry cancellation race for {trade.symbol}"
            )
        fills = await self._futures_client.get_user_trades(
            trade.symbol,
            limit=100,
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )
        fill_order_ids = {trade.entry_order_id}
        executed_order_id = str(getattr(order, "executed_order_id", "") or "")
        if executed_order_id:
            fill_order_ids.add(executed_order_id)
        await self._activate_filled_entry(
            trade=trade,
            confirmed_order=order,
            fills=[item for item in fills if getattr(item, "order_id", None) in fill_order_ids],
            spec=spec,
        )
        self._log_event(
            logging.WARNING,
            "live_trading.pending_entry_cancel_fill_race",
            reason=reason,
            **self._trade_log_fields(trade),
        )
        return False

    async def _replace_pending_entry_order(
        self,
        *,
        trade: LiveTrade,
        parsed: ParsedSignal,
    ) -> str:
        if not trade.is_waiting_entry:
            raise ValueError("Entry order can only be replaced while waiting for fill")
        requested_price = self._entry_reference(parsed)
        if requested_price is None or requested_price <= Decimal("0"):
            raise ValueError("Entry update did not contain a valid price")

        order_was_unfilled = await self._cancel_entry_order_and_confirm_unfilled(
            trade=trade,
            reason="replace_pending_entry",
        )
        if not order_was_unfilled:
            return "Original entry filled during replacement; no duplicate order was submitted"

        previous_price = trade.entry_price
        margin_budget = trade.margin
        trade.entry_price = requested_price
        trade.requested_entry_low = parsed.entry_low
        trade.requested_entry_high = parsed.entry_high
        trade.entry_type = (
            parsed.entry_type.value
            if parsed.entry_type is not EntryType.UNKNOWN
            else EntryType.LIMIT.value
        )
        if parsed.stop_loss is not None:
            trade.requested_stop_loss = parsed.stop_loss
        if parsed.take_profits:
            trade.requested_take_profits = list(parsed.take_profits)
        if parsed.leverage is not None:
            trade.leverage = min(
                max(parsed.leverage, 1),
                self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE,
            )
        if margin_budget > Decimal("0"):
            trade.quantity = (
                margin_budget * Decimal(str(max(trade.leverage, 1))) / trade.entry_price
            ).quantize(Decimal("0.00000001"))
            trade.remaining_quantity = trade.quantity
        trade.entry_order_id = None
        trade.entry_order_client_id = None
        trade.entry_order_type = None
        trade.entry_order_status = None
        trade.entry_submitted_at = None
        trade.entry_order_expires_at = None
        trade.entry_order_plan = []
        trade.entry_planned_quantity = Decimal("0")
        trade.entry_filled_quantity = Decimal("0")
        trade.last_exchange_sync_error = None

        if self._pm is None or self._strategy is None:
            raise ValueError("Trading components are unavailable for entry replacement")
        protection_notes = self._pm.rebase_trade_protection_after_entry_fill(
            trade=trade,
            strategy=self._strategy,
        )
        if trade.message_history:
            trade.message_history[-1].notes.extend(protection_notes)
            trade.message_history[-1].notes.append(
                f"pending_entry_replaced={previous_price}->{requested_price}"
            )
        mark_price = await self._get_mark_price(trade.symbol)
        await self._real_open_position(
            trade,
            current_mark_price=mark_price,
        )
        self._persist_trade_runtime_state(trade)
        if trade.is_waiting_entry:
            return (
                f"Pending {trade.entry_order_type} entry replaced "
                f"{previous_price} → {trade.entry_price}"
            )
        return f"Replacement entry filled at {trade.entry_price}; protection activated"

    async def _finalize_waiting_entry_without_fill(
        self,
        *,
        trade: LiveTrade,
        reason: str,
        signal_status: SignalStatus,
    ) -> None:
        trade.status = "expired" if signal_status is SignalStatus.EXPIRED else "cancelled"
        trade.entry_order_status = signal_status.value.upper()
        trade.close_reason = reason
        trade.closed_at = _utc_now()
        trade.remaining_quantity = Decimal("0")
        trade.unrealized_pnl = Decimal("0")
        self._open_trades.pop(trade.signal_id, None)
        self._account_coordinator.unregister_trade(trade.trade_id)
        self.session.open_positions_count = max(
            0,
            self.session.open_positions_count - 1,
        )
        context = self._contexts.get(trade.channel_id)
        if context is not None:
            self._mark_signal_terminal(
                context=context,
                signal_id=trade.signal_id,
                status=signal_status,
                trade=trade,
            )
        self.store.save_trade(trade)
        self.store.save_session(self.session)
        self._emit_trade_update(trade)
        self._emit_session_update()
        self._log_event(
            logging.INFO,
            "live_trading.pending_entry_finalized",
            reason=reason,
            signal_status=signal_status.value,
            **self._trade_log_fields(trade),
        )

    @staticmethod
    def _is_close_fill_for_trade(trade: LiveTrade, fill: Any) -> bool:
        side = str(getattr(fill, "side", "") or "").strip().upper()
        if trade.side == TradeSide.SHORT.value:
            return side in {"BUY", "BUY_CLOSE", "CLOSE_BUY"}
        if trade.side == TradeSide.LONG.value:
            return side in {"SELL", "SELL_CLOSE", "CLOSE_SELL"}
        return False

    @staticmethod
    def _exchange_fill_datetime(fill: Any) -> datetime | None:
        raw_time = getattr(fill, "time", 0)
        try:
            timestamp_ms = int(raw_time)
        except (TypeError, ValueError):
            return None
        if timestamp_ms <= 0:
            return None
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=timestamp_ms)

    @staticmethod
    def _matching_take_profit_index(
        *,
        trade: LiveTrade,
        order_id: str,
        order_type: str,
        average_price: Decimal,
        tick_size: Decimal,
    ) -> int | None:
        tracked_indexes = {
            item.order_id: item.target_index for item in trade.tp_order_plan if item.order_id
        }
        if order_id in tracked_indexes:
            return tracked_indexes[order_id]
        if order_id in trade.tp_order_ids:
            return trade.targets_hit + trade.tp_order_ids.index(order_id)
        if "LIMIT" not in order_type.upper() or average_price <= 0:
            return None
        pending = list(enumerate(trade.take_profits[trade.targets_hit :], trade.targets_hit))
        if not pending:
            return None
        target_index, target_price = min(
            pending,
            key=lambda item: abs(item[1] - average_price),
        )
        tolerance = max(tick_size, Decimal("0.00000001"))
        if abs(target_price - average_price) <= tolerance:
            return target_index
        return None

    @classmethod
    def _owned_close_order_metadata(
        cls,
        *,
        trade: LiveTrade,
        history_orders: list[Any],
    ) -> dict[str, tuple[str, int | None]]:
        """Map exchange order ids to an owned close kind and optional TP index."""

        owned: dict[str, tuple[str, int | None]] = {}
        for item in trade.tp_order_plan:
            if item.order_id:
                owned[item.order_id] = ("take_profit", item.target_index)
        for index, order_id in enumerate(trade.tp_order_ids, start=trade.targets_hit):
            if order_id:
                owned.setdefault(order_id, ("take_profit", index))
        if trade.sl_order_id:
            owned[trade.sl_order_id] = ("stop_loss", None)

        tp_prefix = f"triak_tp_{trade.trade_id}_"
        sl_prefix = f"triak_sl_{trade.trade_id}_"
        for order in history_orders:
            order_id = str(getattr(order, "order_id", "") or "")
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            if not order_id:
                continue
            if client_order_id.startswith(tp_prefix):
                owned[order_id] = (
                    "take_profit",
                    cls._extract_tp_target_index(trade, client_order_id),
                )
            elif client_order_id.startswith(sl_prefix):
                owned[order_id] = ("stop_loss", None)
        return owned

    async def _reconcile_missing_exchange_position_fills(
        self,
        *,
        trade: LiveTrade,
        symbol_user_trades: list[Any],
        history_orders: list[Any] | None = None,
        require_owned_order: bool = False,
    ) -> bool:
        """Apply close fills before treating a missing position as an unknown close."""
        if self._futures_client is None:
            return False
        cutoff = self._as_utc(trade.entry_filled_at or trade.entry_submitted_at or trade.opened_at)
        owned_orders = self._owned_close_order_metadata(
            trade=trade,
            history_orders=history_orders or [],
        )
        eligible = []
        for fill in self._unprocessed_exchange_fills(trade, symbol_user_trades):
            if not self._is_close_fill_for_trade(trade, fill):
                continue
            order_id = str(getattr(fill, "order_id", "") or "")
            if require_owned_order and order_id not in owned_orders:
                continue
            fill_time = self._exchange_fill_datetime(fill)
            if fill_time is not None and fill_time < cutoff - timedelta(seconds=5):
                continue
            eligible.append(fill)
        if not eligible:
            return False

        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(f"Contract spec unavailable for {trade.symbol}")
        eligible.sort(
            key=lambda fill: (
                int(getattr(fill, "time", 0) or 0),
                str(getattr(fill, "order_id", "") or ""),
            )
        )
        grouped: dict[str, list[Any]] = {}
        for fill in eligible:
            order_id = str(getattr(fill, "order_id", "") or "")
            group_key = order_id or self._exchange_fill_key(fill)
            grouped.setdefault(group_key, []).append(fill)

        reconciled = False
        tick_size = self._coerce_decimal(getattr(spec, "tick_size", Decimal("0")))
        for group_key, fills in grouped.items():
            if not trade.is_open or trade.remaining_quantity <= 0:
                break
            contract_quantity = sum(
                (self._coerce_decimal(getattr(fill, "qty", Decimal("0"))) for fill in fills),
                Decimal("0"),
            )
            if contract_quantity <= 0:
                continue
            asset_quantity = _from_exchange_contract_quantity(contract_quantity, spec)
            if asset_quantity <= 0:
                continue
            (
                applied_quantity,
                quantity_already_applied,
                applied_ratio,
            ) = self._consume_pending_fill_audit(
                trade=trade,
                executed_quantity=asset_quantity,
            )
            weighted_notional = sum(
                (
                    self._coerce_decimal(getattr(fill, "price", Decimal("0")))
                    * self._coerce_decimal(getattr(fill, "qty", Decimal("0")))
                    for fill in fills
                ),
                Decimal("0"),
            )
            average_price = weighted_notional / contract_quantity
            realized_pnl = (
                sum(
                    (
                        self._coerce_decimal(getattr(fill, "realized_pnl", Decimal("0")))
                        for fill in fills
                    ),
                    Decimal("0"),
                )
                * applied_ratio
            )
            fees = (
                sum(
                    (
                        self._coerce_decimal(getattr(fill, "commission", Decimal("0")))
                        for fill in fills
                    ),
                    Decimal("0"),
                )
                * applied_ratio
            )
            order_id = str(getattr(fills[0], "order_id", "") or group_key)
            order_type = str(getattr(fills[0], "order_type", "") or "")
            owned_kind, owned_target_index = owned_orders.get(order_id, ("unknown", None))
            target_index = owned_target_index
            if owned_kind == "take_profit" and target_index is None:
                target_index = self._matching_take_profit_index(
                    trade=trade,
                    order_id=order_id,
                    order_type=order_type,
                    average_price=average_price,
                    tick_size=tick_size,
                )
            elif owned_kind == "unknown":
                target_index = self._matching_take_profit_index(
                    trade=trade,
                    order_id=order_id,
                    order_type=order_type,
                    average_price=average_price,
                    tick_size=tick_size,
                )
            is_take_profit = target_index is not None
            is_stop_loss = owned_kind == "stop_loss" or "LOSS" in order_type.upper()
            reason = (
                f"tp{target_index + 1}_hit"
                if target_index is not None
                else ("sl_hit" if is_stop_loss else "exchange_close_reconciled")
            )
            attribution = MessageAttribution(
                message_id=0,
                channel_id=trade.channel_id,
                channel_label=trade.channel_label,
                message_preview=(
                    "exchange take-profit fill recovered from userTrades"
                    if is_take_profit
                    else "exchange stop/market-close fill recovered from userTrades"
                ),
                message_date=self._exchange_fill_datetime(fills[0]) or _utc_now(),
                action="partial_close" if is_take_profit else "closed",
                notes=[
                    "exchange_missing_position_fill_reconciled "
                    f"order={order_id} type={order_type or 'unknown'}"
                ],
            )
            self._apply_exchange_close_result(
                trade=trade,
                requested_quantity=applied_quantity,
                executed_quantity=applied_quantity,
                avg_price=average_price,
                realized_pnl=realized_pnl,
                fees=fees,
                reason=reason,
                order_id=order_id,
                message=attribution,
                quantity_already_applied=quantity_already_applied,
            )
            self._record_processed_exchange_fills(trade, fills)
            reconciled = True
            if target_index is not None:
                self._apply_strategy_after_exchange_target_fill(
                    trade=trade,
                    target_index=target_index,
                    attribution=attribution,
                )

        if not reconciled:
            return False
        if trade.pending_fill_audit_quantity <= Decimal("0"):
            self._clear_trade_health_issue(
                trade,
                prefix=f"exchange quantity drift for {trade.trade_id} requires fill audit:",
            )
        self._clear_trade_health_issue(
            trade,
            prefix=f"exchange reconciliation required for {trade.trade_id}:",
        )
        self._log_event(
            logging.INFO,
            "live_trading.exchange_missing_position_fills_reconciled",
            close_reason=trade.close_reason,
            realized_pnl=str(trade.realized_pnl),
            fees=str(trade.fees),
            **self._trade_log_fields(trade),
        )
        if trade.is_open:
            self.store.save_trade(trade)
            return True

        trade.sl_order_id = None
        trade.tp_order_ids = []
        trade.tp_order_plan = []
        trade.required_tp_order_count = 0
        self._finalize_closed_trade(trade)
        context = self._contexts.get(trade.channel_id)
        if context is not None:
            self._mark_signal_terminal(
                context=context,
                signal_id=trade.signal_id,
                status=SignalStatus.CLOSED,
                trade=trade,
            )
        return True

    async def _sync_exchange_state(self) -> None:
        async with self._account_coordinator.execution_guard():
            await self._sync_exchange_state_unlocked()

    async def _sync_exchange_state_unlocked(self) -> None:
        if self._futures_client is None:
            return
        use_demo_symbol = self._use_demo_exchange_symbol()
        positions = await self._futures_client.get_open_positions(use_demo_symbol=use_demo_symbol)
        order_symbols = {trade.symbol for trade in self._open_trades.values() if trade.symbol}
        recent_orders: list[LiveExchangeOrderSnapshot] = []
        by_symbol_orders: dict[str, list[LiveExchangeOrderSnapshot]] = {}
        by_symbol_history_orders: dict[str, list[Any]] = {}
        by_symbol_open_regular_orders: dict[str, list[Any]] = {}
        by_symbol_open_protection_orders: dict[str, list[Any]] = {}
        by_symbol_user_trades: dict[str, list[Any]] = {}
        by_symbol_specs: dict[str, Any] = {}
        for symbol in sorted(order_symbols):
            try:
                history = await self._futures_client.get_order_history(
                    symbol,
                    limit=100,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception as exc:
                history = []
                self._log_event(
                    logging.WARNING,
                    "live_trading.exchange_order_history_query_failed",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            snapshots = [self._order_snapshot(item) for item in history]
            by_symbol_history_orders[symbol] = history
            by_symbol_orders[symbol] = snapshots
            recent_orders.extend(snapshots[:5])
            try:
                regular_open_orders = await self._futures_client.get_open_orders(
                    symbol,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception:
                regular_open_orders = []
            by_symbol_open_regular_orders[symbol] = regular_open_orders
            recent_orders.extend(self._order_snapshot(item) for item in regular_open_orders[:5])
            try:
                protection_orders = await self._futures_client.get_open_orders(
                    symbol,
                    order_type="STOP_PROFIT_LOSS",
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception:
                protection_orders = []
            try:
                protection_orders.extend(
                    await self._futures_client.get_open_algo_orders(
                        symbol,
                        use_demo_symbol=use_demo_symbol,
                    )
                )
            except Exception:
                pass
            by_symbol_open_protection_orders[symbol] = protection_orders
            recent_orders.extend(self._order_snapshot(item) for item in protection_orders[:5])
            try:
                symbol_user_trades = await self._futures_client.get_user_trades(
                    symbol,
                    limit=100,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception:
                symbol_user_trades = []
            by_symbol_user_trades[symbol] = symbol_user_trades
            try:
                spec = await self._futures_client.get_contract_spec(symbol)
            except Exception:
                spec = None
            if spec is not None and isinstance(
                getattr(spec, "contract_multiplier", None),
                (Decimal, int, str),
            ):
                by_symbol_specs[symbol] = spec

        position_snapshots = [
            self._position_snapshot(
                item,
                spec=by_symbol_specs.get(
                    canonical_market_symbol(
                        getattr(item, "symbol_internal", None) or getattr(item, "symbol", None)
                    )
                    or ""
                ),
            )
            for item in positions
        ]
        self.session.exchange_snapshot = LiveExchangeSnapshot(
            positions=position_snapshots,
            recent_orders=recent_orders[:40],
            error=None,
        )

        for trade in list(self._open_trades.values()):
            if trade.entry_order_plan:
                spec = by_symbol_specs.get(trade.symbol)
                if spec is None:
                    trade.last_exchange_sync_error = (
                        f"Contract spec unavailable while reconciling {trade.symbol} range entry"
                    )
                    self._persist_trade_runtime_state(trade)
                    continue
                range_entry_expired = (
                    trade.entry_order_expires_at is not None
                    and _utc_now() >= trade.entry_order_expires_at
                )
                if range_entry_expired:
                    await self._cancel_range_entry_orders(
                        trade=trade,
                        reason="range_entry_ladder_expired",
                        reconcile_fills=True,
                        spec=spec,
                        ensure_protection=True,
                    )
                    if trade.entry_filled_quantity <= Decimal("0"):
                        await self._finalize_waiting_entry_without_fill(
                            trade=trade,
                            reason="entry_order_expired",
                            signal_status=SignalStatus.EXPIRED,
                        )
                        continue
                    trade.entry_order_expires_at = None
                else:
                    await self._reconcile_range_entry_orders(
                        trade=trade,
                        history_orders=by_symbol_history_orders.get(trade.symbol, []),
                        open_orders=by_symbol_open_regular_orders.get(trade.symbol, []),
                        symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                        spec=spec,
                    )
            if not trade.is_waiting_entry:
                await self._reconcile_missing_exchange_position_fills(
                    trade=trade,
                    symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                    history_orders=by_symbol_history_orders.get(trade.symbol, []),
                    require_owned_order=True,
                )
                if trade.signal_id not in self._open_trades or not trade.is_open:
                    continue
            trade.exchange_position = self._allocate_exchange_position_to_trade(
                trade,
                self._find_matching_exchange_position(
                    trade=trade,
                    positions=position_snapshots,
                ),
            )
            context = self._contexts.get(trade.channel_id)
            if trade.is_waiting_entry:
                if trade.entry_order_plan:
                    continue
                activated = await self._reconcile_pending_entry(
                    trade=trade,
                    history_orders=by_symbol_history_orders.get(trade.symbol, []),
                    open_orders=by_symbol_open_regular_orders.get(trade.symbol, []),
                    symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                )
                if not activated:
                    continue
                trade.exchange_position = self._allocate_exchange_position_to_trade(
                    trade,
                    self._find_matching_exchange_position(
                        trade=trade,
                        positions=position_snapshots,
                    ),
                )
            if trade.exchange_position is not None and not trade.is_waiting_entry:
                self._reconcile_trade_quantity_to_exchange(
                    trade=trade,
                    exchange_quantity=trade.exchange_position.quantity,
                )
            if trade.exchange_position is None:
                # Filled orders can become visible before the aggregate
                # positions endpoint converges. Reconcile confirmed fills
                # immediately, but require repeated missing snapshots across
                # the configured grace window before abandoning ownership.
                await self._reconcile_exchange_trade_protection(
                    trade=trade,
                    open_regular_orders=by_symbol_open_regular_orders.get(
                        trade.symbol,
                        [],
                    ),
                    open_protection_orders=by_symbol_open_protection_orders.get(
                        trade.symbol,
                        [],
                    ),
                    symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                    allow_repair=False,
                )
                if trade.signal_id not in self._open_trades:
                    continue
                await self._reconcile_missing_exchange_position_fills(
                    trade=trade,
                    symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                    history_orders=by_symbol_history_orders.get(trade.symbol, []),
                )
                if trade.signal_id not in self._open_trades:
                    continue
                if not self._should_confirm_exchange_position_missing(
                    trade,
                    reason="exchange_position_missing_snapshot",
                ):
                    trade.last_exchange_sync_at = _utc_now()
                    self.store.save_trade(trade)
                    if context is not None:
                        state = context.get_signal(trade.signal_id)
                        if state is not None:
                            self._sync_signal_snapshot(
                                context=context,
                                state=state,
                                trade=trade,
                            )
                    self._emit_trade_update(trade)
                    continue
                await self._resolve_confirmed_missing_exchange_position(
                    context=context,
                    trade=trade,
                    reason="exchange_position_missing_snapshot",
                    symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
                    history_orders=by_symbol_history_orders.get(trade.symbol, []),
                )
                continue
            self._clear_exchange_position_miss_state(trade)
            await self._reconcile_exchange_trade_protection(
                trade=trade,
                open_regular_orders=by_symbol_open_regular_orders.get(trade.symbol, []),
                open_protection_orders=by_symbol_open_protection_orders.get(trade.symbol, []),
                symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
            )
            if trade.signal_id not in self._open_trades:
                continue
            trade.exchange_order_history = list(by_symbol_orders.get(trade.symbol, []))
            trade.exchange_order_history.extend(
                self._order_snapshot(item)
                for item in by_symbol_open_regular_orders.get(trade.symbol, [])
            )
            trade.exchange_order_history.extend(
                self._order_snapshot(item)
                for item in by_symbol_open_protection_orders.get(trade.symbol, [])
            )
            if trade.exchange_position is not None:
                trade.mark_price = trade.exchange_position.mark_price
                trade.unrealized_pnl = trade.exchange_position.unrealized_pnl
            trade.last_exchange_sync_at = _utc_now()
            trade.last_exchange_sync_error = None
            if context is not None:
                state = context.get_signal(trade.signal_id)
                if state is not None:
                    self._sync_signal_snapshot(context=context, state=state, trade=trade)
            self.store.save_trade(trade)
        self.session.total_unrealized_pnl = sum(
            (
                trade.exchange_position.unrealized_pnl
                if trade.exchange_position is not None
                else trade.unrealized_pnl
                for trade in self._open_trades.values()
            ),
            Decimal("0"),
        )

    def _sync_signal_snapshot(
        self,
        *,
        context: ChannelContext,
        state: SignalState,
        trade: LiveTrade | None,
    ) -> None:
        parsed = state.current_signal
        channel_label = self._channel_label(state.channel_id)
        entry_zone = None
        last_message = (
            context.get_message(state.related_message_ids[-1])
            if state.related_message_ids
            else None
        )
        if parsed and (parsed.entry_low is not None or parsed.entry_high is not None):
            entry_zone = {
                "low": str(parsed.entry_low if parsed.entry_low is not None else ""),
                "high": str(parsed.entry_high if parsed.entry_high is not None else ""),
            }
        notes: list[str] = []
        if trade and trade.message_history:
            notes.extend(trade.message_history[-1].notes)
        if trade and trade.last_exchange_sync_error:
            notes.append(f"exchange_error={trade.last_exchange_sync_error}")
        snapshot = LiveSignalSnapshot(
            signal_id=state.signal_id,
            channel_id=state.channel_id,
            channel_label=channel_label,
            created_from_message_id=state.created_from_message_id,
            related_message_ids=list(state.related_message_ids),
            status=state.status.value,
            status_group="active"
            if state.status
            in {
                SignalStatus.PENDING_CONSOLIDATION,
                SignalStatus.ORDER_PLANNED,
                SignalStatus.ORDER_SUBMITTED,
                SignalStatus.OPEN,
            }
            else "inactive",
            symbol=parsed.symbol if parsed else None,
            exchange_symbol=trade.exchange_symbol if trade is not None else None,
            side=parsed.side.value if parsed else None,
            entry_type=(
                trade.entry_type
                if trade is not None
                else (parsed.entry_type.value if parsed else None)
            ),
            entry_low=parsed.entry_low if parsed else None,
            entry_high=parsed.entry_high if parsed else None,
            entry_zone=entry_zone,
            stop_loss=(
                trade.stop_loss if trade is not None else (parsed.stop_loss if parsed else None)
            ),
            take_profits=(
                list(trade.take_profits)
                if trade is not None
                else (list(parsed.take_profits) if parsed else [])
            ),
            leverage=trade.leverage if trade is not None else (parsed.leverage if parsed else None),
            trade_id=trade.trade_id if trade is not None else None,
            trade_status=trade.status if trade is not None else None,
            targets_hit=trade.targets_hit if trade is not None else 0,
            opened_at=trade.opened_at if trade is not None else None,
            updated_at=state.updated_at,
            closed_at=trade.closed_at if trade is not None else None,
            close_reason=trade.close_reason if trade is not None else None,
            last_message_id=state.related_message_ids[-1] if state.related_message_ids else None,
            last_message_date=last_message.date if last_message is not None else None,
            message_count=len(state.related_message_ids),
            notes=notes,
            exchange_position=trade.exchange_position if trade is not None else None,
            exchange_order_history=(
                list(trade.exchange_order_history) if trade is not None else []
            ),
        )
        self.store.save_signal_snapshot(self.session.session_id, snapshot)

    def _mark_signal_terminal(
        self,
        *,
        context: ChannelContext,
        signal_id: str,
        status: SignalStatus,
        trade: LiveTrade | None,
    ) -> None:
        state = context.get_signal(signal_id)
        if state is None:
            return
        state.status = status
        state.updated_at = _utc_now()
        self._sync_signal_snapshot(context=context, state=state, trade=trade)

    @staticmethod
    def _position_snapshot(
        position: Any,
        *,
        spec: Any | None = None,
    ) -> LiveExchangePositionSnapshot:
        contract_quantity = LiveTradingEngine._coerce_decimal(position.position)
        contract_available = LiveTradingEngine._coerce_decimal(position.available)
        quantity = (
            _from_exchange_contract_quantity(contract_quantity, spec)
            if spec is not None
            else contract_quantity
        )
        available = (
            _from_exchange_contract_quantity(contract_available, spec)
            if spec is not None
            else contract_available
        )
        return LiveExchangePositionSnapshot(
            symbol=position.symbol_internal,
            exchange_symbol=position.exchange_symbol,
            side=position.side,
            quantity=quantity,
            available=available,
            avg_price=position.avg_price,
            mark_price=position.mark_price,
            leverage=position.leverage,
            margin=position.margin,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
            raw_status=position.margin_type,
        )

    @staticmethod
    def _order_snapshot(order: Any) -> LiveExchangeOrderSnapshot:
        return LiveExchangeOrderSnapshot(
            order_id=order.order_id,
            client_order_id=order.client_order_id or None,
            symbol=from_futures_symbol(order.symbol),
            exchange_symbol=order.exchange_symbol,
            side=order.side,
            order_type=order.order_type,
            status=order.status,
            orig_qty=order.orig_qty,
            executed_qty=order.executed_qty,
            avg_price=order.avg_price,
            price=order.price,
            stop_price=order.stop_price,
            leverage=order.leverage,
            trigger_by=order.trigger_by or None,
            position_side=order.position_side or None,
        )

    def _use_demo_exchange_symbol(self) -> bool:
        return self.session.trading_mode == "demo"

    def _uses_exchange_execution(self) -> bool:
        return self._futures_client is not None and self.session.trading_mode in {"demo", "live"}


# ── Factory ────────────────────────────────────────────────────────────────


def build_engine_from_config(
    *,
    config: LiveSessionConfig,
    settings: Settings,
    store: LiveTradingStore,
    notifier: Callable[[dict[str, Any]], None] | None = None,
    telegram_client: TelegramClientInterface | None = None,
    account_coordinator: AccountExecutionCoordinator | None = None,
) -> tuple[LiveSession, LiveTradingEngine]:
    session_id = f"ls_{uuid.uuid4().hex[:12]}"
    labels = [f"@{ch.rsplit('/', 1)[-1]}" if "/" in ch else ch for ch in config.channels]
    session = LiveSession(
        session_id=session_id,
        channels=config.channels,
        channel_labels=labels,
        trading_mode=config.trading_mode,
        initial_balance=Decimal("0"),
        risk_per_trade_pct=config.risk_per_trade_pct,
        strategy_key=config.strategy_key,
        use_ai=config.use_ai,
        interval=config.interval,
        label=config.label or build_live_session_label(config.channels[0], config.trading_mode),
    )
    engine = LiveTradingEngine(
        settings=settings,
        session=session,
        store=store,
        notifier=notifier,
        telegram_client=telegram_client,
        account_coordinator=account_coordinator,
    )
    return session, engine


def build_engine_from_session(
    *,
    session: LiveSession,
    settings: Settings,
    store: LiveTradingStore,
    notifier: Callable[[dict[str, Any]], None] | None = None,
    telegram_client: TelegramClientInterface | None = None,
    account_coordinator: AccountExecutionCoordinator | None = None,
) -> tuple[LiveSession, LiveTradingEngine]:
    engine = LiveTradingEngine(
        settings=settings,
        session=session,
        store=store,
        notifier=notifier,
        telegram_client=telegram_client,
        account_coordinator=account_coordinator,
    )
    return session, engine
