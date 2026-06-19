"""Trailing stop strategy that advances the stop to the previous TP."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from triak_trade.backtesting.strategies.base import TargetHitAction
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy


@dataclass
class TrailingTakeProfitStrategy(DefaultRiskManagedStrategy):
    """
    Default risk-managed strategy with trailing stop promotion by TP ladder.

    Rules:
    - After TP1: optional breakeven if ``risk_free_on_first_tp`` is enabled.
    - After TP2: stop loss moves to TP1.
    - After TP3: stop loss moves to TP2.
    - And so on.
    """

    name: str = "tp_trailing_risk_managed"

    def get_target_hit_action(
        self,
        *,
        targets_hit_so_far: int,
        remaining_targets_including_this: int,
        entry_price: Decimal,
        take_profits: list[Decimal],
    ) -> TargetHitAction:
        base = super().get_target_hit_action(
            targets_hit_so_far=targets_hit_so_far,
            remaining_targets_including_this=remaining_targets_including_this,
            entry_price=entry_price,
            take_profits=take_profits,
        )
        current_target_index = targets_hit_so_far
        if current_target_index <= 0:
            return base
        previous_tp_index = current_target_index - 1
        if previous_tp_index >= len(take_profits):
            return base
        return TargetHitAction(
            close_fraction=base.close_fraction,
            move_sl_to_entry=False,
            new_stop_loss=take_profits[previous_tp_index],
        )
