"""Base protocol and types for trade strategies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from triak_trade.domain.enums import TradeSide


@dataclass(frozen=True)
class TargetHitAction:
    """Instructions returned by a strategy when a TP target is hit."""

    close_fraction: Decimal
    """Fraction of remaining quantity to close at this target (0-1)."""

    move_sl_to_entry: bool = False
    """If True, move the stop loss to the entry price (risk-free / breakeven)."""


@runtime_checkable
class TradeStrategy(Protocol):
    """
    Protocol that all trade strategies must implement.

    A strategy is a stateless set of rules that controls:
    - Synthetic stop placement when a signal has no stop loss.
    - How much of the position to close at each take-profit target.
    - Whether to move the stop loss to breakeven after a target hit.

    Strategies are designed to be reusable in both backtesting and live
    execution — they contain no simulation-specific state.
    """

    name: str

    def get_synthetic_stop(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
    ) -> Decimal:
        """
        Return a synthetic stop-loss price for a signal that has none.

        The returned price must be on the correct side of the entry:
        - LONG: below entry_price
        - SHORT: above entry_price
        """
        ...

    def get_synthetic_take_profits(
        self,
        *,
        side: TradeSide,
        entry_price: Decimal,
        stop_loss: Decimal,
    ) -> list[Decimal]:
        """
        Return a fallback take-profit ladder when the signal has no explicit TP.

        The returned values must be on the correct side of the entry:
        - LONG: above entry_price
        - SHORT: below entry_price
        """
        ...

    def get_target_hit_action(
        self,
        *,
        targets_hit_so_far: int,
        remaining_targets_including_this: int,
    ) -> TargetHitAction:
        """
        Return the action to take when a TP target is hit.

        Parameters
        ----------
        targets_hit_so_far:
            Number of TP targets that were hit *before* the current one (0-indexed).
        remaining_targets_including_this:
            Count of targets still pending, including the one currently being hit.
            When this equals 1, it is the final target.
        """
        ...
