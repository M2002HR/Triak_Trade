"""Pure stop-loss calculations shared by execution-facing modules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from triak_trade.domain.enums import TradeSide


@dataclass(frozen=True)
class StopLossRiskResult:
    """The safe stop price and its modeled worst-case net loss."""

    stop_loss: Decimal
    risk_amount: Decimal
    risk_budget: Decimal
    was_capped: bool


def max_quantity_for_stop_risk_budget(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    balance_at_entry: Decimal,
    fee_rate_pct: Decimal,
    max_loss_pct_of_balance: Decimal,
) -> Decimal:
    """Return the largest quantity that preserves the requested stop.

    Risk is linear in quantity, including entry and exit fees. Returning zero
    means the inputs cannot produce a safe positive position size.
    """

    if (
        entry_price <= Decimal("0")
        or stop_loss <= Decimal("0")
        or balance_at_entry <= Decimal("0")
        or side not in {TradeSide.LONG, TradeSide.SHORT}
    ):
        return Decimal("0")
    risk_budget = balance_at_entry * max(
        max_loss_pct_of_balance,
        Decimal("0"),
    ) / Decimal("100")
    if risk_budget <= Decimal("0"):
        return Decimal("0")
    fee_rate = max(fee_rate_pct, Decimal("0")) / Decimal("100")
    risk_per_unit = _net_loss_at_stop(
        side=side,
        entry_price=entry_price,
        stop_loss=stop_loss,
        quantity=Decimal("1"),
        fee_rate=fee_rate,
    )
    if risk_per_unit <= Decimal("0"):
        return Decimal("0")
    return risk_budget / risk_per_unit


def clamp_stop_loss_to_risk_budget(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    balance_at_entry: Decimal,
    fee_rate_pct: Decimal,
    max_loss_pct_of_balance: Decimal,
) -> StopLossRiskResult:
    """Clamp an adverse stop so modeled net loss cannot exceed its budget.

    The calculation includes one entry and one exit fee. A stop already on the
    profitable side of entry is left untouched unless fees would still make its
    exit exceed the configured loss budget.
    """

    risk_budget = max(balance_at_entry, Decimal("0")) * max(
        max_loss_pct_of_balance, Decimal("0")
    ) / Decimal("100")
    if (
        entry_price <= Decimal("0")
        or stop_loss <= Decimal("0")
        or quantity <= Decimal("0")
        or balance_at_entry <= Decimal("0")
        or side not in {TradeSide.LONG, TradeSide.SHORT}
    ):
        return StopLossRiskResult(
            stop_loss=stop_loss,
            risk_amount=Decimal("0"),
            risk_budget=risk_budget,
            was_capped=False,
        )

    fee_rate = max(fee_rate_pct, Decimal("0")) / Decimal("100")
    if side is TradeSide.LONG:
        denominator = Decimal("1") - fee_rate
        minimum_safe_stop = (
            entry_price * (Decimal("1") + fee_rate) - (risk_budget / quantity)
        ) / denominator
        safe_stop = max(minimum_safe_stop, Decimal("0"))
        capped_stop = max(stop_loss, safe_stop)
    else:
        denominator = Decimal("1") + fee_rate
        maximum_safe_stop = (
            entry_price * (Decimal("1") - fee_rate) + (risk_budget / quantity)
        ) / denominator
        capped_stop = min(stop_loss, maximum_safe_stop)

    risk_amount = _net_loss_at_stop(
        side=side,
        entry_price=entry_price,
        stop_loss=capped_stop,
        quantity=quantity,
        fee_rate=fee_rate,
    )
    # Repeating Decimal divisions can land infinitesimally beyond the budget.
    # Move one representable Decimal toward entry until the invariant holds.
    for _ in range(4):
        if risk_amount <= risk_budget:
            break
        capped_stop = (
            capped_stop.next_plus()
            if side is TradeSide.LONG
            else capped_stop.next_minus()
        )
        risk_amount = _net_loss_at_stop(
            side=side,
            entry_price=entry_price,
            stop_loss=capped_stop,
            quantity=quantity,
            fee_rate=fee_rate,
        )
    return StopLossRiskResult(
        stop_loss=capped_stop,
        risk_amount=risk_amount,
        risk_budget=risk_budget,
        was_capped=capped_stop != stop_loss,
    )


def _net_loss_at_stop(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    gross_pnl = (
        (stop_loss - entry_price) * quantity
        if side is TradeSide.LONG
        else (entry_price - stop_loss) * quantity
    )
    fees = (entry_price + stop_loss) * quantity * fee_rate
    return max(-(gross_pnl - fees), Decimal("0"))
