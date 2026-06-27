"""Position sizing, P&L, and SL/TP management for live/demo trading."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import TradeSide
from triak_trade.domain.models import ParsedSignal
from triak_trade.live_trading.models import LiveSession, LiveTrade, MessageAttribution


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PositionSizingResult:
    def __init__(
        self,
        *,
        quantity: Decimal,
        allocation_pct: Decimal,
        margin: Decimal,
        leverage: int,
        entry_price: Decimal,
        stop_loss: Decimal | None,
        take_profits: list[Decimal],
        is_synthetic_stop: bool,
        notes: list[str],
    ) -> None:
        self.quantity = quantity
        self.allocation_pct = allocation_pct
        self.margin = margin
        self.leverage = leverage
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profits = take_profits
        self.is_synthetic_stop = is_synthetic_stop
        self.notes = notes


class LivePositionManager:
    """Handles position sizing, trade creation, SL/TP updates, and P&L calculation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def compute_position_sizing(
        self,
        *,
        session: LiveSession,
        signal: ParsedSignal,
        current_balance: Decimal,
        strategy: TradeStrategy,
    ) -> PositionSizingResult:
        notes: list[str] = []
        side = signal.side
        if current_balance <= Decimal("0"):
            raise ValueError("Current account balance is zero; cannot size position")

        leverage_raw = signal.leverage or self.settings.LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE
        leverage = min(
            max(leverage_raw, 1),
            self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE,
        )
        if leverage_raw != leverage:
            notes.append(
                f"leverage clamped {leverage_raw}x -> {leverage}x "
                f"(max={self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE})"
            )

        entry_price = _resolve_entry_price(signal)
        if entry_price is None or entry_price <= 0:
            raise ValueError("Cannot determine entry price for position sizing")

        allocation_pct = _allocation_pct_for_signal(
            allocation_factor_pct=session.risk_per_trade_pct,
            leverage=Decimal(str(leverage)),
            min_allocation_pct=self.settings.LIVE_TRADING_MIN_ALLOCATION_PCT,
            max_allocation_pct=self.settings.LIVE_TRADING_MAX_ALLOCATION_PCT,
        )
        notes.append(f"allocation_pct={allocation_pct}")
        allocation_amount = current_balance * allocation_pct / Decimal("100")
        quantity = (allocation_amount * Decimal(str(leverage)) / entry_price).quantize(
            Decimal("0.00000001")
        )
        if quantity <= 0:
            raise ValueError("Computed quantity is zero or negative")

        stop_loss = signal.stop_loss
        is_synthetic_stop = False
        if stop_loss is None:
            if strategy is not None:
                stop_loss = strategy.get_synthetic_stop(
                    side=side,
                    entry_price=entry_price,
                    balance_at_entry=current_balance,
                    quantity=quantity,
                    fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
                )
                notes.append(f"synthetic_stop_strategy={getattr(strategy, 'name', 'unknown')}")
            else:
                stop_loss = _synthetic_stop(
                    side=side,
                    entry_price=entry_price,
                    stop_pct=self.settings.LIVE_TRADING_DEFAULT_STOP_PCT,
                )
                notes.append(f"synthetic_stop_pct={self.settings.LIVE_TRADING_DEFAULT_STOP_PCT}")
                stop_loss, quantity, synthetic_stop_notes = _cap_synthetic_stop_loss_risk(
                    side=side,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    quantity=quantity,
                    balance_at_entry=current_balance,
                    fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
                    max_loss_pct_of_balance=self.settings.LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT,
                )
                notes.extend(synthetic_stop_notes)
            is_synthetic_stop = True
        if quantity <= 0:
            raise ValueError("Quantity became zero after synthetic stop risk capping")

        take_profits = _sanitize_take_profits(
            take_profits=list(signal.take_profits),
            side="long" if side.is_long else "short",
            entry_price=entry_price,
            stop_loss=stop_loss,
        )
        if len(take_profits) < len(signal.take_profits):
            notes.append(f"tp_direction_filtered={len(signal.take_profits) - len(take_profits)}")
        if not take_profits and strategy is not None and stop_loss is not None:
            strategy_tps = strategy.get_synthetic_take_profits(
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
                notional_value=entry_price * quantity,
            )
            take_profits = _sanitize_take_profits(
                take_profits=strategy_tps,
                side="long" if side.is_long else "short",
                entry_price=entry_price,
                stop_loss=stop_loss,
            )
            if take_profits:
                notes.append(
                    "synthetic_take_profits_strategy="
                    + ",".join(str(item) for item in take_profits)
                )

        margin = (entry_price * quantity / Decimal(str(leverage))).quantize(Decimal("0.00000001"))

        return PositionSizingResult(
            quantity=quantity,
            allocation_pct=allocation_pct,
            margin=margin,
            leverage=leverage,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profits=take_profits,
            is_synthetic_stop=is_synthetic_stop,
            notes=notes,
        )

    def create_trade(
        self,
        *,
        session: LiveSession,
        signal: ParsedSignal,
        sizing: PositionSizingResult,
        trigger_message_id: int,
        trigger_message_preview: str,
        trigger_message_date: datetime,
        channel_id: str,
        channel_input: str,
        channel_label: str,
        signal_id: str,
    ) -> LiveTrade:
        trade_id = f"lt_{uuid.uuid4().hex[:12]}"
        attribution = MessageAttribution(
            message_id=trigger_message_id,
            channel_id=channel_id,
            channel_label=channel_label,
            message_preview=trigger_message_preview[:200],
            message_date=trigger_message_date,
            action="opened",
            notes=sizing.notes,
        )
        side = signal.side
        trade = LiveTrade(
            trade_id=trade_id,
            session_id=session.session_id,
            signal_id=signal_id,
            channel_id=channel_id,
            channel_input=channel_input,
            channel_label=channel_label,
            symbol=signal.symbol or "",
            side="long" if side.is_long else "short",
            leverage=sizing.leverage,
            entry_price=sizing.entry_price,
            quantity=sizing.quantity,
            remaining_quantity=sizing.quantity,
            stop_loss=sizing.stop_loss,
            take_profits=sizing.take_profits,
            margin=sizing.margin,
            balance_at_entry=(
                session.paper_balance
                if session.trading_mode == "demo"
                else Decimal("0")
            ),
            status="open",
            message_history=[attribution],
        )
        return trade

    def update_stop_loss(
        self,
        *,
        trade: LiveTrade,
        new_sl: Decimal | None,
        message: MessageAttribution,
        move_to_entry: bool = False,
    ) -> None:
        if move_to_entry:
            trade.stop_loss = trade.entry_price
            message.notes.append(f"SL moved to entry (breakeven) {trade.entry_price}")
        elif new_sl is not None:
            trade.stop_loss = new_sl
            message.notes.append(f"SL updated to {new_sl}")
        trade.add_attribution(message)

    def update_take_profits(
        self,
        *,
        trade: LiveTrade,
        new_tps: list[Decimal],
        message: MessageAttribution,
    ) -> None:
        sanitized = _sanitize_take_profits(
            take_profits=new_tps,
            side=trade.side,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
        )
        # Preserve already-hit targets, append new pending ones (matches backtest)
        trade.take_profits = trade.take_profits[: trade.targets_hit] + sanitized
        message.notes.append(f"TPs updated: {[str(t) for t in sanitized]}")
        trade.add_attribution(message)

    def update_leverage(
        self,
        *,
        trade: LiveTrade,
        new_leverage: int | None,
        message: MessageAttribution,
    ) -> bool:
        if new_leverage is None or new_leverage <= 0:
            return False
        clamped = min(new_leverage, self.settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE)
        trade.leverage = clamped
        message.action = "set_leverage"
        if clamped != new_leverage:
            message.notes.append(
                f"leverage clamped {new_leverage}x -> {clamped}x"
            )
        else:
            message.notes.append(f"leverage updated to {clamped}x")
        trade.add_attribution(message)
        return True

    def apply_mark_price(
        self,
        *,
        trade: LiveTrade,
        mark_price: Decimal,
        fee_rate_pct: Decimal,
    ) -> None:
        trade.mark_price = mark_price
        pnl = _calculate_unrealized_pnl(
            side=trade.side,
            entry_price=trade.entry_price,
            mark_price=mark_price,
            quantity=trade.remaining_quantity,
            fee_rate_pct=fee_rate_pct,
        )
        trade.unrealized_pnl = pnl

    def check_sl_tp_hit(
        self,
        *,
        trade: LiveTrade,
        mark_price: Decimal,
        strategy: TradeStrategy,
        fee_rate_pct: Decimal,
    ) -> list[str]:
        """Returns list of triggered events: ['sl_hit', 'tp1_hit', ...].
        Caller is responsible for applying the close/partial-close.
        """
        events: list[str] = []
        if not trade.is_open or trade.remaining_quantity <= 0:
            return events

        side = trade.side
        is_long = side == "long"

        # Check take-profits in order
        hit_tp_idx = None
        for idx, tp in enumerate(trade.take_profits[trade.targets_hit :], start=trade.targets_hit):
            if (is_long and mark_price >= tp) or (not is_long and mark_price <= tp):
                hit_tp_idx = idx
                break

        if hit_tp_idx is not None:
            events.append(f"tp{hit_tp_idx + 1}_hit")

        # Check stop-loss
        if trade.stop_loss is not None:
            if (is_long and mark_price <= trade.stop_loss) or (
                not is_long and mark_price >= trade.stop_loss
            ):
                events.append("sl_hit")

        return events

    def apply_partial_close(
        self,
        *,
        trade: LiveTrade,
        close_fraction: Decimal,
        close_price: Decimal,
        reason: str,
        fee_rate_pct: Decimal,
        message: MessageAttribution | None = None,
        is_tp_hit: bool = False,
    ) -> Decimal:
        """Close a fraction of the trade. Returns realized PnL for this partial close.

        ``is_tp_hit`` must be True when triggered by a take-profit level; only then
        does ``targets_hit`` advance. Manual closes must pass False (the default) so
        the TP tracking index is not skewed — matching backtest behavior.
        """
        close_qty = (trade.remaining_quantity * close_fraction).quantize(Decimal("0.00000001"))
        if close_qty <= 0:
            return Decimal("0")

        pnl = _calculate_realized_pnl(
            side=trade.side,
            entry_price=trade.entry_price,
            exit_price=close_price,
            quantity=close_qty,
            fee_rate_pct=fee_rate_pct,
        )
        trade.realized_pnl += pnl
        trade.fees += _calc_fees(
            entry_price=trade.entry_price,
            exit_price=close_price,
            quantity=close_qty,
            fee_rate_pct=fee_rate_pct,
        )
        trade.remaining_quantity = max(
            Decimal("0"), trade.remaining_quantity - close_qty
        )
        if is_tp_hit:
            trade.targets_hit += 1
        trade.status = "partial_close" if trade.remaining_quantity > 0 else "closed"
        if trade.status == "closed":
            trade.closed_at = _utc_now()
            trade.exit_price = close_price
            trade.close_reason = reason

        if message:
            message.action = "partial_close" if trade.status == "partial_close" else "closed"
            message.notes.append(
                f"partial close {close_fraction * 100:.1f}% @ {close_price}, pnl={pnl:.4f}"
            )
            trade.add_attribution(message)
        return pnl

    def close_trade(
        self,
        *,
        trade: LiveTrade,
        close_price: Decimal,
        reason: str,
        fee_rate_pct: Decimal,
        message: MessageAttribution | None = None,
    ) -> Decimal:
        """Fully close a trade. Returns realized PnL."""
        if not trade.is_open:
            return Decimal("0")
        pnl = _calculate_realized_pnl(
            side=trade.side,
            entry_price=trade.entry_price,
            exit_price=close_price,
            quantity=trade.remaining_quantity,
            fee_rate_pct=fee_rate_pct,
        )
        trade.realized_pnl += pnl
        trade.fees += _calc_fees(
            entry_price=trade.entry_price,
            exit_price=close_price,
            quantity=trade.remaining_quantity,
            fee_rate_pct=fee_rate_pct,
        )
        trade.remaining_quantity = Decimal("0")
        trade.unrealized_pnl = Decimal("0")
        trade.status = "closed"
        trade.closed_at = _utc_now()
        trade.exit_price = close_price
        trade.close_reason = reason

        if message:
            message.action = "closed"
            message.notes.append(f"closed @ {close_price}, pnl={pnl:.4f}")
            trade.add_attribution(message)
        return pnl


