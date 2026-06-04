"""Event-driven trade simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
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


class BacktestSimulator:
    def simulate(
        self,
        *,
        events: list[BacktestEvent],
        candles: list[Candle],
        initial_balance: Decimal,
        risk_per_trade_pct: Decimal,
        fill_policy: BacktestFillPolicy,
    ) -> tuple[list[SimulatedTrade], Decimal]:
        trades: list[SimulatedTrade] = []
        open_positions: dict[str, _OpenPosition] = {}
        balance = initial_balance
        sorted_events = sorted(events, key=lambda item: item.timestamp)
        sorted_candles = sorted(candles, key=lambda item: item.open_time)
        candle_index = 0

        for event in sorted_events:
            candle_index = self._process_candles_until(
                open_positions=open_positions,
                candles=sorted_candles,
                start_index=candle_index,
                stop_at=event.timestamp,
                fill_policy=fill_policy,
                trades=trades,
            )
            parsed = event.parsed_signal
            if (
                parsed.action is SignalAction.OPEN
                and parsed.symbol
                and parsed.stop_loss is not None
            ):
                entry_price, entry_time = self._find_entry_execution(
                    parsed.entry_type,
                    parsed.entry_low,
                    parsed.entry_high,
                    event.timestamp,
                    sorted_candles,
                    parsed.symbol,
                )
                if entry_price is None or entry_time is None:
                    trades.append(
                        SimulatedTrade(
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
                    )
                    continue

                risk_amount = (balance * risk_per_trade_pct) / Decimal("100")
                stop_distance = abs(entry_price - parsed.stop_loss)
                if stop_distance <= Decimal("0"):
                    continue
                qty = risk_amount / stop_distance
                open_positions[event.signal_id or f"sig_{len(open_positions)+1}"] = _OpenPosition(
                    trade_id=f"trade_{event.signal_id or len(open_positions)+1}",
                    signal_id=event.signal_id or "unknown",
                    channel_id=parsed.source_channel_id,
                    symbol=parsed.symbol,
                    side=parsed.side,
                    entry_price=entry_price,
                    stop_loss=parsed.stop_loss,
                    take_profits=parsed.take_profits,
                    original_quantity=qty,
                    remaining_quantity=qty,
                    entry_time=entry_time,
                    realized_pnl=Decimal("0"),
                    realized_fees=Decimal("0"),
                    exit_price=None,
                    exit_time=None,
                    status="open",
                    notes=[],
                    targets_hit=0,
                    manual_partial_exit=False,
                )
            elif event.related_signal_id in open_positions:
                position = open_positions[event.related_signal_id]
                if parsed.action is SignalAction.CANCEL:
                    trades.append(
                        self._close_remaining_position(
                            position,
                            event.timestamp,
                            position.entry_price,
                            "cancelled" if position.targets_hit == 0 else "partial_tp_then_cancel",
                        )
                    )
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

        candle_index = self._process_candles_until(
            open_positions=open_positions,
            candles=sorted_candles,
            start_index=candle_index,
            stop_at=None,
            fill_policy=fill_policy,
            trades=trades,
        )

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
            balance += outcome.pnl
            del open_positions[signal_id]

        return trades, balance

    def _find_entry_execution(
        self,
        entry_type: EntryType,
        entry_low: Decimal | None,
        entry_high: Decimal | None,
        signal_time: datetime,
        candles: list[Candle],
        symbol: str,
    ) -> tuple[Decimal | None, datetime | None]:
        relevant_candles = [c for c in candles if c.symbol == symbol]
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
        trades: list[SimulatedTrade],
    ) -> int:
        index = start_index
        while index < len(candles):
            candle = candles[index]
            if stop_at is not None and candle.close_time > stop_at:
                break
            closed_signal_ids: list[str] = []
            for signal_id, position in list(open_positions.items()):
                if position.symbol != candle.symbol or candle.open_time < position.entry_time:
                    continue
                status = self._apply_candle_to_position(position, candle, fill_policy)
                if status is None:
                    continue
                trades.append(self._finalize_position(position, status=status))
                closed_signal_ids.append(signal_id)
            for signal_id in closed_signal_ids:
                del open_positions[signal_id]
            index += 1
        return index

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
        pnl_pct = (
            (position.realized_pnl / exposure) * Decimal("100")
            if exposure > Decimal("0")
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

    def _last_price(self, candles: list[Candle], symbol: str, fallback: Decimal) -> Decimal:
        relevant = [c for c in candles if c.symbol == symbol]
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
