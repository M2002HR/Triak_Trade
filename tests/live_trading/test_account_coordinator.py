"""Unit tests for account-wide multi-session execution coordination."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from triak_trade.live_trading.account_coordinator import (
    AccountExecutionConflictError,
    AccountExecutionCoordinator,
    AccountPositionPolicy,
    run_account_coordination_dry_run,
)
from triak_trade.live_trading.models import LiveEntryOrderPlan, LiveTrade


def _trade(
    trade_id: str,
    *,
    session_id: str = "ls_one",
    channel_id: str = "@one",
    side: str = "long",
    quantity: str = "1",
    entry: str = "100",
    stop: str = "95",
    opened_at: datetime | None = None,
) -> LiveTrade:
    return LiveTrade(
        trade_id=trade_id,
        session_id=session_id,
        signal_id=f"sig_{trade_id}",
        channel_id=channel_id,
        channel_input=channel_id,
        channel_label=channel_id,
        symbol="BTCUSDT",
        side=side,
        entry_price=Decimal(entry),
        quantity=Decimal(quantity),
        remaining_quantity=Decimal(quantity),
        stop_loss=Decimal(stop),
        take_profits=[Decimal("110") if side == "long" else Decimal("90")],
        balance_at_entry=Decimal("1000"),
        status="open",
        opened_at=opened_at or datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_execution_guard_serializes_concurrent_tasks() -> None:
    coordinator = AccountExecutionCoordinator()
    active = 0
    maximum_active = 0

    async def worker() -> None:
        nonlocal active, maximum_active
        async with coordinator.execution_guard():
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(worker(), worker(), worker())

    assert maximum_active == 1


@pytest.mark.asyncio
async def test_duplicate_same_direction_is_consensus_not_second_order() -> None:
    coordinator = AccountExecutionCoordinator(
        duplicate_window_seconds=300,
        duplicate_price_tolerance_bps=Decimal("20"),
    )
    first = _trade("first")
    coordinator.register_trade(first, trading_mode="live")
    candidate = _trade(
        "second",
        session_id="ls_two",
        channel_id="@two",
        opened_at=first.opened_at + timedelta(seconds=10),
    )

    decision = await coordinator.coordinate_open(candidate, trading_mode="live")

    assert decision.action == "duplicate"
    assert decision.approved_quantity == Decimal("0")
    assert decision.duplicate_trade_id == "first"


@pytest.mark.asyncio
async def test_same_direction_is_resized_to_symbol_risk_capacity() -> None:
    coordinator = AccountExecutionCoordinator(
        symbol_risk_cap_pct=Decimal("1"),
        fee_rate_pct=Decimal("0"),
        duplicate_window_seconds=0,
    )
    first = _trade("first", quantity="1", entry="100", stop="95")
    coordinator.register_trade(first, trading_mode="live")
    candidate = _trade(
        "second",
        session_id="ls_two",
        channel_id="@two",
        quantity="2",
        entry="100",
        stop="95",
    )

    decision = await coordinator.coordinate_open(candidate, trading_mode="live")

    # Account risk cap is 10 USDT. Existing risk is 5; only one more unit fits.
    assert decision.action == "open"
    assert decision.approved_quantity == Decimal("1")
    assert candidate.quantity == Decimal("1")
    assert candidate.remaining_quantity == Decimal("1")


@pytest.mark.asyncio
async def test_opposite_signal_reduces_then_opens_only_residual() -> None:
    coordinator = AccountExecutionCoordinator(
        position_policy=AccountPositionPolicy.NET,
        symbol_risk_cap_pct=Decimal("100"),
        fee_rate_pct=Decimal("0"),
        duplicate_window_seconds=0,
    )
    long_trade = _trade("long", quantity="6")
    close_requests: list[Decimal] = []

    async def close_handler(trade_id: str, quantity: Decimal, reason: str) -> Decimal:
        assert trade_id == "long"
        assert reason == "netted_by_signal:sig_short"
        close_requests.append(quantity)
        long_trade.remaining_quantity -= quantity
        long_trade.status = "closed" if long_trade.remaining_quantity == 0 else "partial_close"
        coordinator.register_trade(
            long_trade,
            trading_mode="live",
            close_handler=close_handler,
        )
        if not long_trade.is_open:
            coordinator.unregister_trade(long_trade.trade_id)
        return quantity

    coordinator.register_trade(
        long_trade,
        trading_mode="live",
        close_handler=close_handler,
    )
    short_trade = _trade(
        "short",
        session_id="ls_two",
        channel_id="@two",
        side="short",
        quantity="10",
        stop="105",
    )

    decision = await coordinator.coordinate_open(short_trade, trading_mode="live")

    assert close_requests == [Decimal("6")]
    assert decision.action == "open"
    assert decision.offset_quantity == Decimal("6")
    assert decision.approved_quantity == Decimal("4")
    assert short_trade.quantity == Decimal("4")


@pytest.mark.asyncio
async def test_opposite_signal_fully_netted_does_not_open_new_side() -> None:
    coordinator = AccountExecutionCoordinator(
        position_policy=AccountPositionPolicy.NET,
        symbol_risk_cap_pct=Decimal("100"),
        duplicate_window_seconds=0,
    )
    long_trade = _trade("long", quantity="10")

    async def close_handler(trade_id: str, quantity: Decimal, reason: str) -> Decimal:
        del trade_id, reason
        long_trade.remaining_quantity -= quantity
        long_trade.status = "partial_close"
        coordinator.register_trade(
            long_trade,
            trading_mode="live",
            close_handler=close_handler,
        )
        return quantity

    coordinator.register_trade(
        long_trade,
        trading_mode="live",
        close_handler=close_handler,
    )
    short_trade = _trade("short", side="short", quantity="6", stop="105")

    decision = await coordinator.coordinate_open(short_trade, trading_mode="live")

    assert decision.action == "netted"
    assert decision.offset_quantity == Decimal("6")
    assert decision.approved_quantity == Decimal("0")
    assert long_trade.remaining_quantity == Decimal("4")


@pytest.mark.asyncio
async def test_opposite_signal_fails_closed_without_owner_engine() -> None:
    coordinator = AccountExecutionCoordinator(position_policy=AccountPositionPolicy.NET)
    coordinator.register_trade(_trade("long"), trading_mode="live")
    candidate = _trade("short", side="short", stop="105")

    with pytest.raises(AccountExecutionConflictError, match="no active owner engine"):
        await coordinator.coordinate_open(candidate, trading_mode="live")


def test_fill_and_order_ownership_are_account_wide() -> None:
    coordinator = AccountExecutionCoordinator()
    first = _trade("first")
    second = _trade("second", session_id="ls_two", channel_id="@two")

    assert coordinator.mark_fill_owner(
        "trade:123",
        first,
        trading_mode="live",
    )
    assert not coordinator.mark_fill_owner(
        "trade:123",
        second,
        trading_mode="live",
    )
    assert coordinator.fill_is_available("trade:123", "first")
    assert not coordinator.fill_is_available("trade:123", "second")

    coordinator.mark_order_owner("order-1", first, trading_mode="live")
    assert coordinator.order_is_owned_by("order-1", "first")
    assert not coordinator.order_is_owned_by("order-1", "second")


def test_restore_claims_every_range_entry_order() -> None:
    coordinator = AccountExecutionCoordinator()
    trade = _trade("range")
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("90") + Decimal(index * 10),
            quantity=Decimal("1"),
            fraction=Decimal("0.25") if index != 1 else Decimal("0.50"),
            order_id=f"range-order-{index}",
            status="NEW",
        )
        for index in range(3)
    ]

    coordinator.restore_trade(trade, trading_mode="live")

    assert all(
        coordinator.order_is_owned_by(f"range-order-{index}", trade.trade_id)
        for index in range(3)
    )


@pytest.mark.asyncio
async def test_partial_range_fill_reserves_unfilled_legs_and_blocks_netting() -> None:
    coordinator = AccountExecutionCoordinator()
    trade = _trade("range", quantity="25")
    trade.entry_filled_quantity = Decimal("25")
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("90") + Decimal(index * 10),
            quantity=quantity,
            fraction=fraction,
            order_id=f"range-order-{index}",
            status="FILLED" if index == 0 else "NEW",
            executed_quantity=Decimal("25") if index == 0 else Decimal("0"),
            average_fill_price=Decimal("90") if index == 0 else Decimal("0"),
        )
        for index, (quantity, fraction) in enumerate(
            (
                (Decimal("25"), Decimal("0.25")),
                (Decimal("50"), Decimal("0.50")),
                (Decimal("25"), Decimal("0.25")),
            )
        )
    ]
    coordinator.register_trade(trade, trading_mode="live")

    assert coordinator.bucket_logical_quantity(
        trading_mode="live",
        symbol=trade.symbol,
        side=trade.side,
    ) == Decimal("100")
    opposite = _trade("opposite", side="short", quantity="10", stop="105")
    with pytest.raises(AccountExecutionConflictError, match="cancel it first"):
        await coordinator.coordinate_open(opposite, trading_mode="live")


def test_duplicate_durable_order_ownership_blocks_bucket() -> None:
    coordinator = AccountExecutionCoordinator()
    first = _trade("first")
    first.sl_order_id = "shared-stop"
    second = _trade("second", session_id="ls_two", channel_id="@two")
    second.sl_order_id = "shared-stop"

    coordinator.restore_trade(first, trading_mode="live")
    coordinator.restore_trade(second, trading_mode="live")

    reason = coordinator.bucket_block_reason(second, trading_mode="live")
    assert reason is not None
    assert "multiple logical owners" in reason


def test_restore_stopped_session_blocks_bucket_without_registering_stale_leg() -> None:
    coordinator = AccountExecutionCoordinator()
    stopped_trade = _trade("stopped", session_id="ls_stopped")
    store = MagicMock()
    store.list_sessions.return_value = [
        SimpleNamespace(session_id="ls_stopped", trading_mode="live", status="stopped")
    ]
    store.list_trades.return_value = [stopped_trade]

    coordinator.restore_from_store(store)

    assert coordinator.bucket_logical_quantity(
        trading_mode="live",
        symbol="BTCUSDT",
        side="long",
    ) == Decimal("0")
    reason = coordinator.bucket_block_reason(stopped_trade, trading_mode="live")
    assert reason is not None
    assert "stopped session" in reason


def test_restore_duplicate_legacy_fill_keeps_first_owner_without_poisoning_bucket() -> None:
    coordinator = AccountExecutionCoordinator()
    first = _trade("first")
    second = _trade("second", session_id="ls_two", channel_id="@two")
    first.processed_exchange_fill_ids = ["trade:legacy"]
    second.processed_exchange_fill_ids = ["trade:legacy"]

    coordinator.restore_trade(first, trading_mode="live")
    coordinator.restore_trade(second, trading_mode="live")

    assert coordinator.fill_owner("trade:legacy") == "first"
    assert coordinator.bucket_block_reason(second, trading_mode="live") is None


@pytest.mark.asyncio
async def test_account_coordination_dry_run_covers_core_policies() -> None:
    result = await run_account_coordination_dry_run()

    assert result["ok"] is True
    assert result["duplicate_action"] == "duplicate"
    assert result["opposite_action"] == "netted"
    assert result["primary_remaining_quantity"] == "0.6"
    assert result["aggregate_action"] == "open"
