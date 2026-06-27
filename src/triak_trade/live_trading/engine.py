"""Live / demo trading engine — real-time Telegram signal processing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
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
    detect_tp_list_update,
    extract_close_fraction,
    normalize_related_signal_action,
)
from triak_trade.backtesting.strategies.registry import load_strategy
from triak_trade.config.settings import Settings
from triak_trade.core.symbols import canonical_market_symbol
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, SignalStatus, TradeSide
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.futures import (
    ToobitFuturesClient,
    _from_exchange_contract_quantity,
    _to_exchange_contract_quantity,
    build_futures_client_from_settings,
    from_futures_symbol,
    to_exchange_futures_symbol,
)
from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveExchangeOrderSnapshot,
    LiveExchangePositionSnapshot,
    LiveExchangeSnapshot,
    LiveMessageTrace,
    LiveSession,
    LiveSessionConfig,
    LiveSignalSnapshot,
    LiveTrade,
    LiveTradingSnapshot,
    MessageAttribution,
    _utc_now,
)
from triak_trade.live_trading.position_manager import LivePositionManager
from triak_trade.live_trading.store import LiveTradingStore
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.telethon_client import TelethonTelegramClient

log = logging.getLogger(__name__)


@dataclass
class _ExchangeCloseResult:
    order_id: str
    status: str
    avg_price: Decimal
    executed_quantity: Decimal
    realized_pnl: Decimal
    fees: Decimal


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
    ) -> None:
        self.settings = settings
        self.session = session
        self.store = store
        self.notifier = notifier

        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tg_task: asyncio.Task[None] | None = None

        # Per-channel signal state
        self._contexts: dict[str, ChannelContext] = {}

        # signal_id → open trade (in-memory, backed by store)
        self._open_trades: dict[str, LiveTrade] = {}

        # Recent message traces for dashboard feed (up to 200)
        self._message_traces: list[LiveMessageTrace] = []

        # Components built in _setup_components()
        self._classifier: MessageClassifier | None = None
        self._telegram_client: TelethonTelegramClient | None = None
        self._futures_client: ToobitFuturesClient | None = None
        self._pm: LivePositionManager | None = None
        self._strategy: Any = None
        self._validator = ParsedSignalValidator()

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._setup_components()
        self._restore_runtime_state()

        # Fetch initial account info immediately
        await self._refresh_account()

        self.session.mark_running()
        self.store.save_session(self.session)
        self._emit_session_update()
        log.info(
            "LiveTradingEngine started session=%s channels=%s mode=%s",
            self.session.session_id,
            self.session.channels,
            self.session.trading_mode,
        )

        bg_tasks = [
            asyncio.create_task(self._price_refresh_loop(), name="price_refresh"),
            asyncio.create_task(self._account_refresh_loop(), name="account_refresh"),
            asyncio.create_task(self._consolidation_tick_loop(), name="consolidation"),
        ]

        try:
            assert self._telegram_client is not None
            self._tg_task = asyncio.create_task(
                self._telegram_client.listen_new_messages(
                    self.session.channels,
                    self._handle_message,
                ),
                name="tg_listener",
            )
            await self._tg_task
        except asyncio.CancelledError:
            log.info("LiveTradingEngine: Telegram listener cancelled (stop requested)")
        except Exception as exc:
            error = f"Telegram listener failed: {type(exc).__name__}: {exc}"
            log.error("LiveTradingEngine: %s", error, exc_info=True)
            self.session.mark_stopped(error=error)
            self.store.save_session(self.session)
            self._emit_session_update()
        finally:
            self._running = False
            self._tg_task = None
            for t in bg_tasks:
                t.cancel()
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        if self.session.status == "running":
            self.session.mark_stopped()
            self.store.save_session(self.session)
            self._emit_session_update()

    def stop(self) -> None:
        """Thread-safe stop — cancels the Telegram listener task."""
        self._running = False
        loop = self._loop
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._async_stop(), loop)

    async def _async_stop(self) -> None:
        # First disconnect the Telegram client (this will unblock run_until_disconnected)
        if self._telegram_client is not None:
            try:
                await self._telegram_client.stop()
            except Exception:
                pass
        # Then cancel the task if still running
        if self._tg_task and not self._tg_task.done():
            self._tg_task.cancel()
        self.session.mark_stopped()
        self.store.save_session(self.session)
        self._emit_session_update()

    def get_snapshot(self) -> LiveTradingSnapshot:
        open_trades = list(self._open_trades.values())
        closed = self.store.list_closed_trades(self.session.session_id, limit=30)
        return LiveTradingSnapshot(
            session=self.session,
            open_trades=open_trades,
            recent_closed_trades=closed,
            account_info=self.session.account_info,
        )

    def get_recent_messages(self, limit: int = 50) -> list[LiveMessageTrace]:
        return list(reversed(self._message_traces[-limit:]))

    # ── Component Setup ────────────────────────────────────────────────────

    def _setup_components(self) -> None:
        s = self.settings
        self._pm = LivePositionManager(s)
        self._strategy = load_strategy(self.session.strategy_key)

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
                )
                log.info("LiveTradingEngine: using AI classifier")
            except Exception as exc:
                if self._must_fail_closed_without_ai():
                    raise RuntimeError(
                        "AI classifier init failed for exchange execution session"
                    ) from exc
                log.warning("AI classifier init failed, falling back to regex", exc_info=True)
                self._classifier = RegexMessageClassifier()
        else:
            if self._must_fail_closed_without_ai():
                raise RuntimeError("AI classifier is required for exchange execution session")
            self._classifier = RegexMessageClassifier()
            log.info("LiveTradingEngine: using regex classifier")

        self._telegram_client = TelethonTelegramClient(settings=s)

        # Build futures client for both demo (mark prices) and live (orders)
        try:
            self._futures_client = build_futures_client_from_settings(s)
        except Exception:
            log.warning("Failed to build futures client", exc_info=True)

    def _restore_runtime_state(self) -> None:
        self._open_trades = {
            trade.signal_id: trade
            for trade in self.store.list_open_trades(self.session.session_id)
        }
        self.session.open_positions_count = len(self._open_trades)
        self._message_traces = list(
            reversed(self.store.list_message_traces(self.session.session_id, limit=200))
        )

        traces = list(
            reversed(self.store.list_message_traces(self.session.session_id, limit=5000))
        )
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
            context.add_signal(
                self._signal_state_from_snapshot(snapshot),
                pending=snapshot.status == SignalStatus.PENDING_CONSOLIDATION.value,
            )

    def _signal_state_from_snapshot(self, snapshot: LiveSignalSnapshot) -> SignalState:
        parsed_signal: ParsedSignal | None = None
        if snapshot.symbol is not None or snapshot.stop_loss is not None or snapshot.take_profits:
            side = TradeSide.UNKNOWN
            if snapshot.side in {"long", "buy"}:
                side = TradeSide.LONG
            elif snapshot.side in {"short", "sell"}:
                side = TradeSide.SHORT
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
        return SignalState(
            signal_id=snapshot.signal_id,
            channel_id=snapshot.channel_id,
            status=SignalStatus(snapshot.status),
            created_from_message_id=snapshot.created_from_message_id,
            related_message_ids=(
                list(snapshot.related_message_ids)
                or [snapshot.created_from_message_id]
            ),
            current_signal=parsed_signal,
            version=max(1, snapshot.message_count or 1),
            created_at=snapshot.opened_at or snapshot.updated_at,
            updated_at=snapshot.updated_at,
            expires_at=None,
        )

    def _must_fail_closed_without_ai(self) -> bool:
        return (
            self.session.trading_mode == "live"
            and bool(getattr(self.settings, "LIVE_TRADING_REQUIRE_AI_CLASSIFIER", False))
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
            await self._process_message(message)
        except Exception:
            log.error(
                "Error processing message %s from %s",
                message.message_id,
                message.channel_id,
                exc_info=True,
            )

    async def _process_message(self, message: RawTelegramMessage) -> None:
        self.session.total_messages_processed += 1
        channel_label = self._channel_label(message.channel_id)

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
        context.add_recent_message(message)

        # Download media if needed
        if (
            bool(message.raw_payload.get("has_media"))
            and bool(message.raw_payload.get("caption_present"))
            and self._telegram_client is not None
        ):
            try:
                message = await self._telegram_client.ensure_media_payload(message)
            except Exception:
                pass

        # Classify
        assert self._classifier is not None
        classified = self._classifier.classify(message, context)
        parsed = classified.parsed_signal

        # ── Text-directive upgrades (matches backtest real_runner logic) ──────
        # 1. close_all overrides ANY AI action, even IGNORE/UNKNOWN
        close_all_detected = detect_close_all_instruction(message.text)
        if close_all_detected and parsed.action is not SignalAction.CLOSE:
            parsed = parsed.model_copy(update={"action": SignalAction.CLOSE})

        # 2. For UNKNOWN/IGNORE: try text-based promotion before giving up
        if parsed.action in (SignalAction.IGNORE, SignalAction.UNKNOWN):
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
        if related_signal_id is not None and self._uses_exchange_execution():
            related_trade = self._open_trades.get(related_signal_id)
            if related_trade is not None:
                trade_is_live = await self._ensure_trade_still_open_on_exchange(
                    context=context,
                    trade=related_trade,
                    reason="followup_exchange_position_missing",
                )
                if not trade_is_live:
                    trace.debug_notes.append(
                        "related_signal_detached_exchange_position_missing="
                        f"{related_trade.trade_id}"
                    )
                    if parsed.action is SignalAction.OPEN:
                        related_signal_id = None
                        trace.correlation_method = "new_signal"
                        trace.correlation_note = "stale_related_trade_detached"
                    else:
                        related_signal_id = None
                        trace.correlation_method = "exchange_position_missing"
                        trace.correlation_note = "stale_related_trade_detached"
        if parsed.action is SignalAction.OPEN and related_signal_id is None:
            trace.correlation_method = "new_signal"
            trace.correlation_note = None
            trace.debug_notes = [
                note for note in trace.debug_notes if note != "no_signal_for_followup"
            ]

        # ── Route by effective action ─────────────────────────────────────────
        if parsed.action in (SignalAction.IGNORE, SignalAction.UNKNOWN):
            # Still ambiguous after all upgrades → drop
            trace.final_status = "ignored"
            trace.effect_summary = "Message ignored by classifier"
            self._push_trace(trace)
            self.store.save_session(self.session)
            self._emit_session_update()
            return

        # close_all closes EVERY open trade regardless of correlation (matches backtest simulator)
        if close_all_detected and parsed.action is SignalAction.CLOSE:
            await self._handle_close_all_trades(message, trace, channel_label)
            self._push_trace(trace)
            self.store.save_session(self.session)
            self._emit_session_update()
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
            log.info("Signal queued: %s %s %s", signal_id, parsed.symbol, parsed.side.value)

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

        self._push_trace(trace)
        self.store.save_session(self.session)
        self._emit_session_update()

    # ── Close-All Handler ─────────────────────────────────────────────────

    async def _handle_close_all_trades(
        self,
        message: RawTelegramMessage,
        trace: LiveMessageTrace,
        channel_label: str,
    ) -> None:
        """Close every open trade — matches BacktestSimulator close_all behavior."""
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
                    log.warning("Close-all exchange close failed for %s: %s", trade.trade_id, exc)
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
            return
        if self._uses_exchange_execution():
            trade_is_live = await self._ensure_trade_still_open_on_exchange(
                context=context,
                trade=trade,
                reason="followup_exchange_position_missing",
            )
            if not trade_is_live:
                trace.final_status = "no_open_trade"
                trace.effect_summary = (
                    f"Signal {signal_id} has no live exchange position to update"
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

        assert self._pm is not None
        fee_rate = self.settings.LIVE_TRADING_FEE_RATE_PCT

        if effective_action is SignalAction.CLOSE or close_all:
            fraction = close_fraction_raw or Decimal("1")
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
            previous_stop_loss = trade.stop_loss
            self._pm.update_stop_loss(
                trade=trade,
                new_sl=parsed.stop_loss,
                message=attribution,
                move_to_entry=move_to_entry,
            )
            if self._uses_exchange_execution():
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
                    if previous_stop_loss is not None:
                        try:
                            await self._sync_trade_protection(
                                trade,
                                refresh_take_profits=False,
                                refresh_stop_loss=True,
                            )
                        except Exception as restore_exc:
                            attribution.notes.append(
                                f"exchange_previous_sl_restore_failed={restore_exc}"
                            )
                        else:
                            attribution.notes.append("exchange_previous_sl_restored")
                    trace.final_status = "update_sl_failed"
                    trace.effect_summary = f"SL update failed on exchange: {exc}"
            new_sl = trade.entry_price if move_to_entry else parsed.stop_loss
            if trace.final_status != "update_sl_failed":
                trace.final_status = "updated_sl"
                trace.effect_summary = (
                    "SL moved to entry (breakeven)" if move_to_entry
                    else f"SL updated to {new_sl}"
                )

        elif effective_action is SignalAction.UPDATE_TP:
            # Try AI-parsed TPs first, then extract from text
            new_tps = parsed.take_profits or detect_tp_list_update(message.text)
            if new_tps:
                self._pm.update_take_profits(trade=trade, new_tps=new_tps, message=attribution)
                if self._uses_exchange_execution():
                    try:
                        await self._sync_trade_protection(trade)
                    except Exception as exc:
                        trade.last_exchange_sync_error = str(exc)
                        attribution.notes.append(f"exchange_trading_stop_failed={exc}")
                trace.final_status = "updated_tp"
                trace.effect_summary = f"TPs updated: {[str(t) for t in new_tps]}"
            else:
                trace.final_status = "no_tp_found"
                trace.effect_summary = "UPDATE_TP but no TP values extracted"
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
                    "TPs="
                    + ",".join(str(item) for item in trade.take_profits[trade.targets_hit :])
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
            if protection_needs_sync and self._uses_exchange_execution():
                try:
                    await self._sync_trade_protection(trade)
                except Exception as exc:
                    trade.last_exchange_sync_error = str(exc)
                    attribution.notes.append(f"exchange_trading_stop_failed={exc}")
            if not updates_applied:
                updates_applied.append("entry replay ignored for already-open trade")
            trace.final_status = "updated_entry"
            trace.effect_summary = " / ".join(updates_applied)
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
        parsed = self._normalize_missing_entry_to_market(parsed)
        normalized_symbol = canonical_market_symbol(parsed.symbol)
        if normalized_symbol is not None and normalized_symbol != parsed.symbol:
            parsed = parsed.model_copy(update={"symbol": normalized_symbol})
            state.current_signal = parsed

        if not parsed.symbol:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
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
                return

        # Get current mark price for MARKET entries
        mark_price = await self._get_mark_price(parsed.symbol)
        from triak_trade.domain.enums import EntryType
        if mark_price and (
            parsed.entry_type is EntryType.MARKET
            or (parsed.entry_low is None and parsed.entry_high is None)
        ):
            parsed = parsed.model_copy(
                update={"entry_low": mark_price, "entry_high": mark_price}
            )
        parsed, geometry_error = self._normalize_or_reject_open_geometry(parsed)
        if geometry_error is not None:
            log.debug("Signal %s rejected by open geometry checks: %s", signal_id, geometry_error)
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
            log.debug("Signal %s failed validation: %s", signal_id, errors)
            state.current_signal = parsed
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(signal_id=signal_id, status="invalid", note=str(errors))
            return

        await self._open_position(signal_id=signal_id, state=state, parsed=parsed, context=context)

    @staticmethod
    def _normalize_missing_entry_to_market(parsed: ParsedSignal) -> ParsedSignal:
        if (
            parsed.action is SignalAction.OPEN
            and parsed.entry_type is not EntryType.MARKET
            and parsed.entry_low is None
            and parsed.entry_high is None
        ):
            return parsed.model_copy(update={"entry_type": EntryType.MARKET})
        return parsed

    @staticmethod
    def _entry_reference(parsed: ParsedSignal) -> Decimal | None:
        if parsed.entry_low is not None and parsed.entry_high is not None:
            return (parsed.entry_low + parsed.entry_high) / Decimal("2")
        return parsed.entry_low or parsed.entry_high

    def _normalize_or_reject_open_geometry(
        self,
        parsed: ParsedSignal,
    ) -> tuple[ParsedSignal, str | None]:
        if parsed.action is not SignalAction.OPEN:
            return parsed, None
        reference = self._entry_reference(parsed)
        if reference is None or reference <= 0:
            return parsed, None

        score_long = 0
        score_short = 0
        if parsed.stop_loss is not None:
            if parsed.stop_loss < reference:
                score_long += 1
            elif parsed.stop_loss > reference:
                score_short += 1
        for tp in parsed.take_profits:
            if tp > reference:
                score_long += 1
            elif tp < reference:
                score_short += 1

        updates: dict[str, object] = {}
        if parsed.side in {TradeSide.BUY, TradeSide.SELL} and score_long != score_short:
            updates["side"] = TradeSide.LONG if score_long > score_short else TradeSide.SHORT
        normalized = parsed.model_copy(update=updates) if updates else parsed

        if normalized.stop_loss is None:
            return normalized, None
        if normalized.side.is_long and normalized.stop_loss >= reference:
            return (
                normalized,
                "inconsistent long geometry: stop_loss is not below entry/market price",
            )
        if normalized.side.is_short and normalized.stop_loss <= reference:
            return (
                normalized,
                "inconsistent short geometry: stop_loss is not above entry/market price",
            )
        return normalized, None

    async def _open_position(
        self,
        *,
        signal_id: str,
        state: SignalState,
        parsed: ParsedSignal,
        context: ChannelContext,
    ) -> None:
        assert self._pm is not None and self._strategy is not None

        if self.settings.KILL_SWITCH_ENABLED:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(
                signal_id=signal_id,
                status="kill_switch_blocked",
                note=self.settings.KILL_SWITCH_REASON or "Kill Switch is active",
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
            log.warning("Cannot size position %s: %s", signal_id, exc)
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._sync_signal_snapshot(context=context, state=state, trade=None)
            self._emit_trace_update(signal_id=signal_id, status="invalid_sizing", note=str(exc))
            return

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
                await self._real_open_position(trade)
            except Exception as exc:
                log.error("Real order failed for trade %s: %s", trade.trade_id, exc)
                trade.status = "closed"
                trade.close_reason = f"order_failed: {exc}"
                trade.closed_at = _utc_now()
                trade.last_exchange_sync_error = str(exc)
                state.status = SignalStatus.INVALID
                context.add_signal(state, pending=False)
                self._sync_signal_snapshot(context=context, state=state, trade=trade)
                self.store.save_trade(trade)
                self._emit_trace_update(
                    signal_id=signal_id, status="order_failed", note=str(exc)
                )
                return

        # Register trade
        self._open_trades[signal_id] = trade
        state.status = SignalStatus.OPEN
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
            status="opened_trade",
            note=(
                f"Opened {trade.side.upper()} {trade.symbol} "
                f"qty={trade.quantity} @ {trade.entry_price} lev={trade.leverage}x"
            ),
            trade_id=trade.trade_id,
        )
        log.info(
            "Opened trade %s %s %s @ %s lev=%sx",
            trade.trade_id, trade.symbol, trade.side,
            trade.entry_price, trade.leverage,
        )

    # ── Price Refresh ──────────────────────────────────────────────────────

    async def _price_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.LIVE_TRADING_PRICE_REFRESH_SECONDS)
            try:
                await self._refresh_all_prices()
            except Exception:
                log.debug("Price refresh error", exc_info=True)

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

        to_remove: list[str] = []
        for signal_id, trade in list(self._open_trades.items()):
            mark = price_map.get(trade.symbol)
            if mark is None:
                continue
            self._pm.apply_mark_price(trade=trade, mark_price=mark, fee_rate_pct=fee_rate)
            if self._uses_exchange_execution() and self._trade_has_exchange_protection(trade):
                self.store.save_trade(trade)
                continue

            events = self._pm.check_sl_tp_hit(
                trade=trade, mark_price=mark,
                strategy=self._strategy, fee_rate_pct=fee_rate,
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
                            log.warning("Exchange SL close failed for %s: %s", trade.trade_id, exc)
                            break
                    else:
                        self._pm.close_trade(
                            trade=trade, close_price=mark,
                            reason="sl_hit", fee_rate_pct=fee_rate,
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

    # ── Account Refresh ────────────────────────────────────────────────────

    async def _account_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.LIVE_TRADING_ACCOUNT_REFRESH_SECONDS)
            try:
                await self._refresh_account()
            except Exception:
                log.debug("Account refresh error", exc_info=True)

    async def _refresh_account(self) -> None:
        if self._futures_client is None:
            return
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
        except Exception as exc:
            self.session.account_info = LiveAccountInfo(
                error=f"account fetch failed: {exc}"
            )
            self.session.exchange_snapshot = None
            self.store.save_session(self.session)
            return
        try:
            await self._sync_exchange_state()
        except Exception as exc:
            self.session.exchange_snapshot = self.session.exchange_snapshot or None
            if self.session.exchange_snapshot is not None:
                self.session.exchange_snapshot.error = f"exchange sync failed: {exc}"
        self.store.save_session(self.session)

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

    async def _real_open_position(self, trade: LiveTrade) -> None:
        assert self._futures_client is not None
        use_demo_symbol = self._use_demo_exchange_symbol()
        trade.exchange_symbol = to_exchange_futures_symbol(
            trade.symbol,
            use_demo_symbol=use_demo_symbol,
        )
        spec = await self._futures_client.get_contract_spec(trade.symbol)
        if spec is None:
            raise ValueError(f"Contract spec unavailable for {trade.symbol}")
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
            log.warning("set_leverage failed for %s: %s", trade.symbol, exc)
            trade.last_exchange_sync_error = str(exc)

        if trade.side == "long":
            order = await self._futures_client.open_long(
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
                use_demo_symbol=use_demo_symbol,
            )

        confirmed_order, fills = await self._futures_client.wait_for_order_fill(
            symbol=trade.symbol,
            order_id=order.order_id,
            use_demo_symbol=use_demo_symbol,
            timeout_seconds=float(self.settings.LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS),
        )
        if confirmed_order is None:
            raise ValueError(f"Toobit did not return open order {order.order_id} in history")
        if confirmed_order.status.upper() in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            raise ValueError(
                f"Toobit open order {confirmed_order.order_id} finished with status "
                f"{confirmed_order.status}"
            )

        trade.entry_order_id = confirmed_order.order_id
        trade.exchange_symbol = confirmed_order.exchange_symbol or trade.exchange_symbol
        executed_contract_qty = (
            sum((fill.qty for fill in fills), Decimal("0"))
            if fills
            else confirmed_order.executed_qty
        )
        executed_quantity = _from_exchange_contract_quantity(executed_contract_qty, spec)
        if executed_quantity > 0:
            trade.quantity = executed_quantity
            trade.remaining_quantity = executed_quantity
        if fills:
            weighted_notional = sum((fill.price * fill.qty for fill in fills), Decimal("0"))
            weighted_qty = sum((fill.qty for fill in fills), Decimal("0"))
            if weighted_qty > 0:
                trade.entry_price = weighted_notional / weighted_qty
        elif confirmed_order.avg_price and confirmed_order.avg_price > 0:
            trade.entry_price = confirmed_order.avg_price
        if trade.leverage > 0:
            trade.margin = (
                trade.entry_price * trade.quantity / Decimal(str(trade.leverage))
            ).quantize(Decimal("0.00000001"))
        trade.last_exchange_sync_error = None
        try:
            await self._refresh_account()
        except Exception:
            log.debug("Account refresh after open failed", exc_info=True)
        try:
            await self._sync_trade_protection(trade)
        except Exception as exc:
            trade.last_exchange_sync_error = str(exc)
            if self.settings.LIVE_TRADING_FAIL_CLOSED_ON_PROTECTION_SYNC_ERROR:
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
                    "Failed to set trading protection; position was flattened automatically: "
                    f"{exc}"
                ) from exc
            log.warning("Failed to set trading stop for %s: %s", trade.trade_id, exc)
        log.info(
            "Real order placed: %s side=%s qty=%s avg_price=%s",
            confirmed_order.order_id,
            trade.side,
            confirmed_order.executed_qty,
            trade.entry_price,
        )

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
        trade.margin = (
            trade.entry_price * trade.quantity / Decimal(str(trade.leverage))
        ).quantize(Decimal("0.00000001"))
        clamp_note = (
            "exchange_leverage_clamped="
            f"{previous_leverage}x->{trade.leverage}x "
            f"qty={previous_quantity}->{trade.quantity}"
        )
        if trade.message_history:
            trade.message_history[-1].notes.append(clamp_note)

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
                continue
            if candidate != original_leverage and trade.message_history:
                trade.message_history[-1].notes.append(
                    "exchange_leverage_fallback="
                    f"{original_leverage}x->{trade.leverage}x "
                    f"qty={original_quantity}->{trade.quantity}"
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
            level for level in [75, 50, 40, 25, 20, 10, 5, 3, 2, 1]
            if level < requested_leverage
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
        assert self._futures_client is not None
        await self._cancel_existing_trade_protection(trade)
        order, requested_quantity = await self._submit_exchange_close_order(trade, fraction)
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
            executed_quantity
            if avg_price > 0 and executed_quantity > 0
            else Decimal("0")
        )
        exchange_position: Any | None = None
        exchange_remaining_quantity: Decimal | None = None
        if fraction >= Decimal("1"):
            exchange_position, exchange_remaining_quantity = (
                await self._fetch_trade_exchange_position_quantity(
                    trade=trade,
                    spec=spec,
                )
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
                residual_confirmed_order, residual_fills = (
                    await self._futures_client.wait_for_order_fill(
                        symbol=trade.symbol,
                        order_id=residual_order.order_id,
                        use_demo_symbol=self._use_demo_exchange_symbol(),
                        timeout_seconds=float(
                            self.settings.LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS
                        ),
                    )
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
                exchange_position, exchange_remaining_quantity = (
                    await self._fetch_trade_exchange_position_quantity(
                        trade=trade,
                        spec=spec,
                    )
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
        trade.exchange_position = exchange_position
        try:
            await self._refresh_account()
        except Exception:
            log.debug("Account refresh after close failed", exc_info=True)
        if trade.is_open:
            try:
                await self._sync_trade_protection(trade)
            except Exception as exc:
                trade.last_exchange_sync_error = str(exc)
                log.warning(
                    "Failed to refresh trading stop after close for %s: %s",
                    trade.trade_id,
                    exc,
                )
        return _ExchangeCloseResult(
            order_id=",".join(close_order_ids),
            status=confirmed_order.status,
            avg_price=avg_price,
            executed_quantity=executed_quantity,
            realized_pnl=realized_pnl,
            fees=fees,
        )

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
    ) -> None:
        closed_quantity = min(trade.remaining_quantity, executed_quantity)
        trade.realized_pnl += realized_pnl
        trade.fees += fees
        trade.remaining_quantity = max(Decimal("0"), trade.remaining_quantity - closed_quantity)
        if avg_price > 0:
            trade.exit_price = avg_price
            trade.mark_price = avg_price
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
            f"executed_qty={closed_quantity} avg_price={avg_price} "
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
        else:
            trade.status = "partial_close"

    def _book_trade_realized_totals(self, trade: LiveTrade) -> None:
        pnl_delta = trade.realized_pnl - trade.realized_pnl_booked
        fee_delta = trade.fees - trade.fees_booked
        if pnl_delta:
            self.session.total_realized_pnl += pnl_delta
            trade.realized_pnl_booked = trade.realized_pnl
        if fee_delta:
            self.session.total_fees += fee_delta
            trade.fees_booked = trade.fees

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

    async def _ensure_trade_still_open_on_exchange(
        self,
        *,
        context: ChannelContext,
        trade: LiveTrade,
        reason: str,
    ) -> bool:
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
            return True
        self._mark_trade_closed_on_exchange(
            context=context,
            trade=trade,
            reason=reason,
        )
        return False

    def _mark_trade_closed_on_exchange(
        self,
        *,
        context: ChannelContext | None,
        trade: LiveTrade,
        reason: str,
    ) -> None:
        if trade.signal_id not in self._open_trades:
            return
        trade.status = "closed"
        trade.close_reason = reason
        trade.closed_at = _utc_now()
        trade.unrealized_pnl = Decimal("0")
        trade.exchange_position = None
        trade.sl_order_id = None
        trade.tp_order_ids = []
        trade.last_exchange_sync_error = reason
        trade.add_attribution(
            MessageAttribution(
                message_id=0,
                channel_id=trade.channel_id,
                channel_label=trade.channel_label,
                message_preview="exchange position missing during sync",
                message_date=_utc_now(),
                action="closed",
                notes=[reason],
            )
        )
        self._open_trades.pop(trade.signal_id, None)
        self.session.open_positions_count = max(0, self.session.open_positions_count - 1)
        self.session.closed_trades_count += 1
        self.store.save_trade(trade)
        self._emit_trade_update(trade)
        if context is not None:
            self._mark_signal_terminal(
                context=context,
                signal_id=trade.signal_id,
                status=SignalStatus.CLOSED,
                trade=trade,
            )

    async def _sync_trade_protection(
        self,
        trade: LiveTrade,
        *,
        refresh_take_profits: bool = True,
        refresh_stop_loss: bool = True,
    ) -> None:
        if self._futures_client is None or not trade.is_open:
            return
        stop_loss, normalized_take_profits = await self._normalize_trade_protection_levels(trade)
        trade.stop_loss = stop_loss
        trade.take_profits = normalized_take_profits
        tp_orders = self._exchange_take_profit_orders(trade) if refresh_take_profits else []
        if refresh_take_profits or refresh_stop_loss:
            await self._cancel_existing_trade_protection(
                trade,
                cancel_take_profits=refresh_take_profits,
                cancel_stop_loss=refresh_stop_loss,
            )
        if stop_loss is None and not tp_orders:
            if refresh_stop_loss:
                trade.sl_order_id = None
            if refresh_take_profits:
                trade.tp_order_ids = []
            self._persist_trade_runtime_state(trade)
            return
        close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        if refresh_take_profits:
            trade.tp_order_ids = []
            for target_index, tp_price, tp_quantity in tp_orders:
                order = await self._futures_client.place_order(
                    symbol=trade.symbol,
                    side=close_side,
                    order_type="LIMIT",
                    quantity=tp_quantity,
                    price=tp_price,
                    client_order_id=self._make_tp_client_order_id(trade, target_index),
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
                trade.tp_order_ids.append(order.order_id)
        if refresh_stop_loss and stop_loss is not None:
            side = "LONG" if trade.side == "long" else "SHORT"
            await self._futures_client.set_trading_stop(
                symbol=trade.symbol,
                side=side,
                stop_loss=stop_loss,
                sl_quantity=trade.remaining_quantity,
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
        await self._refresh_trade_protection_ids(trade)
        self._persist_trade_runtime_state(trade)

    def _make_tp_client_order_id(self, trade: LiveTrade, target_index: int) -> str:
        return f"triak_tp_{trade.trade_id}_{target_index + 1}_{uuid.uuid4().hex[:10]}"

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

    async def _cancel_existing_trade_protection(
        self,
        trade: LiveTrade,
        *,
        cancel_take_profits: bool = True,
        cancel_stop_loss: bool = True,
    ) -> None:
        if self._futures_client is None:
            return
        tp_order_prefix = f"triak_tp_{trade.trade_id}_"
        target_close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        if cancel_take_profits:
            tp_order_ids = set(trade.tp_order_ids)
            try:
                regular_orders = await self._futures_client.get_open_orders(
                    trade.symbol,
                    use_demo_symbol=self._use_demo_exchange_symbol(),
                )
            except Exception as exc:
                regular_orders = []
                log.debug(
                    "Failed to query existing TP orders for %s before cancel: %s",
                    trade.trade_id,
                    exc,
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
                    log.debug(
                        "Failed to cancel existing TP order %s for %s: %s",
                        order_id,
                        trade.trade_id,
                        exc,
                    )
            trade.tp_order_ids = []
        if cancel_stop_loss:
            orders = await self._futures_client.get_open_orders(
                trade.symbol,
                order_type="STOP_PROFIT_LOSS",
                use_demo_symbol=self._use_demo_exchange_symbol(),
            )
            target_order_prefix = "STOP_LONG_" if trade.side == "long" else "STOP_SHORT_"
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
                    log.debug(
                        "Failed to cancel existing trading stop %s for %s: %s",
                        order.order_id,
                        trade.trade_id,
                        exc,
                    )
            trade.sl_order_id = None

    async def _refresh_trade_protection_ids(self, trade: LiveTrade) -> None:
        if self._futures_client is None:
            return
        regular_open_orders = await self._futures_client.get_open_orders(
            trade.symbol,
            use_demo_symbol=self._use_demo_exchange_symbol(),
        )
        open_orders = await self._futures_client.get_open_orders(
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
        sl_order_id: str | None = None
        target_order_prefix = "STOP_LONG_" if trade.side == "long" else "STOP_SHORT_"
        target_close_side = "SELL_CLOSE" if trade.side == "long" else "BUY_CLOSE"
        for order in open_orders:
            order_type = order.order_type.upper()
            if order.stop_price <= 0 or "STOP_" not in order_type:
                continue
            if order.side.upper() != target_close_side:
                continue
            if not order_type.startswith(target_order_prefix):
                continue
            if "LOSS" in order_type:
                sl_order_id = order.order_id
        trade.tp_order_ids = tp_order_ids
        trade.sl_order_id = sl_order_id

    def _trade_has_exchange_protection(self, trade: LiveTrade) -> bool:
        return bool(trade.sl_order_id or trade.tp_order_ids)

    def _detach_trade_protection_order(self, trade: LiveTrade, order_id: str) -> None:
        if trade.sl_order_id == order_id:
            trade.sl_order_id = None
        if trade.tp_order_ids:
            trade.tp_order_ids = [item for item in trade.tp_order_ids if item != order_id]

    async def _reconcile_exchange_trade_protection(
        self,
        *,
        trade: LiveTrade,
        open_regular_orders: list[Any],
        open_protection_orders: list[Any],
        symbol_user_trades: list[Any],
    ) -> None:
        if self._futures_client is None or not self._trade_has_exchange_protection(trade):
            return
        open_regular_ids = {order.order_id for order in open_regular_orders}
        open_stop_ids = {order.order_id for order in open_protection_orders}
        for order_id in list(trade.tp_order_ids):
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
            await self._apply_exchange_protection_fill(
                trade=trade,
                protection_order=order,
                symbol_user_trades=symbol_user_trades,
            )
            return

        if trade.sl_order_id is None:
            return
        for order_id in [trade.sl_order_id]:
            if order_id in open_stop_ids:
                continue
            try:
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

    def _exchange_take_profit_orders(
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

    async def _apply_exchange_protection_fill(
        self,
        *,
        trade: LiveTrade,
        protection_order: Any,
        symbol_user_trades: list[Any],
    ) -> None:
        assert self._futures_client is not None
        executed_order_id = protection_order.executed_order_id or protection_order.order_id
        fills = [
            fill for fill in symbol_user_trades
            if fill.order_id in {executed_order_id, protection_order.order_id}
        ]
        if not fills:
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
            weighted_notional / executed_contract_qty
            if executed_contract_qty > 0
            else Decimal("0")
        )
        realized_pnl = sum((fill.realized_pnl for fill in fills), Decimal("0"))
        fees = sum((fill.commission for fill in fills), Decimal("0"))
        is_tp = (
            protection_order.order_id in trade.tp_order_ids
            or "PROFIT" in protection_order.order_type.upper()
        )
        current_target_index = trade.targets_hit
        reason = f"tp{current_target_index + 1}_hit" if is_tp else "sl_hit"
        attribution = MessageAttribution(
            message_id=0,
            channel_id=trade.channel_id,
            channel_label=trade.channel_label,
            message_preview=(
                "exchange take-profit fill" if is_tp else "exchange stop-loss fill"
            ),
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
            requested_quantity=executed_quantity,
            executed_quantity=executed_quantity,
            avg_price=avg_price,
            realized_pnl=realized_pnl,
            fees=fees,
            reason=reason,
            order_id=executed_order_id,
            message=attribution,
        )
        self._detach_trade_protection_order(trade, protection_order.order_id)
        if is_tp and self._strategy is not None:
            remaining = len(trade.take_profits) - current_target_index
            action = self._strategy.get_target_hit_action(
                targets_hit_so_far=current_target_index,
                remaining_targets_including_this=remaining,
                entry_price=trade.entry_price,
                take_profits=trade.take_profits,
            )
            trade.targets_hit += 1
            if action.new_stop_loss is not None and trade.stop_loss != action.new_stop_loss:
                trade.stop_loss = action.new_stop_loss
                attribution.notes.append(f"next_stop_loss={action.new_stop_loss}")
            elif action.move_sl_to_entry and trade.stop_loss != trade.entry_price:
                trade.stop_loss = trade.entry_price
                attribution.notes.append(f"next_stop_loss={trade.entry_price}")
        if trade.is_open:
            await self._sync_trade_protection(trade)
            return
        trade.sl_order_id = None
        trade.tp_order_ids = []
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
        self.store.save_trade(trade)
        self._emit_trade_update(trade)
        log.info(
            "Closed trade %s %s pnl=%.4f reason=%s",
            trade.trade_id, trade.symbol,
            float(trade.realized_pnl), trade.close_reason,
        )

    def _channel_label(self, channel_id: str) -> str:
        for ch in self.session.channels:
            slug = ch.rsplit("/", 1)[-1].lstrip("@")
            cid = channel_id.lstrip("@").lower()
            if cid == slug.lower() or ch == channel_id or ch.lower() == channel_id.lower():
                return f"@{slug}"
        # If it looks like a URL already, extract slug
        if channel_id.startswith("https://t.me/"):
            return f"@{channel_id.rsplit('/', 1)[-1]}"
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
                self.notifier({
                    "type": "live_message",
                    "message": trace.model_dump(mode="json"),
                })
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
                        self.notifier({
                            "type": "live_message",
                            "message": trace.model_dump(mode="json"),
                        })
                    except Exception:
                        pass
                return

    def _emit_session_update(self) -> None:
        if not self.notifier:
            return
        try:
            self.notifier({
                "type": "live_session",
                "session": self.session.model_dump(mode="json"),
            })
        except Exception:
            pass

    def _emit_trade_update(self, trade: LiveTrade) -> None:
        if not self.notifier:
            return
        try:
            self.notifier({
                "type": "live_trade",
                "trade": trade.model_dump(mode="json"),
            })
        except Exception:
            pass

    async def _sync_exchange_state(self) -> None:
        if self._futures_client is None:
            return
        use_demo_symbol = self._use_demo_exchange_symbol()
        positions = await self._futures_client.get_open_positions(
            use_demo_symbol=use_demo_symbol
        )
        order_symbols = {trade.symbol for trade in self._open_trades.values() if trade.symbol}
        recent_orders: list[LiveExchangeOrderSnapshot] = []
        by_symbol_orders: dict[str, list[LiveExchangeOrderSnapshot]] = {}
        by_symbol_open_regular_orders: dict[str, list[Any]] = {}
        by_symbol_open_protection_orders: dict[str, list[Any]] = {}
        by_symbol_user_trades: dict[str, list[Any]] = {}
        for symbol in sorted(order_symbols):
            try:
                history = await self._futures_client.get_order_history(
                    symbol,
                    limit=20,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception:
                continue
            snapshots = [self._order_snapshot(item) for item in history]
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
            by_symbol_open_protection_orders[symbol] = protection_orders
            recent_orders.extend(self._order_snapshot(item) for item in protection_orders[:5])
            try:
                symbol_user_trades = await self._futures_client.get_user_trades(
                    symbol,
                    limit=50,
                    use_demo_symbol=use_demo_symbol,
                )
            except Exception:
                symbol_user_trades = []
            by_symbol_user_trades[symbol] = symbol_user_trades

        position_snapshots = [self._position_snapshot(item) for item in positions]
        self.session.exchange_snapshot = LiveExchangeSnapshot(
            positions=position_snapshots,
            recent_orders=recent_orders[:40],
            error=None,
        )

        for trade in list(self._open_trades.values()):
            await self._reconcile_exchange_trade_protection(
                trade=trade,
                open_regular_orders=by_symbol_open_regular_orders.get(trade.symbol, []),
                open_protection_orders=by_symbol_open_protection_orders.get(trade.symbol, []),
                symbol_user_trades=by_symbol_user_trades.get(trade.symbol, []),
            )
            if trade.signal_id not in self._open_trades:
                continue
            trade.exchange_position = self._find_matching_exchange_position(
                trade=trade,
                positions=position_snapshots,
            )
            context = self._contexts.get(trade.channel_id)
            if trade.exchange_position is None:
                self._mark_trade_closed_on_exchange(
                    context=context,
                    trade=trade,
                    reason="exchange_position_missing",
                )
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
            status_group="active" if state.status in {
                SignalStatus.PENDING_CONSOLIDATION,
                SignalStatus.OPEN,
            } else "inactive",
            symbol=parsed.symbol if parsed else None,
            exchange_symbol=trade.exchange_symbol if trade is not None else None,
            side=parsed.side.value if parsed else None,
            entry_low=parsed.entry_low if parsed else None,
            entry_high=parsed.entry_high if parsed else None,
            entry_zone=entry_zone,
            stop_loss=(
                trade.stop_loss
                if trade is not None
                else (parsed.stop_loss if parsed else None)
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
    def _position_snapshot(position: Any) -> LiveExchangePositionSnapshot:
        return LiveExchangePositionSnapshot(
            symbol=position.symbol_internal,
            exchange_symbol=position.exchange_symbol,
            side=position.side,
            quantity=position.position,
            available=position.available,
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
) -> tuple[LiveSession, LiveTradingEngine]:
    session_id = f"ls_{uuid.uuid4().hex[:12]}"
    labels = [
        f"@{ch.rsplit('/', 1)[-1]}" if "/" in ch else ch
        for ch in config.channels
    ]
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
        label=config.label,
    )
    engine = LiveTradingEngine(
        settings=settings,
        session=session,
        store=store,
        notifier=notifier,
    )
    return session, engine


def build_engine_from_session(
    *,
    session: LiveSession,
    settings: Settings,
    store: LiveTradingStore,
    notifier: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[LiveSession, LiveTradingEngine]:
    engine = LiveTradingEngine(
        settings=settings,
        session=session,
        store=store,
        notifier=notifier,
    )
    return session, engine
