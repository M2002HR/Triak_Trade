"""Tests for fixed-volume exchange take-profit capacity planning."""

from __future__ import annotations

from decimal import Decimal

import pytest

from triak_trade.live_trading.take_profit_planner import (
    TakeProfitCandidate,
    plan_maximum_take_profit_orders,
)


def _candidates(count: int) -> list[TakeProfitCandidate]:
    return [
        TakeProfitCandidate(
            target_index=index,
            price=Decimal("1") + (Decimal(index) / Decimal("10")),
            preferred_contract_quantity=Decimal("122") if index == 0 else Decimal("50"),
        )
        for index in range(count)
    ]


def test_uses_all_targets_when_every_order_can_meet_minimum() -> None:
    plan = plan_maximum_take_profit_orders(
        candidates=_candidates(5),
        total_contract_quantity=Decimal("500"),
        minimum_contract_quantity=Decimal("100"),
        contract_step_size=Decimal("1"),
    )

    assert plan.target_count == 5
    assert [order.target_index for order in plan.orders] == [0, 1, 2, 3, 4]
    assert [order.contract_quantity for order in plan.orders] == [
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
    ]
    assert plan.allocated_contract_quantity == Decimal("500")


def test_selects_maximum_subset_without_increasing_position() -> None:
    plan = plan_maximum_take_profit_orders(
        candidates=_candidates(8),
        total_contract_quantity=Decimal("350"),
        minimum_contract_quantity=Decimal("100"),
        contract_step_size=Decimal("1"),
    )

    assert plan.target_count == 3
    assert [order.target_index for order in plan.orders] == [0, 1, 2]
    assert all(order.contract_quantity >= Decimal("100") for order in plan.orders)
    assert plan.allocated_contract_quantity == Decimal("350")
    assert plan.total_contract_quantity == Decimal("350")


def test_honors_non_unit_step_and_allocates_every_executable_unit() -> None:
    plan = plan_maximum_take_profit_orders(
        candidates=_candidates(8),
        total_contract_quantity=Decimal("379"),
        minimum_contract_quantity=Decimal("95"),
        contract_step_size=Decimal("10"),
    )

    assert plan.target_count == 3
    assert plan.total_contract_quantity == Decimal("370")
    assert plan.minimum_contract_quantity == Decimal("100")
    assert plan.allocated_contract_quantity == Decimal("370")
    assert all(
        order.contract_quantity % Decimal("10") == 0 and order.contract_quantity >= Decimal("100")
        for order in plan.orders
    )


def test_returns_no_orders_when_fixed_position_is_below_minimum() -> None:
    plan = plan_maximum_take_profit_orders(
        candidates=_candidates(8),
        total_contract_quantity=Decimal("99"),
        minimum_contract_quantity=Decimal("100"),
        contract_step_size=Decimal("1"),
    )

    assert plan.target_count == 0
    assert plan.orders == ()
    assert plan.total_contract_quantity == Decimal("99")


@pytest.mark.parametrize(
    ("minimum", "step", "error"),
    [
        (Decimal("1"), Decimal("0"), "contract_step_size"),
        (Decimal("-1"), Decimal("1"), "minimum_contract_quantity"),
    ],
)
def test_rejects_invalid_exchange_constraints(
    minimum: Decimal,
    step: Decimal,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        plan_maximum_take_profit_orders(
            candidates=_candidates(1),
            total_contract_quantity=Decimal("1"),
            minimum_contract_quantity=minimum,
            contract_step_size=step,
        )
