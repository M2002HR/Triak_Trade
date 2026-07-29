"""Pure take-profit capacity planning for exchange-constrained positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal


@dataclass(frozen=True)
class TakeProfitCandidate:
    """A logical target and the strategy's preferred contract quantity."""

    target_index: int
    price: Decimal
    preferred_contract_quantity: Decimal


@dataclass(frozen=True)
class PlannedTakeProfitOrder:
    """A target whose quantity is valid for the exchange contract."""

    target_index: int
    price: Decimal
    contract_quantity: Decimal


@dataclass(frozen=True)
class TakeProfitCapacityPlan:
    """Maximum feasible TP ladder for one fixed-size position."""

    original_target_count: int
    total_contract_quantity: Decimal
    minimum_contract_quantity: Decimal
    contract_step_size: Decimal
    orders: tuple[PlannedTakeProfitOrder, ...]

    @property
    def target_count(self) -> int:
        return len(self.orders)

    @property
    def allocated_contract_quantity(self) -> Decimal:
        return sum(
            (order.contract_quantity for order in self.orders),
            start=Decimal("0"),
        )


def plan_maximum_take_profit_orders(
    *,
    candidates: list[TakeProfitCandidate],
    total_contract_quantity: Decimal,
    minimum_contract_quantity: Decimal,
    contract_step_size: Decimal,
) -> TakeProfitCapacityPlan:
    """Allocate a fixed position across the maximum feasible target count.

    The position is never enlarged. Quantity that is not a complete exchange
    step is excluded, and every selected target receives at least the
    exchange minimum. Earlier targets retain the strategy's preferred
    quantity where that does not make a later target invalid.
    """

    if contract_step_size <= 0:
        raise ValueError("contract_step_size must be positive")
    if minimum_contract_quantity < 0:
        raise ValueError("minimum_contract_quantity cannot be negative")

    total_units = _floor_step_units(total_contract_quantity, contract_step_size)
    normalized_total = Decimal(total_units) * contract_step_size
    minimum_units = max(
        1,
        _ceiling_step_units(minimum_contract_quantity, contract_step_size),
    )
    normalized_minimum = Decimal(minimum_units) * contract_step_size
    maximum_target_count = min(
        len(candidates),
        total_units // minimum_units,
    )
    if maximum_target_count <= 0:
        return TakeProfitCapacityPlan(
            original_target_count=len(candidates),
            total_contract_quantity=normalized_total,
            minimum_contract_quantity=normalized_minimum,
            contract_step_size=contract_step_size,
            orders=(),
        )

    selected_candidates = candidates[:maximum_target_count]
    remaining_units = total_units
    orders: list[PlannedTakeProfitOrder] = []
    for position, candidate in enumerate(selected_candidates):
        remaining_order_count = maximum_target_count - position
        if remaining_order_count == 1:
            allocated_units = remaining_units
        else:
            maximum_units_for_order = remaining_units - (
                (remaining_order_count - 1) * minimum_units
            )
            preferred_units = _floor_step_units(
                candidate.preferred_contract_quantity,
                contract_step_size,
            )
            allocated_units = min(
                maximum_units_for_order,
                max(minimum_units, preferred_units),
            )

        if allocated_units < minimum_units:
            raise AssertionError("TP order quantity is below the exchange minimum")
        orders.append(
            PlannedTakeProfitOrder(
                target_index=candidate.target_index,
                price=candidate.price,
                contract_quantity=Decimal(allocated_units) * contract_step_size,
            )
        )
        remaining_units -= allocated_units

    plan = TakeProfitCapacityPlan(
        original_target_count=len(candidates),
        total_contract_quantity=normalized_total,
        minimum_contract_quantity=normalized_minimum,
        contract_step_size=contract_step_size,
        orders=tuple(orders),
    )
    if remaining_units != 0:
        raise AssertionError("TP planner left exchange quantity unallocated")
    if plan.allocated_contract_quantity != plan.total_contract_quantity:
        raise AssertionError("TP planner must allocate the complete position")
    return plan


def _floor_step_units(quantity: Decimal, step: Decimal) -> int:
    if quantity <= 0:
        return 0
    return int((quantity / step).to_integral_value(rounding=ROUND_DOWN))


def _ceiling_step_units(quantity: Decimal, step: Decimal) -> int:
    if quantity <= 0:
        return 0
    return int((quantity / step).to_integral_value(rounding=ROUND_UP))
