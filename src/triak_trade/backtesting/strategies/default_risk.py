"""Default risk-managed trade strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from triak_trade.backtesting.strategies.base import TargetHitAction
from triak_trade.domain.enums import TradeSide


@dataclass
class DefaultRiskManagedStrategy:
    """
    Risk-managed strategy with automatic risk-free and partial profit taking.

    Rules
    -----
    1. **No stop loss**: place a synthetic stop that caps worst-case loss to a
       configurable percentage of balance at entry.

    2. **Risk-free on first TP**: when ``risk_free_on_first_tp`` is True, the
       stop loss is automatically moved to the entry price after the first
       take-profit target is hit.  This means subsequent SL hits result in a
       breakeven trade rather than a loss.

    3. **Fallback TP ladder**: when a signal omits explicit targets, build a
       ladder from configurable profit percentages on the position notional.

    4. **Partial profit at each TP**: the fraction of remaining position to
       close at each successive TP is taken from ``tp_close_fractions``.
       - If the list is exhausted, the last entry is repeated.
       - The *final* pending target always closes 100 % of remaining quantity,
         regardless of the configured fraction.

    Default ``tp_close_fractions`` rationale
    -----------------------------------------
    Close more aggressively at early targets (which are more likely to hit)
    and let a smaller portion run toward the farther targets:

        TP1 → 35 %   TP2 → 40 % of rest   TP3 → 50 % of rest   TP4+ → all
        Approximate % of total: 35 / 26 / 20 / 20
    """

    name: str = "default_risk_managed"

    synthetic_stop_max_loss_pct_of_balance: Decimal = Decimal("5")
    """
    Cap the worst-case net loss of a synthetic stop-loss position to this
    percent of the balance that existed when the trade opened.
    """

    risk_free_on_first_tp: bool = True
    """Move the stop loss to entry price after the first TP target is hit."""

    synthetic_tp_profit_pct_steps: list[Decimal] = field(
        default_factory=lambda: [
            Decimal("2"),
            Decimal("4"),
            Decimal("6"),
            Decimal("8"),
            Decimal("10"),
        ]
    )
    """
    Profit milestones, as percentages of total position notional, for fallback
    take-profits when the signal omitted explicit targets.

    Example with 120 USDT notional on a LONG:
    - 2%  -> +2.4 USDT
    - 4%  -> +4.8 USDT
    - ...
    """

    tp_close_fractions: list[Decimal] = field(
        default_factory=lambda: [
            Decimal("0.35"),
            Decimal("0.40"),
            Decimal("0.50"),
        ]
    )
    """
    Fraction of *remaining* quantity to close at each successive TP.
    The last element is repeated for any TP beyond the list length.
    The very last pending target always closes 100 % regardless of this list.
    """

    # ------------------------------------------------------------------ #
    # Protocol implementation                                              #
    # ------------------------------------------------------------------ #

    def get_synthetic_stop(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
        balance_at_entry: Decimal,
        quantity: Decimal,
        fee_rate_pct: Decimal,
    ) -> Decimal:
        if (
            quantity <= Decimal("0")
            or entry_price <= Decimal("0")
            or balance_at_entry <= Decimal("0")
        ):
            return entry_price
        max_loss_pct = max(self.synthetic_stop_max_loss_pct_of_balance, Decimal("0"))
        risk_budget = balance_at_entry * max_loss_pct / Decimal("100")
        if risk_budget <= Decimal("0"):
            return entry_price

        fee_rate = max(fee_rate_pct, Decimal("0")) / Decimal("100")
        base_fee_loss = (
            Decimal("2") * entry_price * quantity * fee_rate
            if fee_rate > Decimal("0")
            else Decimal("0")
        )
        if base_fee_loss >= risk_budget:
            return entry_price

        available_price_loss_budget = risk_budget - base_fee_loss
        distance_denominator = (
            quantity * (Decimal("1") + fee_rate)
            if side.is_short
            else quantity * (Decimal("1") - fee_rate)
        )
        if distance_denominator <= Decimal("0"):
            return entry_price
        max_stop_distance = available_price_loss_budget / distance_denominator
        if side.is_short:
            return entry_price + max_stop_distance
        return max(entry_price - max_stop_distance, Decimal("0"))

    def get_synthetic_take_profits(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
        stop_loss: Decimal,
        notional_value: Decimal,
    ) -> list[Decimal]:
        if entry_price <= Decimal("0") or notional_value <= Decimal("0"):
            return []
        profit_pct_steps = self.synthetic_tp_profit_pct_steps or [
            Decimal("2"),
            Decimal("4"),
            Decimal("6"),
            Decimal("8"),
            Decimal("10"),
        ]
        quantity = notional_value / entry_price
        if quantity <= Decimal("0"):
            return []
        targets: list[Decimal] = []
        for step in profit_pct_steps:
            if step <= Decimal("0"):
                continue
            target_profit_value = notional_value * step / Decimal("100")
            price_move = target_profit_value / quantity
            target = (
                entry_price - price_move
                if side.is_short
                else entry_price + price_move
            )
            if target not in targets:
                targets.append(target)
        return targets

    def get_target_hit_action(
        self,
        *,
        targets_hit_so_far: int,
        remaining_targets_including_this: int,
        entry_price: Decimal,
        take_profits: list[Decimal],
    ) -> TargetHitAction:
        is_last = remaining_targets_including_this <= 1
        move_sl = self.risk_free_on_first_tp and targets_hit_so_far == 0

        if is_last:
            return TargetHitAction(close_fraction=Decimal("1"), move_sl_to_entry=move_sl)

        fractions = self.tp_close_fractions
        if not fractions:
            # Fallback: equal split
            fraction = Decimal("1") / Decimal(remaining_targets_including_this)
            return TargetHitAction(close_fraction=fraction, move_sl_to_entry=move_sl)

        # Use the fraction at the 0-based index of this hit; repeat last entry
        idx = min(targets_hit_so_far, len(fractions) - 1)
        fraction = fractions[idx]
        # Clamp to (0, 1) — never close more than 100 % or a negative amount
        fraction = min(max(fraction, Decimal("0.01")), Decimal("0.99"))
        return TargetHitAction(close_fraction=fraction, move_sl_to_entry=move_sl)
