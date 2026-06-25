"""Live / demo trading engine — real-time Telegram signal processing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
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
from triak_trade.domain.enums import SignalAction, SignalStatus
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.futures import (
    ToobitFuturesClient,
    build_futures_client_from_settings,
)
from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveMessageTrace,
    LiveSession,
    LiveSessionConfig,
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
            except Exception:
                log.warning("AI classifier init failed, falling back to regex", exc_info=True)
                self._classifier = RegexMessageClassifier()
        else:
            self._classifier = RegexMessageClassifier()
            log.info("LiveTradingEngine: using regex classifier")

        self._telegram_client = TelethonTelegramClient(settings=s)

        # Build futures client for both demo (mark prices) and live (orders)
        try:
            self._futures_client = build_futures_client_from_settings(s)
        except Exception:
            log.warning("Failed to build futures client", exc_info=True)

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
            channel_label=channel_label,
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
                trace.signal_id = related_signal_id
                trace.final_status = "signal_updated"
                trace.effect_summary = f"Updated pending signal {related_signal_id}"

        else:
            # Follow-up directive (CLOSE, UPDATE_SL, UPDATE_TP, CANCEL, etc.)
            if related_signal_id is not None:
                trace.signal_id = related_signal_id
                context.attach_message(related_signal_id, message)
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
            mark = await self._get_mark_price(trade.symbol)
            close_price = mark if mark else trade.entry_price
            attribution = MessageAttribution(
                message_id=message.message_id,
                channel_id=message.channel_id,
                channel_label=channel_label,
                message_preview=(message.text or "")[:200],
                message_date=message.date,
                action="closed",
            )
            self._pm.close_trade(
                trade=trade,
                close_price=close_price,
                reason="manual_close_all",
                fee_rate_pct=fee_rate,
                message=attribution,
            )
            self._finalize_closed_trade(trade)
            if self.session.trading_mode == "live" and self._futures_client:
                await self._real_close_position(trade, Decimal("1"))
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
                trace.final_status = "closed_trade"
                trace.effect_summary = f"Closed {trade.symbol} @ {close_price}, PnL={pnl:.4f}"
                if self.session.trading_mode == "live" and self._futures_client:
                    await self._real_close_position(trade, fraction)
            else:
                pnl = self._pm.apply_partial_close(
                    trade=trade,
                    close_fraction=fraction,
                    close_price=close_price,
                    reason=f"partial_{int(fraction * 100)}pct",
                    fee_rate_pct=fee_rate,
                    message=attribution,
                )
                self.session.total_realized_pnl += pnl
                if self.session.trading_mode == "demo":
                    self.session.paper_balance += pnl
                trace.final_status = "partial_close"
                trace.effect_summary = (
                    f"Partial close {int(fraction * 100)}% of {trade.symbol} @ {close_price}"
                )
                if self.session.trading_mode == "live" and self._futures_client:
                    await self._real_close_position(trade, fraction)

        elif effective_action is SignalAction.UPDATE_SL or move_to_entry:
            self._pm.update_stop_loss(
                trade=trade,
                new_sl=parsed.stop_loss,
                message=attribution,
                move_to_entry=move_to_entry,
            )
            new_sl = trade.entry_price if move_to_entry else parsed.stop_loss
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
                trace.final_status = "updated_tp"
                trace.effect_summary = f"TPs updated: {[str(t) for t in new_tps]}"
            else:
                trace.final_status = "no_tp_found"
                trace.effect_summary = "UPDATE_TP but no TP values extracted"
        else:
            trace.final_status = "unhandled_followup"
            trace.effect_summary = f"Action={effective_action.value} not handled"

        trace.trade_id = trade.trade_id
        self.store.save_trade(trade)
        self._emit_trade_update(trade)

    # ── Consolidation Tick ─────────────────────────────────────────────────

    async def _consolidation_tick_loop(self) -> None:
        while self._running:
            await asyncio.sleep(10)
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
        ok, errors = self._validator.validate_for_backtest_open(parsed)
        if not ok:
            log.debug("Signal %s failed validation: %s", signal_id, errors)
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
            self._emit_trace_update(signal_id=signal_id, status="invalid", note=str(errors))
            return

        if not parsed.symbol:
            state.status = SignalStatus.INVALID
            context.add_signal(state, pending=False)
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

        await self._open_position(signal_id=signal_id, state=state, parsed=parsed, context=context)

    async def _open_position(
        self,
        *,
        signal_id: str,
        state: SignalState,
        parsed: ParsedSignal,
        context: ChannelContext,
    ) -> None:
        assert self._pm is not None and self._strategy is not None

        orig_msg = context.get_message(state.created_from_message_id)
        trigger_id = state.created_from_message_id
        trigger_text = (orig_msg.text or "")[:200] if orig_msg else ""
        trigger_date = orig_msg.date if orig_msg else state.created_at
        channel_label = self._channel_label(state.channel_id)
        channel_input = self._channel_input(state.channel_id)

        current_balance = (
            self.session.paper_balance
            if self.session.trading_mode == "demo"
            else self._live_balance_for_sizing()
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
        if self.session.trading_mode == "live" and self._futures_client:
            try:
                await self._real_open_position(trade)
            except Exception as exc:
                log.error("Real order failed for trade %s: %s", trade.trade_id, exc)
                trade.status = "closed"
                trade.close_reason = f"order_failed: {exc}"
                trade.closed_at = _utc_now()
                self.store.save_trade(trade)
                self._emit_trace_update(
                    signal_id=signal_id, status="order_failed", note=str(exc)
                )
                return

        # Register trade
        self._open_trades[signal_id] = trade
        state.status = SignalStatus.OPEN
        context.add_signal(state, pending=False)
        self.session.total_signals_opened += 1
        self.session.open_positions_count += 1
        if self.session.trading_mode == "demo":
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
                    self._pm.close_trade(
                        trade=trade, close_price=mark,
                        reason="sl_hit", fee_rate_pct=fee_rate,
                    )
                    self._finalize_closed_trade(trade)
                    if self.session.trading_mode == "live" and self._futures_client:
                        await self._real_close_position(trade, Decimal("1"))
                    to_remove.append(signal_id)
                    break

            if not trade.is_open and signal_id not in to_remove:
                to_remove.append(signal_id)
            self.store.save_trade(trade)

        for sid in to_remove:
            self._open_trades.pop(sid, None)

        self.session.total_unrealized_pnl = sum(
            (t.unrealized_pnl for t in self._open_trades.values()),
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
        pnl = self._pm.apply_partial_close(
            trade=trade,
            close_fraction=action.close_fraction,
            close_price=mark,
            reason=f"tp{idx + 1}_hit",
            fee_rate_pct=fee_rate,
            is_tp_hit=True,
        )
        self.session.total_realized_pnl += pnl
        if self.session.trading_mode == "demo":
            self.session.paper_balance += pnl + trade.margin * action.close_fraction

        if action.move_sl_to_entry and trade.stop_loss != trade.entry_price:
            trade.stop_loss = trade.entry_price

        if self.session.trading_mode == "live" and self._futures_client:
            await self._real_close_position(trade, action.close_fraction)

        if not trade.is_open:
            self._finalize_closed_trade(trade)

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
            info = await self._futures_client.get_full_account_info()
            self.session.account_info = LiveAccountInfo(
                wallet_balance=info.total_wallet_balance,
                available_balance=info.available_balance,
                unrealized_pnl=info.total_unrealized_profit,
                margin_balance=info.available_balance + info.total_position_margin,
                total_position_margin=info.total_position_margin,
                max_withdraw=info.available_balance,
            )
            # Store user_id once
            if info.user_id and not getattr(self.session, "_user_id", None):
                self.session.label = self.session.label or f"User {info.user_id}"
        except Exception as exc:
            self.session.account_info = LiveAccountInfo(
                error=f"account fetch failed: {exc}"
            )
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

    # ── Real Exchange Execution ────────────────────────────────────────────

    async def _real_open_position(self, trade: LiveTrade) -> None:
        assert self._futures_client is not None
        try:
            await self._futures_client.set_leverage(trade.symbol, trade.leverage)
        except Exception as exc:
            log.warning("set_leverage failed for %s: %s", trade.symbol, exc)

        if trade.side == "long":
            order = await self._futures_client.open_long(
                symbol=trade.symbol,
                quantity=trade.quantity,
                leverage=trade.leverage,
            )
        else:
            order = await self._futures_client.open_short(
                symbol=trade.symbol,
                quantity=trade.quantity,
                leverage=trade.leverage,
            )

        trade.entry_order_id = order.order_id
        if order.avg_price and order.avg_price > 0:
            trade.entry_price = order.avg_price
        log.info(
            "Real order placed: %s side=%s qty=%s avg_price=%s",
            order.order_id, trade.side, order.executed_qty, order.avg_price,
        )

    async def _real_close_position(self, trade: LiveTrade, fraction: Decimal) -> None:
        if self._futures_client is None:
            return
        try:
            close_qty = (trade.remaining_quantity * fraction).quantize(Decimal("0.00001"))
            if close_qty <= 0:
                return
            if trade.side == "long":
                await self._futures_client.close_long(
                    symbol=trade.symbol, quantity=close_qty
                )
            else:
                await self._futures_client.close_short(
                    symbol=trade.symbol, quantity=close_qty
                )
        except Exception as exc:
            log.warning("Real close failed for %s: %s", trade.trade_id, exc)

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _get_mark_price(self, symbol: str) -> Decimal | None:
        if self._futures_client is None:
            return None
        try:
            return await self._futures_client.get_mark_price(symbol)
        except Exception:
            return None

    def _finalize_closed_trade(self, trade: LiveTrade) -> None:
        self.session.total_realized_pnl += trade.realized_pnl
        if self.session.trading_mode == "demo":
            self.session.paper_balance += trade.realized_pnl + trade.margin
        self.session.open_positions_count = max(0, self.session.open_positions_count - 1)
        self.session.closed_trades_count += 1
        if trade.realized_pnl >= 0:
            self.session.wins += 1
        else:
            self.session.losses += 1
        self.session.total_fees += trade.fees
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
    initial_balance = config.initial_balance if config.trading_mode == "demo" else Decimal("0")
    session = LiveSession(
        session_id=session_id,
        channels=config.channels,
        channel_labels=labels,
        trading_mode=config.trading_mode,
        initial_balance=initial_balance,
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
