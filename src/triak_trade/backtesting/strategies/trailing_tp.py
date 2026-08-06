"""Trailing stop strategy that advances the stop behind completed TPs."""

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
    - With fewer than five targets, the stop trails one target behind.
    - With five or more targets, TP2 keeps the stop at breakeven and from TP3
      onward the stop trails two targets behind (TP3 -> TP1, TP4 -> TP2, ...).
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
        trailing_gap = 2 if len(take_profits) >= 5 else 1
        previous_tp_index = current_target_index - trailing_gap
        if previous_tp_index < 0:
            return base
        if previous_tp_index >= len(take_profits):
            return base
        return TargetHitAction(
            close_fraction=base.close_fraction,
            move_sl_to_entry=False,
            new_stop_loss=take_profits[previous_tp_index],
        )
