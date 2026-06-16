"""Event-driven trade simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.core.symbols import same_market_symbol
from triak_trade.domain.enums import BacktestFillPolicy, EntryType, SignalAction, TradeSide
from triak_trade.domain.models import Candle, SimulatedTrade


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
    ) -> tuple[list[SimulatedTrade], Decimal, list[SimulationSnapshot]]:
        trades: list[SimulatedTrade] = []
        closed_trades_by_signal: dict[str, SimulatedTrade] = {}
        snapshots: list[SimulationSnapshot] = []
        open_positions: dict[str, _OpenPosition] = {}
        balance = initial_balance
        sorted_events = sorted(events, key=lambda item: item.timestamp)
        sorted_candles = sorted(candles, key=lambda item: item.open_time)
        candle_index = 0

        for event in sorted_events:
            candle_index, resolved_trades = self._process_candles_until(
                open_positions=open_positions,
                candles=sorted_candles,
                start_index=candle_index,
                stop_at=event.timestamp,
                fill_policy=fill_policy,
                active_signal_hours=active_signal_hours,
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
                            )
                        )
                    continue

                notes: list[str] = []
                # The channel may post a signal with no explicit stop_loss. We must
                # still open and track it, so synthesize a stop a fixed percentage
                # from entry (by side) purely for risk-per-trade sizing. A real
                # stop_loss, when present, always takes precedence.
                if parsed.stop_loss is not None:
                    effective_stop = parsed.stop_loss
                else:
                    pct = max(default_stop_pct, Decimal("0")) / Decimal("100")
                    if parsed.side is TradeSide.SHORT:
                        effective_stop = entry_price * (Decimal("1") + pct)
                    else:
                        effective_stop = entry_price * (Decimal("1") - pct)
                    notes.append(f"synthetic_stop_pct={default_stop_pct}")
                risk_amount = (balance * risk_per_trade_pct) / Decimal("100")
                stop_distance = abs(entry_price - effective_stop)
                if stop_distance <= Decimal("0"):
                    continue
                qty = risk_amount / stop_distance
                # Leverage caps how much notional the balance can support. Keep
                # the risk-based quantity as primary sizing, but never let a
                # position exceed balance * effective_leverage in notional.
                signal_leverage = Decimal(event.leverage) if event.leverage else Decimal("1")
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
                open_positions[event.signal_id or f"sig_{len(open_positions)+1}"] = _OpenPosition(
                    trade_id=f"trade_{event.signal_id or len(open_positions)+1}",
                    signal_id=event.signal_id or "unknown",
                    channel_id=parsed.source_channel_id,
                    symbol=parsed.symbol,
                    side=parsed.side,
                    entry_price=entry_price,
                    stop_loss=effective_stop,
                    take_profits=parsed.take_profits,
                    original_quantity=qty,
                    remaining_quantity=qty,
                    entry_time=entry_time,
                    realized_pnl=Decimal("0"),
                    realized_fees=Decimal("0"),
                    exit_price=None,
                    exit_time=None,
                    status="open",
                    notes=notes,
                    targets_hit=0,
                    manual_partial_exit=False,
                    effective_leverage=effective_leverage,
                )
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
                elif parsed.action is SignalAction.UPDATE_SL and parsed.stop_loss is not None:
                    position.stop_loss = parsed.stop_loss
                    position.notes.append(f"stop_loss_updated={parsed.stop_loss}")
                elif parsed.action is SignalAction.UPDATE_TP and parsed.take_profits:
                    position.take_profits = (
                        position.take_profits[: position.targets_hit] + parsed.take_profits
                    )
                    position.notes.append(
                        "take_profits_updated="
                        + ",".join(str(item) for item in parsed.take_profits)
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
                    )
                )

        candle_index, resolved_trades = self._process_candles_until(
            open_positions=open_positions,
            candles=sorted_candles,
            start_index=candle_index,
            stop_at=None,
            fill_policy=fill_policy,
            active_signal_hours=active_signal_hours,
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
            if not self._market_entry_candle_is_aligned(signal_time, next_candle):
                return None, None
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

    @staticmethod
    def _market_entry_candle_is_aligned(signal_time: datetime, candle: Candle) -> bool:
        candle_duration = candle.close_time - candle.open_time
        if candle_duration <= timedelta(0):
            candle_duration = timedelta(minutes=1)
        max_delay = max(candle_duration * 2, timedelta(minutes=2))
        return candle.open_time - signal_time <= max_delay

    def _first_candle_open_after(
        self,
        ts: datetime,
        candles: list[Candle],
        symbol: str,
    ) -> Decimal | None:
        candle = next((c for c in candles if c.symbol == symbol and c.open_time >= ts), None)
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
    ) -> tuple[int, list[SimulatedTrade]]:
        index = start_index
        resolved_trades: list[SimulatedTrade] = []

        symbol_to_positions: dict[str, list[str]] = {}
        for signal_id, pos in open_positions.items():
            symbol_to_positions.setdefault(pos.symbol, []).append(signal_id)

        while index < len(candles):
            candle = candles[index]
            if stop_at is not None and candle.close_time > stop_at:
                break
            
            relevant_signal_ids = symbol_to_positions.get(candle.symbol, [])
            closed_signal_ids: list[str] = []
            
            for signal_id in relevant_signal_ids:
                if signal_id not in open_positions:
                    continue
                
                position = open_positions[signal_id]
                if candle.open_time < position.entry_time:
                    continue

                expiry_status = self._expire_position_if_needed(
                    position=position,
                    candle=candle,
                    active_signal_hours=active_signal_hours,
                )
                if expiry_status is not None:
                    resolved_trades.append(self._finalize_position(position, status=expiry_status))
                    closed_signal_ids.append(signal_id)
                    continue
                
                status = self._apply_candle_to_position(position, candle, fill_policy)
                if status is None:
                    continue
                
                resolved_trades.append(self._finalize_position(position, status=status))
                closed_signal_ids.append(signal_id)

            for signal_id in closed_signal_ids:
                del open_positions[signal_id]
                if signal_id in relevant_signal_ids:
                    relevant_signal_ids.remove(signal_id)
            
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
            self._apply_take_profit_hits(position, candle.close_time, hit_tp_values)
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
            self._apply_take_profit_hits(position, candle.close_time, hit_tp_values)
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
    ) -> None:
        for tp in tp_values:
            if position.remaining_quantity <= Decimal("0"):
                return
            remaining_targets = len(self._pending_take_profits(position))
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
        position.realized_pnl += pnl
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
        pnl_pct = (
            (position.realized_pnl / margin) * Decimal("100")
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
            quantity=position.original_quantity,
            pnl=position.realized_pnl,
            pnl_pct=pnl_pct,
            fees=position.realized_fees,
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
            )

        for signal_id, position in open_positions.items():
            mark_price = self._last_price(relevant_candles, position.symbol, position.entry_price)
            unrealized = self._calculate_pnl(position, mark_price, position.remaining_quantity)
            unrealized_pnl += unrealized
            notional_value = position.entry_price * position.original_quantity
            total_pnl = position.realized_pnl + unrealized
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
                realized_pnl=position.realized_pnl,
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
        )

    def _last_price(self, candles: list[Candle], symbol: str, fallback: Decimal) -> Decimal:
        relevant = [c for c in candles if same_market_symbol(c.symbol, symbol)]
        if not relevant:
            return fallback
        return relevant[-1].close

    def _hit_sl(self, position: _OpenPosition, candle: Candle) -> bool:
        if position.side is TradeSide.SHORT:
            return candle.high >= position.stop_loss
        return candle.low <= position.stop_loss

    def _hit_tp(self, position: _OpenPosition, candle: Candle, tp: Decimal | None) -> bool:
        if tp is None:
            return False
        if position.side is TradeSide.SHORT:
            return candle.low <= tp
        return candle.high >= tp

    def _calculate_pnl(
        self,
        position: _OpenPosition,
        exit_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        direction = Decimal("-1") if position.side is TradeSide.SHORT else Decimal("1")
        return (exit_price - position.entry_price) * quantity * direction
