"""Position sizing, P&L, and SL/TP management for live/demo trading."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.config.settings import Settings
from triak_trade.core.logging import log_event
from triak_trade.domain.enums import TradeSide
from triak_trade.domain.models import ParsedSignal
from triak_trade.live_trading.models import LiveSession, LiveTrade, MessageAttribution
from triak_trade.risk.stop_loss import (
    StopLossRiskResult,
    clamp_stop_loss_to_risk_budget,
    max_quantity_for_stop_risk_budget,
)

log = logging.getLogger(__name__)


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
        balance_at_entry: Decimal,
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
        self.balance_at_entry = balance_at_entry
        self.stop_loss = stop_loss
        self.take_profits = take_profits
        self.is_synthetic_stop = is_synthetic_stop
        self.notes = notes


class LivePositionManager:
    """Handles position sizing, trade creation, SL/TP updates, and P&L calculation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _trade_fields(trade: LiveTrade) -> dict[str, object]:
        return {
            "trade_id": trade.trade_id,
            "signal_id": trade.signal_id,
            "symbol": trade.symbol,
            "side": trade.side,
            "status": trade.status,
            "leverage": trade.leverage,
            "entry_price": str(trade.entry_price),
            "quantity": str(trade.quantity),
            "remaining_quantity": str(trade.remaining_quantity),
            "stop_loss": str(trade.stop_loss) if trade.stop_loss is not None else None,
            "targets_hit": trade.targets_hit,
        }

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
            log_event(
                log,
                logging.WARNING,
                "live_trading.position_sizing_rejected",
                session_id=session.session_id,
                symbol=signal.symbol,
                side=signal.side.value,
                current_balance=str(current_balance),
                reason="non_positive_balance",
            )
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
            log_event(
                log,
                logging.WARNING,
                "live_trading.position_sizing_rejected",
                session_id=session.session_id,
                symbol=signal.symbol,
                side=signal.side.value,
                leverage=leverage,
                reason="missing_entry_price",
            )
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
            log_event(
                log,
                logging.WARNING,
                "live_trading.position_sizing_rejected",
                session_id=session.session_id,
                symbol=signal.symbol,
                side=signal.side.value,
                leverage=leverage,
                allocation_pct=str(allocation_pct),
                current_balance=str(current_balance),
                entry_price=str(entry_price),
                reason="non_positive_quantity",
            )
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
        assert stop_loss is not None
        if signal.stop_loss is not None:
            max_stop_safe_quantity = max_quantity_for_stop_risk_budget(
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
                balance_at_entry=current_balance,
                fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
                max_loss_pct_of_balance=(
                    self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE
                ),
            ).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if max_stop_safe_quantity <= Decimal("0"):
                raise ValueError(
                    "Requested stop loss leaves no positive risk-safe position size"
                )
            if quantity > max_stop_safe_quantity:
                previous_quantity = quantity
                quantity = max_stop_safe_quantity
                notes.append(
                    "quantity_risk_capped_to_preserve_stop="
                    f"{previous_quantity}->{quantity}; stop_loss={stop_loss}"
                )
        stop_risk = self._clamp_stop_loss(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            quantity=quantity,
            balance_at_entry=current_balance,
        )
        if signal.stop_loss is not None and stop_risk.was_capped:
            raise ValueError(
                "Risk-sized quantity could not preserve the requested stop loss"
            )
        stop_loss = signal.stop_loss if signal.stop_loss is not None else stop_risk.stop_loss
        if signal.stop_loss is None and stop_risk.was_capped:
            notes.append(
                "synthetic_stop_loss_risk_capped="
                f"max_loss_pct={self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE}; "
                f"risk_budget={stop_risk.risk_budget}; risk_amount={stop_risk.risk_amount}"
            )
        if quantity <= 0:
            log_event(
                log,
                logging.WARNING,
                "live_trading.position_sizing_rejected",
                session_id=session.session_id,
                symbol=signal.symbol,
                side=signal.side.value,
                leverage=leverage,
                entry_price=str(entry_price),
                reason="synthetic_stop_capped_to_zero_quantity",
            )
            raise ValueError("Quantity became zero after synthetic stop risk capping")

        take_profits = _sanitize_take_profits(
            take_profits=list(signal.take_profits),
            side="long" if side.is_long else "short",
            entry_price=entry_price,
            stop_loss=stop_loss,
        )
        if len(take_profits) < len(signal.take_profits):
            notes.append(f"tp_direction_filtered={len(signal.take_profits) - len(take_profits)}")
        explicit_take_profits_used = bool(take_profits)
        synthetic_take_profits_used = False
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
            synthetic_take_profits_used = bool(take_profits)
        tp_floor_notes: list[str] = []
        if synthetic_take_profits_used and not explicit_take_profits_used:
            take_profits, tp_floor_notes = _enforce_minimum_first_take_profit(
                take_profits=take_profits,
                is_long=side.is_long,
                entry_price=entry_price,
                min_profit_pct=self.settings.LIVE_TRADING_MIN_FIRST_TAKE_PROFIT_PCT,
            )
        if not take_profits:
            raise ValueError("No valid take-profit targets available for position sizing")
        if synthetic_take_profits_used and take_profits:
            notes.append(
                "synthetic_take_profits_strategy="
                + ",".join(str(item) for item in take_profits)
            )
        notes.extend(tp_floor_notes)

        margin = (entry_price * quantity / Decimal(str(leverage))).quantize(Decimal("0.00000001"))

        log_event(
            log,
            logging.INFO,
            "live_trading.position_sized",
            session_id=session.session_id,
            symbol=signal.symbol,
            side=signal.side.value,
            leverage=leverage,
            current_balance=str(current_balance),
            allocation_pct=str(allocation_pct),
            margin=str(margin),
            quantity=str(quantity),
            entry_price=str(entry_price),
            stop_loss=str(stop_loss) if stop_loss is not None else None,
            take_profit_count=len(take_profits),
            is_synthetic_stop=is_synthetic_stop,
            notes=notes,
        )

        return PositionSizingResult(
            quantity=quantity,
            allocation_pct=allocation_pct,
            margin=margin,
            leverage=leverage,
            entry_price=entry_price,
            balance_at_entry=current_balance,
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
            entry_type=signal.entry_type.value,
            requested_entry_low=signal.entry_low,
            requested_entry_high=signal.entry_high,
            requested_stop_loss=signal.stop_loss,
            requested_take_profits=list(signal.take_profits),
            quantity=sizing.quantity,
            remaining_quantity=sizing.quantity,
            stop_loss=sizing.stop_loss,
            take_profits=sizing.take_profits,
            margin=sizing.margin,
            balance_at_entry=sizing.balance_at_entry,
            status="waiting_entry",
            message_history=[attribution],
        )
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_created",
            session_id=session.session_id,
            trade_id=trade.trade_id,
            signal_id=signal_id,
            symbol=trade.symbol,
            side=trade.side,
            leverage=trade.leverage,
            quantity=str(trade.quantity),
            margin=str(trade.margin),
            stop_loss=str(trade.stop_loss) if trade.stop_loss is not None else None,
            take_profit_count=len(trade.take_profits),
            trigger_message_id=trigger_message_id,
            channel_id=channel_id,
        )
        return trade

    def update_stop_loss(
        self,
        *,
        trade: LiveTrade,
        new_sl: Decimal | None,
        message: MessageAttribution,
        move_to_entry: bool = False,
    ) -> StopLossRiskResult | None:
        previous_stop_loss = trade.stop_loss
        applied_risk: StopLossRiskResult | None = None
        if move_to_entry:
            trade.stop_loss = trade.entry_price
            message.notes.append(f"SL moved to entry (breakeven) {trade.entry_price}")
        elif new_sl is not None:
            risk = self.clamp_trade_stop_loss(trade=trade, stop_loss=new_sl)
            if risk.was_capped:
                message.action = "ignored"
                message.notes.append(
                    "SL update rejected to preserve requested value and account risk: "
                    f"requested={new_sl}; maximum_safe_stop={risk.stop_loss}; "
                    f"risk_budget={risk.risk_budget}"
                )
                trade.add_attribution(message)
                log_event(
                    log,
                    logging.WARNING,
                    "live_trading.trade_stop_loss_update_rejected",
                    requested_stop_loss=str(new_sl),
                    existing_stop_loss=(
                        str(previous_stop_loss)
                        if previous_stop_loss is not None
                        else None
                    ),
                    risk_budget=str(risk.risk_budget),
                    message_id=message.message_id,
                    **self._trade_fields(trade),
                )
                return risk
            trade.stop_loss = new_sl
            message.notes.append(f"SL updated to {new_sl}")
            applied_risk = risk
        else:
            return None
        trade.add_attribution(message)
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_stop_loss_updated",
            previous_stop_loss=(
                str(previous_stop_loss) if previous_stop_loss is not None else None
            ),
            new_stop_loss=str(trade.stop_loss) if trade.stop_loss is not None else None,
            move_to_entry=move_to_entry,
            message_id=message.message_id,
            **self._trade_fields(trade),
        )
        if applied_risk is not None:
            return applied_risk
        return (
            self.clamp_trade_stop_loss(trade=trade, stop_loss=trade.stop_loss)
            if trade.stop_loss is not None
            else None
        )

    def clamp_trade_stop_loss(
        self,
        *,
        trade: LiveTrade,
        stop_loss: Decimal,
    ) -> StopLossRiskResult:
        side = TradeSide.LONG if trade.side == "long" else TradeSide.SHORT
        return self._clamp_stop_loss(
            side=side,
            entry_price=trade.entry_price,
            stop_loss=stop_loss,
            quantity=trade.remaining_quantity,
            balance_at_entry=trade.balance_at_entry,
        )

    def _clamp_stop_loss(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
        stop_loss: Decimal,
        quantity: Decimal,
        balance_at_entry: Decimal,
    ) -> StopLossRiskResult:
        return clamp_stop_loss_to_risk_budget(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            quantity=quantity,
            balance_at_entry=balance_at_entry,
            fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
            max_loss_pct_of_balance=self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE,
        )

    def _record_stop_loss_risk_cap(
        self,
        *,
        trade: LiveTrade,
        requested_stop_loss: Decimal,
        result: StopLossRiskResult,
        message_id: int,
    ) -> None:
        if not result.was_capped:
            return
        log_event(
            log,
            logging.WARNING,
            "live_trading.trade_stop_loss_risk_capped",
            requested_stop_loss=str(requested_stop_loss),
            capped_stop_loss=str(result.stop_loss),
            risk_budget=str(result.risk_budget),
            risk_amount=str(result.risk_amount),
            max_loss_pct_of_balance=str(
                self.settings.LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE
            ),
            message_id=message_id,
            **self._trade_fields(trade),
        )

    def update_take_profits(
        self,
        *,
        trade: LiveTrade,
        new_tps: list[Decimal],
        message: MessageAttribution,
    ) -> bool:
        sanitized = _sanitize_take_profits(
            take_profits=new_tps,
            side=trade.side,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
        )
        if not sanitized:
            message.notes.append("TP update ignored: no valid price targets")
            trade.add_attribution(message)
            log_event(
                log,
                logging.WARNING,
                "live_trading.trade_take_profits_update_ignored",
                requested_take_profit_count=len(new_tps),
                message_id=message.message_id,
                **self._trade_fields(trade),
            )
            return False
        # Preserve already-hit targets, append new pending ones (matches backtest)
        trade.take_profits = trade.take_profits[: trade.targets_hit] + sanitized
        message.notes.append(f"TPs updated: {[str(t) for t in sanitized]}")
        trade.add_attribution(message)
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_take_profits_updated",
            take_profit_count=len(trade.take_profits),
            pending_take_profit_count=max(0, len(trade.take_profits) - trade.targets_hit),
            new_take_profits=[str(item) for item in sanitized],
            message_id=message.message_id,
            **self._trade_fields(trade),
        )
        return True

    def build_default_take_profits(
        self,
        *,
        trade: LiveTrade,
        strategy: TradeStrategy,
    ) -> list[Decimal]:
        """Build a safe synthetic TP ladder for a trade that has none pending."""
        if trade.stop_loss is None or trade.entry_price <= Decimal("0"):
            return []
        side = TradeSide.LONG if trade.side == "long" else TradeSide.SHORT
        try:
            generated = strategy.get_synthetic_take_profits(
                side=side,
                entry_price=trade.entry_price,
                stop_loss=trade.stop_loss,
                notional_value=trade.entry_price * trade.remaining_quantity,
            )
        except Exception:
            log.exception(
                "live_trading.default_take_profits_generation_failed",
                extra=self._trade_fields(trade),
            )
            return []
        sanitized = _sanitize_take_profits(
            take_profits=list(generated),
            side=trade.side,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
        )
        sanitized, _ = _enforce_minimum_first_take_profit(
            take_profits=sanitized,
            is_long=trade.side == "long",
            entry_price=trade.entry_price,
            min_profit_pct=self.settings.LIVE_TRADING_MIN_FIRST_TAKE_PROFIT_PCT,
        )
        return sanitized

    def build_default_stop_loss(
        self,
        *,
        trade: LiveTrade,
        strategy: TradeStrategy,
    ) -> Decimal | None:
        """Build and risk-cap a synthetic stop for an unprotected open trade."""
        if (
            trade.entry_price <= Decimal("0")
            or trade.remaining_quantity <= Decimal("0")
            or trade.balance_at_entry <= Decimal("0")
        ):
            return None
        side = TradeSide.LONG if trade.side == "long" else TradeSide.SHORT
        try:
            generated = strategy.get_synthetic_stop(
                side=side,
                entry_price=trade.entry_price,
                balance_at_entry=trade.balance_at_entry,
                quantity=trade.remaining_quantity,
                fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
            )
        except Exception:
            log.exception(
                "live_trading.default_stop_loss_generation_failed",
                extra=self._trade_fields(trade),
            )
            return None
        if generated <= Decimal("0"):
            return None
        if side.is_long and generated >= trade.entry_price:
            return None
        if side.is_short and generated <= trade.entry_price:
            return None
        return self.clamp_trade_stop_loss(
            trade=trade,
            stop_loss=generated,
        ).stop_loss

    def rebase_trade_protection_after_entry_fill(
        self,
        *,
        trade: LiveTrade,
        strategy: TradeStrategy,
    ) -> list[str]:
        """Recalculate protection from the actual exchange fill price.

        Pending orders can fill at a price that differs from the requested
        entry. Protection must never retain geometry calculated from the
        pre-fill reference price.
        """
        if trade.entry_price <= Decimal("0") or trade.remaining_quantity <= Decimal("0"):
            raise ValueError("Cannot build protection without a positive entry fill")

        notes: list[str] = []
        side = TradeSide.LONG if trade.side == "long" else TradeSide.SHORT
        requested_stop = (
            trade.requested_stop_loss
            if trade.requested_stop_loss is not None
            else trade.stop_loss
        )
        stop_is_directionally_valid = (
            requested_stop is not None
            and requested_stop > Decimal("0")
            and (
                (side.is_long and requested_stop < trade.entry_price)
                or (side.is_short and requested_stop > trade.entry_price)
            )
        )
        if stop_is_directionally_valid:
            assert requested_stop is not None
            stop_loss = requested_stop
        else:
            stop_loss = strategy.get_synthetic_stop(
                side=side,
                entry_price=trade.entry_price,
                balance_at_entry=trade.balance_at_entry,
                quantity=trade.remaining_quantity,
                fee_rate_pct=self.settings.LIVE_TRADING_FEE_RATE_PCT,
            )
            notes.append(
                "entry_fill_synthetic_stop="
                f"{getattr(strategy, 'name', 'unknown')}:{stop_loss}"
            )

        stop_risk = self._clamp_stop_loss(
            side=side,
            entry_price=trade.entry_price,
            stop_loss=stop_loss,
            quantity=trade.remaining_quantity,
            balance_at_entry=trade.balance_at_entry,
        )
        if trade.requested_stop_loss is not None and stop_risk.was_capped:
            raise ValueError(
                "Actual entry fill would require moving the requested stop loss "
                f"({stop_loss}->{stop_risk.stop_loss}); refusing unsafe protection rewrite"
            )
        trade.stop_loss = (
            stop_loss
            if trade.requested_stop_loss is not None
            else stop_risk.stop_loss
        )
        if trade.requested_stop_loss is None and stop_risk.was_capped:
            notes.append(
                "entry_fill_synthetic_stop_loss_risk_capped="
                f"{stop_loss}->{trade.stop_loss}; risk_budget={stop_risk.risk_budget}"
            )

        explicit_requested_tps = bool(trade.requested_take_profits)
        requested_tps = list(
            trade.requested_take_profits
            if explicit_requested_tps
            else trade.take_profits
        )
        take_profits = _sanitize_take_profits(
            take_profits=requested_tps,
            side=trade.side,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
        )
        if len(take_profits) < len(requested_tps):
            notes.append(
                f"entry_fill_tp_direction_filtered={len(requested_tps) - len(take_profits)}"
            )
        if not take_profits:
            take_profits = self.build_default_take_profits(
                trade=trade,
                strategy=strategy,
            )
            if take_profits:
                notes.append(
                    "entry_fill_default_take_profits="
                    + ",".join(str(item) for item in take_profits)
                )
        if not explicit_requested_tps:
            take_profits, floor_notes = _enforce_minimum_first_take_profit(
                take_profits=take_profits,
                is_long=side.is_long,
                entry_price=trade.entry_price,
                min_profit_pct=self.settings.LIVE_TRADING_MIN_FIRST_TAKE_PROFIT_PCT,
            )
            notes.extend(f"entry_fill_{note}" for note in floor_notes)
        if not take_profits:
            raise ValueError("No valid take-profit targets after entry fill")
        trade.take_profits = take_profits
        trade.targets_hit = 0
        return notes

    def update_leverage(
        self,
        *,
        trade: LiveTrade,
        new_leverage: int | None,
        message: MessageAttribution,
    ) -> bool:
        if new_leverage is None or new_leverage <= 0:
            log_event(
                log,
                logging.INFO,
                "live_trading.trade_leverage_update_ignored",
                requested_leverage=new_leverage,
                message_id=message.message_id,
                **self._trade_fields(trade),
            )
            return False
        previous_leverage = trade.leverage
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
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_leverage_updated",
            previous_leverage=previous_leverage,
            requested_leverage=new_leverage,
            new_leverage=clamped,
            was_clamped=clamped != new_leverage,
            message_id=message.message_id,
            **self._trade_fields(trade),
        )
        return True

    def apply_mark_price(
        self,
        *,
        trade: LiveTrade,
        mark_price: Decimal,
        fee_rate_pct: Decimal,
    ) -> None:
        previous_mark_price = trade.mark_price
        previous_unrealized_pnl = trade.unrealized_pnl
        trade.mark_price = mark_price
        pnl = _calculate_unrealized_pnl(
            side=trade.side,
            entry_price=trade.entry_price,
            mark_price=mark_price,
            quantity=trade.remaining_quantity,
            fee_rate_pct=fee_rate_pct,
        )
        trade.unrealized_pnl = pnl
        log_event(
            log,
            logging.DEBUG,
            "live_trading.trade_mark_price_applied",
            previous_mark_price=(
                str(previous_mark_price) if previous_mark_price is not None else None
            ),
            mark_price=str(mark_price),
            previous_unrealized_pnl=str(previous_unrealized_pnl),
            unrealized_pnl=str(trade.unrealized_pnl),
            fee_rate_pct=str(fee_rate_pct),
            **self._trade_fields(trade),
        )

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

        log_event(
            log,
            logging.DEBUG,
            "live_trading.trade_triggers_evaluated",
            mark_price=str(mark_price),
            trigger_stop_loss=str(trade.stop_loss) if trade.stop_loss is not None else None,
            event_count=len(events),
            events=events,
            **self._trade_fields(trade),
        )
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
            log_event(
                log,
                logging.INFO,
                "live_trading.trade_partial_close_ignored",
                close_fraction=str(close_fraction),
                close_price=str(close_price),
                reason=reason,
                **self._trade_fields(trade),
            )
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
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_partial_closed",
            close_fraction=str(close_fraction),
            close_price=str(close_price),
            close_quantity=str(close_qty),
            realized_pnl=str(pnl),
            reason=reason,
            is_tp_hit=is_tp_hit,
            **self._trade_fields(trade),
        )
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
            log_event(
                log,
                logging.INFO,
                "live_trading.trade_close_ignored",
                close_price=str(close_price),
                reason=reason,
                **self._trade_fields(trade),
            )
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
        log_event(
            log,
            logging.INFO,
            "live_trading.trade_closed",
            close_price=str(close_price),
            realized_pnl=str(pnl),
            reason=reason,
            fee_rate_pct=str(fee_rate_pct),
            **self._trade_fields(trade),
        )
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


def _enforce_minimum_first_take_profit(
    *,
    take_profits: list[Decimal],
    is_long: bool,
    entry_price: Decimal,
    min_profit_pct: Decimal,
) -> tuple[list[Decimal], list[str]]:
    if not take_profits or entry_price <= Decimal("0") or min_profit_pct <= Decimal("0"):
        return take_profits, []

    threshold_multiplier = (
        Decimal("1") + (min_profit_pct / Decimal("100"))
        if is_long
        else Decimal("1") - (min_profit_pct / Decimal("100"))
    )
    threshold = (entry_price * threshold_multiplier).quantize(Decimal("0.00000001"))
    adjusted = [
        tp
        for tp in take_profits
        if (tp >= threshold if is_long else tp <= threshold)
    ]
    if adjusted == take_profits:
        return take_profits, []

    notes = [
        f"tp1_min_profit_pct={min_profit_pct}",
        f"tp1_min_profit_floor={threshold}",
    ]
    if adjusted:
        notes.append(f"tp1_min_profit_removed={len(take_profits) - len(adjusted)}")
        return adjusted, notes

    notes.append(f"tp1_min_profit_replaced_all={len(take_profits)}")
    return [threshold], notes


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
