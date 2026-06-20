"""Event-driven trade simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.core.symbols import canonical_market_symbol, same_market_symbol
from triak_trade.domain.enums import BacktestFillPolicy, EntryType, SignalAction, TradeSide
from triak_trade.domain.models import Candle, SimulatedTrade


@dataclass(frozen=True)
class PriceLevelSpan:
    kind: str
    label: str
    value: Decimal
    started_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True)
class SignalPricePoint:
    timestamp: datetime
    candle_open_time: datetime
    candle_close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    stop_loss: Decimal | None
    take_profits: list[Decimal]
    mark_price: Decimal
    source_message_id: int | None = None


@dataclass
class _OpenPosition:
    trade_id: str
    signal_id: str
    channel_id: str
    symbol: str
    side: TradeSide
    entry_price: Decimal
    stop_loss: Decimal
    take_profits: list[Decimal]
    original_quantity: Decimal
    remaining_quantity: Decimal
    entry_time: datetime
    realized_pnl: Decimal
    realized_fees: Decimal
    exit_price: Decimal | None
    exit_time: datetime | None
    status: str
    notes: list[str]
    targets_hit: int
    manual_partial_exit: bool
    effective_leverage: Decimal = Decimal("1")
    fee_rate_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class SimulationSignalState:
    signal_id: str
    symbol: str
    side: TradeSide
    status: str
    original_quantity: Decimal
    open_quantity: Decimal
    entry_price: Decimal | None
    stop_loss: Decimal | None
    take_profits: list[Decimal]
    notional_value: Decimal
    risk_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl_pct: Decimal
    mark_price: Decimal
    entry_time: datetime
    exit_time: datetime | None
    exit_price: Decimal | None
    targets_hit: int
    notes: list[str]
    effective_leverage: Decimal = Decimal("1")
    margin: Decimal = Decimal("0")
    price_history: list[SignalPricePoint] | None = None
    stop_loss_history: list[PriceLevelSpan] | None = None
    take_profit_history: list[PriceLevelSpan] | None = None


@dataclass(frozen=True)
class SimulationSnapshot:
    timestamp: datetime
    source_message_id: int | None
    open_positions: int
    closed_trades: int
    wins: int
    losses: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    realized_balance: Decimal
    current_balance: Decimal
    signal_states: dict[str, SimulationSignalState]
    checkpoint_kind: str = "message"


class BacktestSimulator:
    def simulate(
        self,
        *,
        events: list[BacktestEvent],
        candles: list[Candle],
        initial_balance: Decimal,
        risk_per_trade_pct: Decimal,
        fill_policy: BacktestFillPolicy,
        active_signal_hours: int | None = None,
        max_effective_leverage: Decimal | None = None,
        default_stop_pct: Decimal = Decimal("5"),
        strategy: TradeStrategy | None = None,
        fee_rate_pct: Decimal = Decimal("0"),
        default_signal_leverage: Decimal = Decimal("1"),
    ) -> tuple[list[SimulatedTrade], Decimal]:
        trades, balance, _snapshots = self._simulate_internal(
            events=events,
            candles=candles,
            initial_balance=initial_balance,
            risk_per_trade_pct=risk_per_trade_pct,
            fill_policy=fill_policy,
            active_signal_hours=active_signal_hours,
            capture_snapshots=False,
            max_effective_leverage=max_effective_leverage,
            default_stop_pct=default_stop_pct,
            strategy=strategy,
            fee_rate_pct=fee_rate_pct,
            default_signal_leverage=default_signal_leverage,
        )
        return trades, balance

    def simulate_with_snapshots(
        self,
        *,
        events: list[BacktestEvent],
        candles: list[Candle],
        initial_balance: Decimal,
        risk_per_trade_pct: Decimal,
        fill_policy: BacktestFillPolicy,
        active_signal_hours: int | None = None,
        max_effective_leverage: Decimal | None = None,
        default_stop_pct: Decimal = Decimal("5"),
        strategy: TradeStrategy | None = None,
        snapshot_interval: timedelta | None = None,
        fee_rate_pct: Decimal = Decimal("0"),
        default_signal_leverage: Decimal = Decimal("1"),
    ) -> tuple[list[SimulatedTrade], Decimal, list[SimulationSnapshot]]:
        return self._simulate_internal(
            events=events,
            candles=candles,
            initial_balance=initial_balance,
            risk_per_trade_pct=risk_per_trade_pct,
            fill_policy=fill_policy,
            active_signal_hours=active_signal_hours,
            capture_snapshots=True,
            max_effective_leverage=max_effective_leverage,
            default_stop_pct=default_stop_pct,
            strategy=strategy,
            snapshot_interval=snapshot_interval,
            fee_rate_pct=fee_rate_pct,
            default_signal_leverage=default_signal_leverage,
        )

    def _simulate_internal(
        self,
        *,
        events: list[BacktestEvent],
        candles: list[Candle],
        initial_balance: Decimal,
        risk_per_trade_pct: Decimal,
        fill_policy: BacktestFillPolicy,
        active_signal_hours: int | None,
        capture_snapshots: bool,
        max_effective_leverage: Decimal | None = None,
        default_stop_pct: Decimal = Decimal("5"),
        strategy: TradeStrategy | None = None,
        snapshot_interval: timedelta | None = None,
        fee_rate_pct: Decimal = Decimal("0"),
        default_signal_leverage: Decimal = Decimal("1"),
    ) -> tuple[list[SimulatedTrade], Decimal, list[SimulationSnapshot]]:
        trades: list[SimulatedTrade] = []
        closed_trades_by_signal: dict[str, SimulatedTrade] = {}
        snapshots: list[SimulationSnapshot] = []
        open_positions: dict[str, _OpenPosition] = {}
        balance = initial_balance
        sorted_events = sorted(events, key=lambda item: item.timestamp)
        sorted_candles = sorted(candles, key=lambda item: item.open_time)
        signal_price_history: dict[str, list[SignalPricePoint]] = {}
        stop_loss_history: dict[str, list[PriceLevelSpan]] = {}
        take_profit_history: dict[str, list[PriceLevelSpan]] = {}
        next_snapshot_at = self._initial_snapshot_anchor(
            candles=sorted_candles,
            events=sorted_events,
            snapshot_interval=snapshot_interval,
        )
        candle_index = 0

        for event in sorted_events:
            candle_index, resolved_trades = self._process_candles_until(
                open_positions=open_positions,
                candles=sorted_candles,
                start_index=candle_index,
                stop_at=event.timestamp,
                fill_policy=fill_policy,
                active_signal_hours=active_signal_hours,
                strategy=strategy,
                capture_snapshots=capture_snapshots,
                snapshots=snapshots,
                closed_trades_by_signal=closed_trades_by_signal,
                initial_balance=initial_balance,
                signal_price_history=signal_price_history,
                stop_loss_history=stop_loss_history,
                take_profit_history=take_profit_history,
                next_snapshot_at=next_snapshot_at,
                snapshot_interval=snapshot_interval,
            )
            if capture_snapshots and snapshot_interval is not None:
                next_snapshot_at = self._advance_snapshot_anchor(
                    next_snapshot_at,
                    snapshots,
                    snapshot_interval,
                )
            for trade in resolved_trades:
                trades.append(trade)
                closed_trades_by_signal[trade.signal_id] = trade
                balance += trade.pnl
            parsed = event.parsed_signal
            if parsed.action is SignalAction.OPEN and parsed.symbol:
                entry_price, entry_time = self._find_entry_execution(
                    parsed.entry_type,
                    parsed.entry_low,
                    parsed.entry_high,
                    event.timestamp,
                    sorted_candles,
                    parsed.symbol,
                )
                if entry_price is None or entry_time is None:
                    no_fill_trade = SimulatedTrade(
                        trade_id=f"no_fill_{event.signal_id or 'x'}",
                        signal_id=event.signal_id or "unknown",
                        channel_id=parsed.source_channel_id,
                        symbol=parsed.symbol,
                        side=parsed.side,
                        entry_time=None,
                        exit_time=None,
                        entry_price=None,
                        exit_price=None,
                        quantity=Decimal("0"),
                        pnl=Decimal("0"),
                        pnl_pct=Decimal("0"),
                        fees=Decimal("0"),
                        status="not_filled",
                        notes=["entry not touched"],
                    )
                    trades.append(no_fill_trade)
                    closed_trades_by_signal[no_fill_trade.signal_id] = no_fill_trade
                    if capture_snapshots:
                        snapshots.append(
                            self._build_snapshot(
                                timestamp=event.timestamp,
                                source_message_id=event.source_message_id,
                                open_positions=open_positions,
                                closed_trades_by_signal=closed_trades_by_signal,
                                candles=sorted_candles,
                                processed_candle_count=candle_index,
                                initial_balance=initial_balance,
                                signal_price_history=signal_price_history,
                                stop_loss_history=stop_loss_history,
                                take_profit_history=take_profit_history,
                                checkpoint_kind="message",
                            )
                        )
                    continue

                notes: list[str] = []
                # The channel may post a signal with no explicit stop_loss. We must
                # still open and track it, so synthesize a stop from the active
                # strategy (if any) or a fixed percentage from entry for sizing.
                # A real stop_loss, when present, always takes precedence.
                if parsed.stop_loss is not None:
                    effective_stop = parsed.stop_loss
                elif strategy is not None:
                    effective_stop = strategy.get_synthetic_stop(
                        side=parsed.side,
                        entry_price=entry_price,
                    )
                    notes.append(f"synthetic_stop_strategy={strategy.name}")
                else:
                    pct = max(default_stop_pct, Decimal("0")) / Decimal("100")
                    if parsed.side.is_short:
                        effective_stop = entry_price * (Decimal("1") + pct)
                    else:
                        effective_stop = entry_price * (Decimal("1") - pct)
                    notes.append(f"synthetic_stop_pct={default_stop_pct}")
                # If balance is zero or negative, we can't size a new position.
                if balance <= Decimal("0"):
                    continue
                risk_amount = (balance * risk_per_trade_pct) / Decimal("100")
                stop_distance = abs(entry_price - effective_stop)
                if stop_distance <= Decimal("0"):
                    continue
                qty = risk_amount / stop_distance
                # Leverage caps how much notional the balance can support. Keep
                # the risk-based quantity as primary sizing, but never let a
                # position exceed balance * effective_leverage in notional.
                signal_leverage = (
                    Decimal(event.leverage) if event.leverage else default_signal_leverage
                )
                if max_effective_leverage is None:
                    # Leverage modeling disabled: keep legacy risk-based sizing with
                    # no margin cap, and report return-on-notional (leverage 1).
                    effective_leverage = Decimal("1")
                else:
                    effective_leverage = min(
                        max(signal_leverage, Decimal("1")),
                        max(max_effective_leverage, Decimal("1")),
                    )
                    if entry_price > Decimal("0"):
                        max_qty_by_margin = (balance * effective_leverage) / entry_price
                        if qty > max_qty_by_margin:
                            notes.append(
                                f"quantity_capped_by_leverage; lev={effective_leverage}; "
                                f"risk_qty={qty}; capped_qty={max_qty_by_margin}"
                            )
                            qty = max_qty_by_margin
                # B7: portfolio-level margin check — only when leverage modeling
                # is active (max_effective_leverage is not None).  In legacy mode
                # (None) no margin model is in effect, so we skip this gate.
                #
                # Use max_effective_leverage (the exchange cap) for portfolio
                # accounting rather than each signal's effective_leverage.  When a
                # signal has no explicit leverage the effective_leverage falls to 1,
                # which would make used_margin equal the full notional and block all
                # subsequent positions unfairly.  The exchange-level cap reflects the
                # true shared margin capacity across the portfolio.
                if max_effective_leverage is not None:
                    portfolio_lev = max(max_effective_leverage, Decimal("1"))
                    used_margin = sum(
                        (pos.entry_price * pos.original_quantity) / portfolio_lev
                        for pos in open_positions.values()
                    )
                    new_margin = (entry_price * qty) / portfolio_lev
                    if used_margin + new_margin > balance:
                        free_margin = max(balance - used_margin, Decimal("0"))
                        if free_margin <= Decimal("0") or entry_price <= Decimal("0"):
                            notes.append("rejected_insufficient_portfolio_margin")
                            continue
                        # Clamp qty to what free margin actually allows.
                        clamped_qty = (free_margin * portfolio_lev) / entry_price
                        notes.append(
                            f"quantity_capped_portfolio_margin; "
                            f"used={used_margin:.4f}; free={free_margin:.4f}; "
                            f"req_margin={new_margin:.4f}; clamped_qty={clamped_qty:.6f}"
                        )
                        qty = clamped_qty

                # Filter take-profits to only those on the correct side of
                # entry.  A TP above entry for a SHORT (or below for a LONG)
                # is a parser artefact; including it would trigger an
                # accidental loss instead of a profit exit.
                valid_tps = [
                    tp for tp in parsed.take_profits
                    if (
                        parsed.side.is_long and tp > entry_price
                    ) or (
                        parsed.side.is_short and tp < entry_price
                    )
                ]
                if len(valid_tps) < len(parsed.take_profits):
                    dropped = len(parsed.take_profits) - len(valid_tps)
                    notes.append(f"tp_direction_filtered={dropped}")
                if not valid_tps and strategy is not None:
                    valid_tps = strategy.get_synthetic_take_profits(
                        side=parsed.side,
                        entry_price=entry_price,
                        stop_loss=effective_stop,
                    )
                    if valid_tps:
                        notes.append(
                            "synthetic_take_profits_strategy="
                            + ",".join(str(item) for item in valid_tps)
                        )
                entry_fee = (
                    entry_price * qty * fee_rate_pct / Decimal("100")
                    if fee_rate_pct > Decimal("0")
                    else Decimal("0")
                )
                open_positions[event.signal_id or f"sig_{len(open_positions)+1}"] = _OpenPosition(
                    trade_id=f"trade_{event.signal_id or len(open_positions)+1}",
                    signal_id=event.signal_id or "unknown",
                    channel_id=parsed.source_channel_id,
                    symbol=parsed.symbol,
                    side=parsed.side,
                    entry_price=entry_price,
                    stop_loss=effective_stop,
                    take_profits=valid_tps,
                    original_quantity=qty,
                    remaining_quantity=qty,
                    entry_time=entry_time,
                    realized_pnl=Decimal("0"),
                    realized_fees=entry_fee,
                    exit_price=None,
                    exit_time=None,
                    status="open",
                    notes=notes,
                    targets_hit=0,
                    manual_partial_exit=False,
                    effective_leverage=effective_leverage,
                    fee_rate_pct=fee_rate_pct,
                )
                self._set_signal_level_history(
                    stop_loss_history=stop_loss_history,
                    take_profit_history=take_profit_history,
                    signal_id=event.signal_id or "unknown",
                    timestamp=entry_time,
                    stop_loss=effective_stop,
                    take_profits=valid_tps,
                )
            elif parsed.action is SignalAction.CLOSE and event.close_all and open_positions:
                for signal_id in list(open_positions):
                    position = open_positions[signal_id]
                    close_price = (
                        self._first_candle_open_after(
                            event.timestamp,
                            sorted_candles,
                            position.symbol,
                        )
                        or position.entry_price
                    )
                    self._close_fraction_of_position(
                        position,
                        event.timestamp,
                        close_price,
                        Decimal("1"),
                        "manual_close_all",
                    )
                    trade = self._finalize_position(
                        position,
                        status=(
                            "closed"
                            if position.targets_hit == 0
                            else "partial_tp_then_close"
                        ),
                    )
                    trades.append(trade)
                    closed_trades_by_signal[trade.signal_id] = trade
                    balance += trade.pnl
                    del open_positions[signal_id]
            elif event.related_signal_id in open_positions:
                position = open_positions[event.related_signal_id]
                if parsed.action is SignalAction.CANCEL:
                    trade = self._close_remaining_position(
                        position,
                        event.timestamp,
                        position.entry_price,
                        "cancelled" if position.targets_hit == 0 else "partial_tp_then_cancel",
                    )
                    trades.append(trade)
                    closed_trades_by_signal[trade.signal_id] = trade
                    balance += trade.pnl
                    del open_positions[event.related_signal_id]
                elif parsed.action is SignalAction.CLOSE:
                    close_price = (
                        self._first_candle_open_after(
                            event.timestamp,
                            sorted_candles,
                            position.symbol,
                        )
                        or position.entry_price
                    )
                    fraction = event.close_fraction or Decimal("1")
                    self._close_fraction_of_position(
                        position,
                        event.timestamp,
                        close_price,
                        fraction,
                        "manual_partial_close" if fraction < Decimal("1") else "manual_close",
                    )
                    if fraction < Decimal("1"):
                        position.manual_partial_exit = True
                    if position.remaining_quantity <= Decimal("0"):
                        trade = self._finalize_position(
                            position,
                            status=(
                                "closed"
                                if position.targets_hit == 0
                                else "partial_tp_then_close"
                            ),
                        )
                        trades.append(trade)
                        closed_trades_by_signal[trade.signal_id] = trade
                        balance += trade.pnl
                        del open_positions[event.related_signal_id]
                elif parsed.action is SignalAction.UPDATE_SL and event.move_stop_to_entry:
                    position.stop_loss = position.entry_price
                    position.notes.append("stop_loss_moved_to_entry")
                    self._replace_stop_loss_history(
                        stop_loss_history=stop_loss_history,
                        signal_id=event.related_signal_id,
                        timestamp=event.timestamp,
                        stop_loss=position.stop_loss,
                    )
                elif parsed.action is SignalAction.UPDATE_SL and parsed.stop_loss is not None:
                    position.stop_loss = parsed.stop_loss
                    position.notes.append(f"stop_loss_updated={parsed.stop_loss}")
                    self._replace_stop_loss_history(
                        stop_loss_history=stop_loss_history,
                        signal_id=event.related_signal_id,
                        timestamp=event.timestamp,
                        stop_loss=parsed.stop_loss,
                    )
                elif parsed.action is SignalAction.UPDATE_TP and parsed.take_profits:
                    position.take_profits = (
                        position.take_profits[: position.targets_hit] + parsed.take_profits
                    )
                    position.notes.append(
                        "take_profits_updated="
                        + ",".join(str(item) for item in parsed.take_profits)
                    )
                    self._replace_take_profit_history(
                        take_profit_history=take_profit_history,
                        signal_id=event.related_signal_id,
                        timestamp=event.timestamp,
                        take_profits=position.take_profits,
                    )
            if capture_snapshots:
                snapshots.append(
                    self._build_snapshot(
                        timestamp=event.timestamp,
                        source_message_id=event.source_message_id,
                        open_positions=open_positions,
                        closed_trades_by_signal=closed_trades_by_signal,
                        candles=sorted_candles,
                        processed_candle_count=candle_index,
                        initial_balance=initial_balance,
                        signal_price_history=signal_price_history,
                        stop_loss_history=stop_loss_history,
                        take_profit_history=take_profit_history,
                        checkpoint_kind="message",
                    )
                )

        candle_index, resolved_trades = self._process_candles_until(
            open_positions=open_positions,
            candles=sorted_candles,
            start_index=candle_index,
            stop_at=None,
            fill_policy=fill_policy,
            active_signal_hours=active_signal_hours,
            strategy=strategy,
            capture_snapshots=capture_snapshots,
            snapshots=snapshots,
            closed_trades_by_signal=closed_trades_by_signal,
            initial_balance=initial_balance,
            signal_price_history=signal_price_history,
            stop_loss_history=stop_loss_history,
            take_profit_history=take_profit_history,
            next_snapshot_at=next_snapshot_at,
            snapshot_interval=snapshot_interval,
        )
        for trade in resolved_trades:
            trades.append(trade)
            closed_trades_by_signal[trade.signal_id] = trade
            balance += trade.pnl

        for signal_id, position in list(open_positions.items()):
            outcome = self._close_remaining_position(
                position,
                position.exit_time or position.entry_time,
                position.exit_price
                or self._last_price(
                    sorted_candles,
                    position.symbol,
                    position.entry_price,
                ),
                "open_until_end" if position.targets_hit == 0 else "partial_tp_open_until_end",
            )
            trades.append(outcome)
            closed_trades_by_signal[outcome.signal_id] = outcome
            balance += outcome.pnl
            del open_positions[signal_id]

        return trades, balance, snapshots

    def _find_entry_execution(
        self,
        entry_type: EntryType,
        entry_low: Decimal | None,
        entry_high: Decimal | None,
        signal_time: datetime,
        candles: list[Candle],
        symbol: str,
    ) -> tuple[Decimal | None, datetime | None]:
        relevant_candles = [c for c in candles if same_market_symbol(c.symbol, symbol)]
        next_candle = next((c for c in relevant_candles if c.open_time >= signal_time), None)
        if next_candle is None:
            return None, None
        if entry_type is EntryType.MARKET:
            return next_candle.open, next_candle.open_time
        if entry_low is not None and entry_high is not None:
            midpoint = (entry_low + entry_high) / Decimal("2")
            for candle in relevant_candles:
                if candle.open_time < signal_time:
                    continue
                if candle.low <= entry_high and candle.high >= entry_low:
                    return midpoint, candle.open_time
            return None, None
        if entry_low is not None:
            for candle in relevant_candles:
                if candle.open_time < signal_time:
                    continue
                if candle.low <= entry_low <= candle.high:
                    return entry_low, candle.open_time
            return None, None
        return next_candle.open, next_candle.open_time

    def _first_candle_open_after(
        self,
        ts: datetime,
        candles: list[Candle],
        symbol: str,
    ) -> Decimal | None:
        candle = next(
            (c for c in candles if same_market_symbol(c.symbol, symbol) and c.open_time >= ts),
            None,
        )
        return candle.open if candle is not None else None

    def _process_candles_until(
        self,
        *,
        open_positions: dict[str, _OpenPosition],
        candles: list[Candle],
        start_index: int,
        stop_at: datetime | None,
        fill_policy: BacktestFillPolicy,
        active_signal_hours: int | None,
        strategy: TradeStrategy | None = None,
        capture_snapshots: bool = False,
        snapshots: list[SimulationSnapshot] | None = None,
        closed_trades_by_signal: dict[str, SimulatedTrade] | None = None,
        initial_balance: Decimal | None = None,
        signal_price_history: dict[str, list[SignalPricePoint]] | None = None,
        stop_loss_history: dict[str, list[PriceLevelSpan]] | None = None,
        take_profit_history: dict[str, list[PriceLevelSpan]] | None = None,
        next_snapshot_at: datetime | None = None,
        snapshot_interval: timedelta | None = None,
    ) -> tuple[int, list[SimulatedTrade]]:
        index = start_index
        resolved_trades: list[SimulatedTrade] = []

        # Normalize symbols to canonical form so "BTC", "BTCUSDT", "BTCUSDT_PERP",
        # etc. all map to the same bucket regardless of how the signal or candle
        # source formatted them.  Positions are keyed by canonical symbol; lookup
        # uses the candle's canonical symbol.
        symbol_to_positions: dict[str, list[str]] = {}
        for signal_id, pos in open_positions.items():
            key = canonical_market_symbol(pos.symbol) or pos.symbol
            symbol_to_positions.setdefault(key, []).append(signal_id)

        while index < len(candles):
            candle = candles[index]
            if stop_at is not None and candle.close_time > stop_at:
                break

            candle_key = canonical_market_symbol(candle.symbol) or candle.symbol
            relevant_signal_ids = symbol_to_positions.get(candle_key, [])
            closed_signal_ids: list[str] = []
            
            for signal_id in relevant_signal_ids:
                if signal_id not in open_positions:
                    continue
                
                position = open_positions[signal_id]
                if candle.open_time < position.entry_time:
                    continue

                if signal_price_history is not None:
                    self._append_signal_price_point(
                        signal_price_history=signal_price_history,
                        signal_id=signal_id,
                        candle=candle,
                        position=position,
                    )
                expiry_status = self._expire_position_if_needed(
                    position=position,
                    candle=candle,
                    active_signal_hours=active_signal_hours,
                )
                if expiry_status is not None:
                    trade = self._finalize_position(position, status=expiry_status)
                    resolved_trades.append(trade)
                    if closed_trades_by_signal is not None:
                        closed_trades_by_signal[trade.signal_id] = trade
                    closed_signal_ids.append(signal_id)
                    continue
                
                status = self._apply_candle_to_position(
                    position,
                    candle,
                    fill_policy,
                    strategy,
                    stop_loss_history,
                )
                if status is None:
                    continue
                
                trade = self._finalize_position(position, status=status)
                resolved_trades.append(trade)
                if closed_trades_by_signal is not None:
                    closed_trades_by_signal[trade.signal_id] = trade
                closed_signal_ids.append(signal_id)

            for signal_id in closed_signal_ids:
                del open_positions[signal_id]
                if signal_id in relevant_signal_ids:
                    relevant_signal_ids.remove(signal_id)

            if (
                capture_snapshots
                and snapshots is not None
                and closed_trades_by_signal is not None
                and initial_balance is not None
                and signal_price_history is not None
                and stop_loss_history is not None
                and take_profit_history is not None
                and next_snapshot_at is not None
                and snapshot_interval is not None
            ):
                while candle.close_time >= next_snapshot_at:
                    snapshots.append(
                        self._build_snapshot(
                            timestamp=next_snapshot_at,
                            source_message_id=None,
                            open_positions=open_positions,
                            closed_trades_by_signal=closed_trades_by_signal,
                            candles=candles,
                            processed_candle_count=index + 1,
                            initial_balance=initial_balance,
                            signal_price_history=signal_price_history,
                            stop_loss_history=stop_loss_history,
                            take_profit_history=take_profit_history,
                            checkpoint_kind="interval",
                        )
                    )
                    next_snapshot_at = next_snapshot_at + snapshot_interval
            
            index += 1
        return index, resolved_trades

    def _expire_position_if_needed(
        self,
        *,
        position: _OpenPosition,
        candle: Candle,
        active_signal_hours: int | None,
    ) -> str | None:
        if active_signal_hours is None or active_signal_hours <= 0:
            return None
        expiry_time = position.entry_time + timedelta(hours=active_signal_hours)
        if candle.open_time < expiry_time:
            return None
        self._close_fraction_of_position(
            position,
            candle.open_time,
            candle.open,
            Decimal("1"),
            "signal_expired",
        )
        return "expired" if position.targets_hit == 0 else "partial_tp_expired"

    def _apply_candle_to_position(
        self,
        position: _OpenPosition,
        candle: Candle,
        fill_policy: BacktestFillPolicy,
        strategy: TradeStrategy | None = None,
        stop_loss_history: dict[str, list[PriceLevelSpan]] | None = None,
    ) -> str | None:
        pending_tps = self._pending_take_profits(position)
        hit_sl = self._hit_sl(position, candle)
        hit_tp_values = [tp for tp in pending_tps if self._hit_tp(position, candle, tp)]

        if hit_sl and hit_tp_values:
            if fill_policy is BacktestFillPolicy.CONSERVATIVE:
                self._close_fraction_of_position(
                    position,
                    candle.close_time,
                    position.stop_loss,
                    Decimal("1"),
                    "sl_hit_same_candle",
                )
                return self._sl_status(position, same_candle=True)
            self._apply_take_profit_hits(
                position,
                candle.close_time,
                hit_tp_values,
                strategy,
                stop_loss_history,
            )
            if position.remaining_quantity > Decimal("0"):
                self._close_fraction_of_position(
                    position,
                    candle.close_time,
                    position.stop_loss,
                    Decimal("1"),
                    "sl_after_partial_tp_same_candle",
                )
                return self._sl_status(position, same_candle=True)
            return self._tp_status(position, same_candle=True)

        if hit_tp_values:
            self._apply_take_profit_hits(
                position,
                candle.close_time,
                hit_tp_values,
                strategy,
                stop_loss_history,
            )
            if position.remaining_quantity <= Decimal("0"):
                return self._tp_status(position, same_candle=False)

        if hit_sl and position.remaining_quantity > Decimal("0"):
            self._close_fraction_of_position(
                position,
                candle.close_time,
                position.stop_loss,
                Decimal("1"),
                "sl_hit",
            )
            return self._sl_status(position, same_candle=False)
        return None

    def _apply_take_profit_hits(
        self,
        position: _OpenPosition,
        exit_time: datetime,
        tp_values: list[Decimal],
        strategy: TradeStrategy | None = None,
        stop_loss_history: dict[str, list[PriceLevelSpan]] | None = None,
    ) -> None:
        for tp in tp_values:
            if position.remaining_quantity <= Decimal("0"):
                return
            remaining_targets = len(self._pending_take_profits(position))

            if strategy is not None:
                action = strategy.get_target_hit_action(
                    targets_hit_so_far=position.targets_hit,
                    remaining_targets_including_this=remaining_targets,
                    entry_price=position.entry_price,
                    take_profits=list(position.take_profits),
                )
                fraction = action.close_fraction
                if action.new_stop_loss is not None and position.stop_loss != action.new_stop_loss:
                    position.stop_loss = action.new_stop_loss
                    position.notes.append(
                        f"stop_loss_moved_to_target_by_strategy={action.new_stop_loss}"
                    )
                    if stop_loss_history is not None:
                        self._replace_stop_loss_history(
                            stop_loss_history=stop_loss_history,
                            signal_id=position.signal_id,
                            timestamp=exit_time,
                            stop_loss=action.new_stop_loss,
                        )
                elif action.move_sl_to_entry and position.stop_loss != position.entry_price:
                    position.stop_loss = position.entry_price
                    position.notes.append("stop_loss_moved_to_entry_by_strategy")
                    if stop_loss_history is not None:
                        self._replace_stop_loss_history(
                            stop_loss_history=stop_loss_history,
                            signal_id=position.signal_id,
                            timestamp=exit_time,
                            stop_loss=position.entry_price,
                        )
            else:
                fraction = (
                    Decimal("1")
                    if remaining_targets <= 1
                    else (Decimal("1") / Decimal(remaining_targets))
                )

            self._close_fraction_of_position(
                position,
                exit_time,
                tp,
                fraction,
                f"take_profit_hit={tp}",
            )
            position.targets_hit += 1

    def _pending_take_profits(self, position: _OpenPosition) -> list[Decimal]:
        pending = position.take_profits[position.targets_hit :]
        return pending if pending else []

    def _close_fraction_of_position(
        self,
        position: _OpenPosition,
        exit_time: datetime,
        exit_price: Decimal,
        fraction: Decimal,
        note: str,
    ) -> None:
        effective_fraction = min(max(fraction, Decimal("0")), Decimal("1"))
        if effective_fraction <= Decimal("0") or position.remaining_quantity <= Decimal("0"):
            return
        quantity = position.remaining_quantity * effective_fraction
        if effective_fraction >= Decimal("1") or quantity > position.remaining_quantity:
            quantity = position.remaining_quantity
        pnl = self._calculate_pnl(position, exit_price, quantity)
        exit_fee = (
            exit_price * quantity * position.fee_rate_pct / Decimal("100")
            if position.fee_rate_pct > Decimal("0")
            else Decimal("0")
        )
        position.realized_pnl += pnl
        position.realized_fees += exit_fee
        position.remaining_quantity -= quantity
        position.exit_time = exit_time
        position.exit_price = exit_price
        position.notes.append(f"{note}; qty={quantity}; px={exit_price}; pnl={pnl}")
        if position.remaining_quantity < Decimal("0"):
            position.remaining_quantity = Decimal("0")

    def _close_remaining_position(
        self,
        position: _OpenPosition,
        exit_time: datetime,
        exit_price: Decimal,
        status: str,
    ) -> SimulatedTrade:
        if position.remaining_quantity > Decimal("0"):
            self._close_fraction_of_position(position, exit_time, exit_price, Decimal("1"), status)
        return self._finalize_position(position, status=status)

    def _tp_status(self, position: _OpenPosition, *, same_candle: bool) -> str:
        if position.manual_partial_exit:
            return "partial_close_then_tp"
        if position.targets_hit > 1:
            return "partial_tp_complete"
        return "tp_hit_same_candle" if same_candle else "tp_hit"

    def _sl_status(self, position: _OpenPosition, *, same_candle: bool) -> str:
        if position.manual_partial_exit:
            return "partial_close_then_sl"
        if position.targets_hit > 0:
            return "partial_tp_then_sl"
        return "sl_hit_same_candle" if same_candle else "sl_hit"

    def _finalize_position(self, position: _OpenPosition, *, status: str) -> SimulatedTrade:
        position.status = status
        exposure = position.entry_price * position.original_quantity
        # PnL percentage is return on committed margin (= notional / leverage),
        # so a leveraged trade shows the amplified percentage return while the
        # absolute dollar PnL is leverage-independent.
        leverage = position.effective_leverage if position.effective_leverage > 0 else Decimal("1")
        margin = exposure / leverage if exposure > Decimal("0") else Decimal("0")
        # Net PnL is gross realized PnL minus all trading fees (entry + exits).
        # Netting here — the single finalize chokepoint that every balance
        # increment flows through — keeps balance, total_pnl, scoring and
        # snapshots consistent (sum(trade.pnl) == total_pnl). With fee_rate=0
        # realized_fees is 0, so this is a no-op for the default configuration.
        fees = max(position.realized_fees, Decimal("0"))
        net_pnl = position.realized_pnl - fees
        pnl_pct = (
            (net_pnl / margin) * Decimal("100")
            if margin > Decimal("0")
            else Decimal("0")
        )
        return SimulatedTrade(
            trade_id=position.trade_id,
            signal_id=position.signal_id,
            channel_id=position.channel_id,
            symbol=position.symbol,
            side=position.side,
            entry_time=position.entry_time,
            exit_time=position.exit_time,
            entry_price=position.entry_price,
            exit_price=position.exit_price,
            quantity=max(position.original_quantity, Decimal("0")),
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            status=status,
            notes=list(position.notes),
        )

    def _build_snapshot(
        self,
        *,
        timestamp: datetime,
        source_message_id: int | None,
        open_positions: dict[str, _OpenPosition],
        closed_trades_by_signal: dict[str, SimulatedTrade],
        candles: list[Candle],
        processed_candle_count: int,
        initial_balance: Decimal,
        signal_price_history: dict[str, list[SignalPricePoint]],
        stop_loss_history: dict[str, list[PriceLevelSpan]],
        take_profit_history: dict[str, list[PriceLevelSpan]],
        checkpoint_kind: str,
    ) -> SimulationSnapshot:
        realized_pnl = sum(
            (trade.pnl for trade in closed_trades_by_signal.values()),
            Decimal("0"),
        )
        signal_states: dict[str, SimulationSignalState] = {}
        relevant_candles = candles[:processed_candle_count]
        unrealized_pnl = Decimal("0")

        for signal_id, trade in closed_trades_by_signal.items():
            mark_price = trade.exit_price or trade.entry_price or Decimal("0")
            signal_states[signal_id] = SimulationSignalState(
                signal_id=signal_id,
                symbol=trade.symbol,
                side=trade.side,
                status=trade.status,
                original_quantity=trade.quantity,
                open_quantity=Decimal("0"),
                entry_price=trade.entry_price,
                stop_loss=None,
                take_profits=[],
                notional_value=(trade.entry_price or Decimal("0")) * trade.quantity,
                risk_amount=Decimal("0"),
                realized_pnl=trade.pnl,
                unrealized_pnl=Decimal("0"),
                total_pnl_pct=trade.pnl_pct,
                mark_price=mark_price,
                entry_time=trade.entry_time or timestamp,
                exit_time=trade.exit_time,
                exit_price=trade.exit_price,
                targets_hit=0,
                notes=list(trade.notes),
                price_history=list(signal_price_history.get(signal_id, [])),
                stop_loss_history=list(stop_loss_history.get(signal_id, [])),
                take_profit_history=list(take_profit_history.get(signal_id, [])),
            )

        for signal_id, position in open_positions.items():
            mark_price = self._last_price(relevant_candles, position.symbol, position.entry_price)
            unrealized = self._calculate_pnl(position, mark_price, position.remaining_quantity)
            unrealized_pnl += unrealized
            notional_value = position.entry_price * position.original_quantity
            # Net the realized component by fees accrued so far (entry + any
            # partial exits) so live snapshots match the finalized net PnL.
            net_realized = position.realized_pnl - max(position.realized_fees, Decimal("0"))
            total_pnl = net_realized + unrealized
            leverage = (
                position.effective_leverage
                if position.effective_leverage > 0
                else Decimal("1")
            )
            margin = notional_value / leverage if notional_value > Decimal("0") else Decimal("0")
            signal_states[signal_id] = SimulationSignalState(
                signal_id=signal_id,
                symbol=position.symbol,
                side=position.side,
                status="open",
                original_quantity=position.original_quantity,
                open_quantity=position.remaining_quantity,
                entry_price=position.entry_price,
                stop_loss=position.stop_loss,
                take_profits=list(position.take_profits),
                notional_value=notional_value,
                risk_amount=(
                    abs(position.entry_price - position.stop_loss)
                    * position.original_quantity
                ),
                realized_pnl=net_realized,
                unrealized_pnl=unrealized,
                total_pnl_pct=(
                    (total_pnl / margin) * Decimal("100")
                    if margin > Decimal("0")
                    else Decimal("0")
                ),
                mark_price=mark_price,
                entry_time=position.entry_time,
                exit_time=position.exit_time,
                exit_price=position.exit_price,
                targets_hit=position.targets_hit,
                notes=list(position.notes),
                effective_leverage=leverage,
                margin=margin,
                price_history=list(signal_price_history.get(signal_id, [])),
                stop_loss_history=list(stop_loss_history.get(signal_id, [])),
                take_profit_history=list(take_profit_history.get(signal_id, [])),
            )

        wins = sum(1 for trade in closed_trades_by_signal.values() if trade.pnl > 0)
        losses = sum(1 for trade in closed_trades_by_signal.values() if trade.pnl < 0)
        realized_balance = initial_balance + realized_pnl
        current_balance = realized_balance + unrealized_pnl
        return SimulationSnapshot(
            timestamp=timestamp,
            source_message_id=source_message_id,
            open_positions=len(open_positions),
            closed_trades=len(closed_trades_by_signal),
            wins=wins,
            losses=losses,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=realized_pnl + unrealized_pnl,
            realized_balance=realized_balance,
            current_balance=current_balance,
            signal_states=signal_states,
            checkpoint_kind=checkpoint_kind,
        )

    def _append_signal_price_point(
        self,
        *,
        signal_price_history: dict[str, list[SignalPricePoint]],
        signal_id: str,
        candle: Candle,
        position: _OpenPosition,
    ) -> None:
        history = signal_price_history.setdefault(signal_id, [])
        if history and history[-1].candle_open_time == candle.open_time:
            return
        history.append(
            SignalPricePoint(
                timestamp=candle.close_time,
                candle_open_time=candle.open_time,
                candle_close_time=candle.close_time,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                stop_loss=position.stop_loss,
                take_profits=list(position.take_profits),
                mark_price=candle.close,
            )
        )

    def _replace_stop_loss_history(
        self,
        *,
        stop_loss_history: dict[str, list[PriceLevelSpan]],
        signal_id: str | None,
        timestamp: datetime,
        stop_loss: Decimal | None,
    ) -> None:
        if signal_id is None or stop_loss is None:
            return
        history = stop_loss_history.setdefault(signal_id, [])
        if history:
            last = history[-1]
            history[-1] = PriceLevelSpan(
                kind=last.kind,
                label=last.label,
                value=last.value,
                started_at=last.started_at,
                ended_at=timestamp,
            )
        history.append(
            PriceLevelSpan(
                kind="stop_loss",
                label="SL",
                value=stop_loss,
                started_at=timestamp,
            )
        )

    def _replace_take_profit_history(
        self,
        *,
        take_profit_history: dict[str, list[PriceLevelSpan]],
        signal_id: str | None,
        timestamp: datetime,
        take_profits: list[Decimal],
    ) -> None:
        if signal_id is None:
            return
        existing = take_profit_history.setdefault(signal_id, [])
        updated: list[PriceLevelSpan] = []
        for span in existing:
            if span.ended_at is None:
                updated.append(
                    PriceLevelSpan(
                        kind=span.kind,
                        label=span.label,
                        value=span.value,
                        started_at=span.started_at,
                        ended_at=timestamp,
                    )
                )
            else:
                updated.append(span)
        take_profit_history[signal_id] = updated
        for index, target in enumerate(take_profits, start=1):
            take_profit_history[signal_id].append(
                PriceLevelSpan(
                    kind="take_profit",
                    label=f"TP{index}",
                    value=target,
                    started_at=timestamp,
                )
            )

    def _set_signal_level_history(
        self,
        *,
        stop_loss_history: dict[str, list[PriceLevelSpan]],
        take_profit_history: dict[str, list[PriceLevelSpan]],
        signal_id: str,
        timestamp: datetime,
        stop_loss: Decimal | None,
        take_profits: list[Decimal],
    ) -> None:
        if stop_loss is not None:
            self._replace_stop_loss_history(
                stop_loss_history=stop_loss_history,
                signal_id=signal_id,
                timestamp=timestamp,
                stop_loss=stop_loss,
            )
        self._replace_take_profit_history(
            take_profit_history=take_profit_history,
            signal_id=signal_id,
            timestamp=timestamp,
            take_profits=take_profits,
        )

    def _initial_snapshot_anchor(
        self,
        *,
        candles: list[Candle],
        events: list[BacktestEvent],
        snapshot_interval: timedelta | None,
    ) -> datetime | None:
        if snapshot_interval is None:
            return None
        anchors = [item.open_time for item in candles]
        anchors.extend(event.timestamp for event in events)
        if not anchors:
            return None
        first = min(anchors)
        seconds = int(snapshot_interval.total_seconds())
        epoch = int(first.timestamp())
        aligned = ((epoch // seconds) + 1) * seconds
        return datetime.fromtimestamp(aligned, tz=first.tzinfo)

    def _advance_snapshot_anchor(
        self,
        next_snapshot_at: datetime | None,
        snapshots: list[SimulationSnapshot],
        snapshot_interval: timedelta,
    ) -> datetime | None:
        if next_snapshot_at is None:
            return None
        if not snapshots:
            return next_snapshot_at
        current = next_snapshot_at
        last_interval_snapshot = next(
            (item for item in reversed(snapshots) if item.checkpoint_kind == "interval"),
            None,
        )
        while last_interval_snapshot is not None and current <= last_interval_snapshot.timestamp:
            current = current + snapshot_interval
        return current

    def _last_price(self, candles: list[Candle], symbol: str, fallback: Decimal) -> Decimal:
        relevant = [c for c in candles if same_market_symbol(c.symbol, symbol)]
        if not relevant:
            return fallback
        return relevant[-1].close

    def _hit_sl(self, position: _OpenPosition, candle: Candle) -> bool:
        if position.side.is_short:
            return candle.high >= position.stop_loss
        return candle.low <= position.stop_loss

    def _hit_tp(self, position: _OpenPosition, candle: Candle, tp: Decimal | None) -> bool:
        if tp is None:
            return False
        if position.side.is_short:
            return candle.low <= tp
        return candle.high >= tp

    def _calculate_pnl(
        self,
        position: _OpenPosition,
        exit_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        direction = Decimal("-1") if position.side.is_short else Decimal("1")
        return (exit_price - position.entry_price) * quantity * direction
