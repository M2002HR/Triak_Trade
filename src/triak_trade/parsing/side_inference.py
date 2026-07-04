"""Helpers for inferring a missing trade side from price geometry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from triak_trade.domain.enums import TradeSide


@dataclass(frozen=True)
class SideInferenceResult:
    """Outcome of price-geometry side inference."""

    side: TradeSide
    reason: str | None = None


def infer_trade_side_from_price_geometry(
    *,
    entry_low: Decimal | None,
    entry_high: Decimal | None,
    stop_loss: Decimal | None,
    take_profits: Sequence[Decimal],
) -> SideInferenceResult:
    """Infer LONG/SHORT from entry, stop-loss, and take-profit geometry.

    The inference is intentionally conservative:

    - We require an entry reference.
    - Conflicting stop/target evidence returns UNKNOWN.
    - Targets on only one side of the entry are enough.
    - Otherwise a stop-loss clearly outside the entry range can decide.
    """

    bounds = _entry_bounds(entry_low=entry_low, entry_high=entry_high)
    if bounds is None:
        return SideInferenceResult(TradeSide.UNKNOWN, "missing_entry_reference")

    lower_bound, upper_bound = bounds
    long_targets = sum(1 for value in take_profits if value > upper_bound)
    short_targets = sum(1 for value in take_profits if value < lower_bound)

    stop_side = TradeSide.UNKNOWN
    if stop_loss is not None:
        if stop_loss < lower_bound:
            stop_side = TradeSide.LONG
        elif stop_loss > upper_bound:
            stop_side = TradeSide.SHORT

    if stop_side is TradeSide.LONG and short_targets > 0:
        return SideInferenceResult(TradeSide.UNKNOWN, "conflicting_stop_and_targets")
    if stop_side is TradeSide.SHORT and long_targets > 0:
        return SideInferenceResult(TradeSide.UNKNOWN, "conflicting_stop_and_targets")

    if long_targets > 0 and short_targets == 0:
        reason = "targets_above_entry"
        if stop_side is TradeSide.LONG:
            reason = f"{reason}+stop_below_entry"
        return SideInferenceResult(TradeSide.LONG, reason)

    if short_targets > 0 and long_targets == 0:
        reason = "targets_below_entry"
        if stop_side is TradeSide.SHORT:
            reason = f"{reason}+stop_above_entry"
        return SideInferenceResult(TradeSide.SHORT, reason)

    if stop_side is TradeSide.LONG:
        return SideInferenceResult(TradeSide.LONG, "stop_below_entry")
    if stop_side is TradeSide.SHORT:
        return SideInferenceResult(TradeSide.SHORT, "stop_above_entry")

    if long_targets > 0 and short_targets > 0:
        return SideInferenceResult(TradeSide.UNKNOWN, "mixed_targets")
    return SideInferenceResult(TradeSide.UNKNOWN, "insufficient_directional_evidence")


def _entry_bounds(
    *,
    entry_low: Decimal | None,
    entry_high: Decimal | None,
) -> tuple[Decimal, Decimal] | None:
    if entry_low is None and entry_high is None:
        return None
    if entry_low is None:
        assert entry_high is not None
        return entry_high, entry_high
    if entry_high is None:
        return entry_low, entry_low
    if entry_low <= entry_high:
        return entry_low, entry_high
    return entry_high, entry_low
