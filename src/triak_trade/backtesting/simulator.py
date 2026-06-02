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
    quantity: Decimal
    entry_time: datetime


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

        for event in events:
            parsed = event.parsed_signal
            if (
                parsed.action is SignalAction.OPEN
                and parsed.symbol
                and parsed.stop_loss is not None
            ):
                entry_price = self._find_entry_price(
                    parsed.entry_type,
                    parsed.entry_low,
                    parsed.entry_high,
                    event.timestamp,
                    candles,
                )
                if entry_price is None:
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
                    quantity=qty,
                    entry_time=event.timestamp,
                )
            elif event.related_signal_id in open_positions:
                position = open_positions[event.related_signal_id]
                if parsed.action is SignalAction.CANCEL:
                    trades.append(
                        self._close_trade(
                            position,
                            event.timestamp,
                            position.entry_price,
                            "cancelled",
                        )
                    )
                    del open_positions[event.related_signal_id]
                elif parsed.action is SignalAction.CLOSE:
                    close_price = (
                        self._first_candle_open_after(
                            event.timestamp,
                            candles,
                            position.symbol,
                        )
                        or position.entry_price
                    )
                    trade = self._close_trade(position, event.timestamp, close_price, "closed")
                    trades.append(trade)
                    balance += trade.pnl
                    del open_positions[event.related_signal_id]
                elif parsed.action is SignalAction.UPDATE_SL and parsed.stop_loss is not None:
                    position.stop_loss = parsed.stop_loss
                elif parsed.action is SignalAction.UPDATE_TP and parsed.take_profits:
                    position.take_profits = parsed.take_profits

        for signal_id, position in list(open_positions.items()):
            outcome = self._resolve_with_candles(position, candles, fill_policy)
            trades.append(outcome)
            balance += outcome.pnl
            del open_positions[signal_id]

        return trades, balance

    def _find_entry_price(
        self,
        entry_type: EntryType,
        entry_low: Decimal | None,
        entry_high: Decimal | None,
        signal_time: datetime,
        candles: list[Candle],
    ) -> Decimal | None:
        next_candle = next((c for c in candles if c.open_time >= signal_time), None)
        if next_candle is None:
            return None
        if entry_type is EntryType.MARKET:
            return next_candle.open
        if entry_low is not None and entry_high is not None:
            midpoint = (entry_low + entry_high) / Decimal("2")
            for candle in candles:
                if candle.open_time < signal_time:
                    continue
                if candle.low <= entry_high and candle.high >= entry_low:
                    return midpoint
            return None
        if entry_low is not None:
            for candle in candles:
                if candle.open_time < signal_time:
                    continue
                if candle.low <= entry_low <= candle.high:
                    return entry_low
            return None
        return next_candle.open

    def _first_candle_open_after(
        self,
        ts: datetime,
        candles: list[Candle],
        symbol: str,
    ) -> Decimal | None:
        candle = next((c for c in candles if c.symbol == symbol and c.open_time >= ts), None)
        return candle.open if candle is not None else None

    def _resolve_with_candles(
        self,
        position: _OpenPosition,
        candles: list[Candle],
        fill_policy: BacktestFillPolicy,
    ) -> SimulatedTrade:
        relevant = [
            c for c in candles if c.symbol == position.symbol and c.open_time >= position.entry_time
        ]
        tp = position.take_profits[0] if position.take_profits else None
        for candle in relevant:
            hit_sl = self._hit_sl(position, candle)
            hit_tp = self._hit_tp(position, candle, tp)
            if hit_sl and hit_tp:
                if fill_policy is BacktestFillPolicy.CONSERVATIVE:
                    return self._close_trade(
                        position,
                        candle.close_time,
                        position.stop_loss,
                        "sl_hit_same_candle",
                    )
                if tp is not None:
                    return self._close_trade(position, candle.close_time, tp, "tp_hit_same_candle")
            elif hit_sl:
                return self._close_trade(position, candle.close_time, position.stop_loss, "sl_hit")
            elif hit_tp and tp is not None:
                return self._close_trade(position, candle.close_time, tp, "tp_hit")

        last_price = relevant[-1].close if relevant else position.entry_price
        return self._close_trade(position, position.entry_time, last_price, "open_until_end")

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

    def _close_trade(
        self,
        position: _OpenPosition,
        exit_time: datetime,
        exit_price: Decimal,
        status: str,
    ) -> SimulatedTrade:
        direction = Decimal("-1") if position.side is TradeSide.SHORT else Decimal("1")
        pnl = (exit_price - position.entry_price) * position.quantity * direction
        pnl_pct = (pnl / (position.entry_price * position.quantity)) * Decimal("100")
        return SimulatedTrade(
            trade_id=position.trade_id,
            signal_id=position.signal_id,
            channel_id=position.channel_id,
            symbol=position.symbol,
            side=position.side,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=Decimal("0"),
            status=status,
            notes=[],
        )