# ─── Helpers ────────────────────────────────────────────────────────────────


def _sanitize_take_profits(
    *,
    take_profits: list[Decimal],
    side: str,
    entry_price: Decimal,
    stop_loss: Decimal | None,
) -> list[Decimal]:
    """Filter and sort TPs — same logic as BacktestSimulator._sanitize_take_profits."""
    from decimal import InvalidOperation
    is_long = side == "long"
    max_distance = (
        abs(entry_price - stop_loss) * Decimal("50")
        if stop_loss is not None
        else None
    )
    sanitized: list[Decimal] = []
    seen: set[Decimal] = set()
    for raw_tp in take_profits:
        try:
            tp = Decimal(raw_tp)
        except (InvalidOperation, TypeError):
            continue
        if tp <= Decimal("0"):
            continue
        if is_long and tp <= entry_price:
            continue
        if not is_long and tp >= entry_price:
            continue
        if max_distance is not None and max_distance > Decimal("0"):
            if abs(entry_price - tp) > max_distance:
                continue
        if tp in seen:
            continue
        seen.add(tp)
        sanitized.append(tp)
    sanitized.sort(reverse=not is_long)
    return sanitized


def _resolve_entry_price(signal: ParsedSignal) -> Decimal | None:
    if signal.entry_high is not None and signal.entry_low is not None:
        return ((signal.entry_high + signal.entry_low) / 2).quantize(Decimal("0.00000001"))
    if signal.entry_high is not None:
        return signal.entry_high
    if signal.entry_low is not None:
        return signal.entry_low
    return None


