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
    1. **No stop loss**: place a synthetic stop at ``no_sl_loss_pct`` percent
       away from entry (e.g. 100 % → entry doubles distance, effectively a very
       wide stop that protects against catastrophic moves but does not
       interfere with normal TP-driven management).

    2. **Risk-free on first TP**: when ``risk_free_on_first_tp`` is True, the
       stop loss is automatically moved to the entry price after the first
       take-profit target is hit.  This means subsequent SL hits result in a
       breakeven trade rather than a loss.

    3. **Partial profit at each TP**: the fraction of remaining position to
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

    no_sl_loss_pct: Decimal = Decimal("100")
    """
    When a signal has no stop loss, place a synthetic stop this many percent
    from entry.  100 % means the stop sits at zero for a LONG (never reached
    in practice) and at double the entry price for a SHORT.
    """

    risk_free_on_first_tp: bool = True
    """Move the stop loss to entry price after the first TP target is hit."""

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

    synthetic_tp_r_multiples: list[Decimal] = field(
        default_factory=lambda: [
            Decimal("1"),
            Decimal("2"),
            Decimal("3"),
        ]
    )
    """
    Risk-multiple ladder used when a signal omits explicit take-profit targets.

    Example for a LONG:
    - risk = entry - stop
    - TP1 = entry + 1R
    - TP2 = entry + 2R
    - TP3 = entry + 3R
    """

    # ------------------------------------------------------------------ #
    # Protocol implementation                                              #
    # ------------------------------------------------------------------ #

    def get_synthetic_stop(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
    ) -> Decimal:
        pct = max(self.no_sl_loss_pct, Decimal("0")) / Decimal("100")
        if side.is_short:
            return entry_price * (Decimal("1") + pct)
        # LONG: stop below entry.  At 100 % this equals 0, which is valid —
        # crypto prices never reach zero in normal trading.
        return max(entry_price * (Decimal("1") - pct), Decimal("0"))

    def get_synthetic_take_profits(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
        stop_loss: Decimal,
    ) -> list[Decimal]:
        risk_distance = abs(entry_price - stop_loss)
        if risk_distance <= Decimal("0"):
            return []
        multiples = self.synthetic_tp_r_multiples or [Decimal("1"), Decimal("2"), Decimal("3")]
        if side.is_short:
            return [
                entry_price - (risk_distance * multiple)
                for multiple in multiples
                if multiple > Decimal("0")
            ]
        return [
            entry_price + (risk_distance * multiple)
            for multiple in multiples
            if multiple > Decimal("0")
        ]

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