def _synthetic_stop(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_pct: Decimal,
) -> Decimal:
    dist = entry_price * stop_pct / Decimal("100")
    if side.is_long:
        return (entry_price - dist).quantize(Decimal("0.00000001"))
    return (entry_price + dist).quantize(Decimal("0.00000001"))


def _allocation_pct_for_signal(
    *,
    allocation_factor_pct: Decimal,
    leverage: Decimal,
    min_allocation_pct: Decimal,
    max_allocation_pct: Decimal,
) -> Decimal:
    effective_leverage = max(leverage, Decimal("1"))
    raw_pct = allocation_factor_pct / effective_leverage
    floor_pct = max(min_allocation_pct, Decimal("0"))
    ceiling_pct = max(max_allocation_pct, floor_pct)
    return min(max(raw_pct, floor_pct), ceiling_pct)


def _cap_synthetic_stop_loss_risk(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    balance_at_entry: Decimal,
    fee_rate_pct: Decimal,
    max_loss_pct_of_balance: Decimal,
) -> tuple[Decimal, Decimal, list[str]]:
    notes: list[str] = []
    if (
        quantity <= Decimal("0")
        or entry_price <= Decimal("0")
        or balance_at_entry <= Decimal("0")
    ):
        return stop_loss, quantity, notes

    max_loss_pct = max(max_loss_pct_of_balance, Decimal("0"))
    risk_budget = balance_at_entry * max_loss_pct / Decimal("100")
    if risk_budget <= Decimal("0"):
        return stop_loss, quantity, notes

    fee_rate = max(fee_rate_pct, Decimal("0")) / Decimal("100")
    base_fee_loss = (
        Decimal("2") * entry_price * quantity * fee_rate
        if fee_rate > Decimal("0")
        else Decimal("0")
    )
    if base_fee_loss >= risk_budget:
        if fee_rate <= Decimal("0"):
            return stop_loss, quantity, notes
        denominator = Decimal("2") * entry_price * fee_rate
        if denominator <= Decimal("0"):
            return stop_loss, quantity, notes
        capped_qty = risk_budget / denominator
        if capped_qty <= Decimal("0"):
            return stop_loss, Decimal("0"), ["synthetic_stop_risk_budget_exhausted_by_fees"]
        notes.append(f"synthetic_stop_qty_capped_for_risk_budget={quantity}->{capped_qty}")
        quantity = min(quantity, capped_qty)
        base_fee_loss = (
            Decimal("2") * entry_price * quantity * fee_rate
            if fee_rate > Decimal("0")
            else Decimal("0")
        )

    available_price_loss_budget = risk_budget - base_fee_loss
    if available_price_loss_budget <= Decimal("0"):
        capped_stop = entry_price
        if stop_loss != capped_stop:
            notes.append(
                "synthetic_stop_risk_capped="
                f"{stop_loss}->{capped_stop}; max_loss_pct={max_loss_pct_of_balance}"
            )
        return capped_stop, quantity, notes

    distance_denominator = (
        quantity * (Decimal("1") + fee_rate)
        if side.is_short
        else quantity * (Decimal("1") - fee_rate)
    )
    if distance_denominator <= Decimal("0"):
        return stop_loss, quantity, notes
    max_stop_distance = available_price_loss_budget / distance_denominator
    current_stop_distance = abs(entry_price - stop_loss)
    if current_stop_distance <= max_stop_distance:
        return stop_loss, quantity, notes

    if side.is_short:
        capped_stop = entry_price + max_stop_distance
    else:
        capped_stop = max(entry_price - max_stop_distance, Decimal("0"))
    notes.append(
        "synthetic_stop_risk_capped="
        f"{stop_loss}->{capped_stop}; max_loss_pct={max_loss_pct_of_balance}"
    )
    return capped_stop, quantity, notes


def _calculate_unrealized_pnl(
    *,
    side: str,
    entry_price: Decimal,
    mark_price: Decimal,
    quantity: Decimal,
    fee_rate_pct: Decimal,
) -> Decimal:
    if side == "long":
        raw_pnl = (mark_price - entry_price) * quantity
    else:
        raw_pnl = (entry_price - mark_price) * quantity
    exit_fee = mark_price * quantity * (fee_rate_pct / Decimal("100"))
    return raw_pnl - exit_fee


def _calculate_realized_pnl(
    *,
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    fee_rate_pct: Decimal,
) -> Decimal:
    if side == "long":
        raw_pnl = (exit_price - entry_price) * quantity
    else:
        raw_pnl = (entry_price - exit_price) * quantity
    fees = _calc_fees(
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        fee_rate_pct=fee_rate_pct,
    )
    return raw_pnl - fees


def _calc_fees(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    fee_rate_pct: Decimal,
) -> Decimal:
    rate = fee_rate_pct / Decimal("100")
    entry_fee = entry_price * quantity * rate
    exit_fee = exit_price * quantity * rate
    return entry_fee + exit_fee
