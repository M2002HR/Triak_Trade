"""Tests for trade strategy module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy
from triak_trade.backtesting.strategies.registry import load_strategy_from_dict
from triak_trade.backtesting.strategies.trailing_tp import TrailingTakeProfitStrategy
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import Candle, ParsedSignal

# ------------------------------------------------------------------ #
# DefaultRiskManagedStrategy — synthetic stop                          #
# ------------------------------------------------------------------ #


class TestGetSyntheticStop:
    def test_long_stop_respects_balance_loss_budget(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_stop_max_loss_pct_of_balance=Decimal("5")
        )
        stop = strategy.get_synthetic_stop(
            side=TradeSide.LONG,
            entry_price=Decimal("100"),
            balance_at_entry=Decimal("100"),
            quantity=Decimal("0.2"),
            fee_rate_pct=Decimal("0"),
        )
        assert stop == Decimal("75")

    def test_short_stop_respects_balance_loss_budget(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_stop_max_loss_pct_of_balance=Decimal("5")
        )
        stop = strategy.get_synthetic_stop(
            side=TradeSide.SHORT,
            entry_price=Decimal("100"),
            balance_at_entry=Decimal("100"),
            quantity=Decimal("0.2"),
            fee_rate_pct=Decimal("0"),
        )
        assert stop == Decimal("125")

    def test_fee_only_budget_collapse_returns_entry(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_stop_max_loss_pct_of_balance=Decimal("5")
        )
        stop = strategy.get_synthetic_stop(
            side=TradeSide.LONG,
            entry_price=Decimal("100"),
            balance_at_entry=Decimal("100"),
            quantity=Decimal("1.25"),
            fee_rate_pct=Decimal("2"),
        )
        assert stop == Decimal("100")


# ------------------------------------------------------------------ #
# DefaultRiskManagedStrategy — TP target actions                       #
# ------------------------------------------------------------------ #


class TestGetTargetHitAction:
    def setup_method(self):
        self.strategy = DefaultRiskManagedStrategy(
            risk_free_on_first_tp=True,
            tp_close_fractions=[Decimal("0.35"), Decimal("0.40"), Decimal("0.50")],
        )

    def test_last_target_always_closes_all(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=1,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110")],
        )
        assert action.close_fraction == Decimal("1")

    def test_last_target_closes_all_even_with_many_hit_before(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=5,
            remaining_targets_including_this=1,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110")],
        )
        assert action.close_fraction == Decimal("1")

    def test_first_tp_uses_first_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=4,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130"), Decimal("140")],
        )
        assert action.close_fraction == Decimal("0.35")

    def test_second_tp_uses_second_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=1,
            remaining_targets_including_this=3,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130")],
        )
        assert action.close_fraction == Decimal("0.40")

    def test_third_tp_uses_third_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=2,
            remaining_targets_including_this=2,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130")],
        )
        assert action.close_fraction == Decimal("0.50")

    def test_fourth_tp_repeats_last_fraction_when_not_last(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=3,
            remaining_targets_including_this=2,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130"), Decimal("140")],
        )
        # Index 3 exceeds list length (3), so last entry (0.50) is used
        assert action.close_fraction == Decimal("0.50")

    def test_risk_free_triggered_on_first_tp(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=3,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130")],
        )
        assert action.move_sl_to_entry is True

    def test_risk_free_not_triggered_after_first_tp(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=1,
            remaining_targets_including_this=2,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120")],
        )
        assert action.move_sl_to_entry is False

    def test_risk_free_disabled(self):
        strategy = DefaultRiskManagedStrategy(risk_free_on_first_tp=False)
        action = strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=3,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130")],
        )
        assert action.move_sl_to_entry is False

    def test_empty_fractions_uses_equal_split(self):
        strategy = DefaultRiskManagedStrategy(tp_close_fractions=[])
        action = strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=4,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130"), Decimal("140")],
        )
        assert action.close_fraction == Decimal("1") / Decimal("4")


class TestTrailingTakeProfitStrategy:
    def test_second_target_moves_stop_to_first_target(self):
        strategy = TrailingTakeProfitStrategy()
        action = strategy.get_target_hit_action(
            targets_hit_so_far=1,
            remaining_targets_including_this=3,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130"), Decimal("140")],
        )
        assert action.new_stop_loss == Decimal("110")
        assert action.move_sl_to_entry is False

    def test_third_target_moves_stop_to_second_target(self):
        strategy = TrailingTakeProfitStrategy()
        action = strategy.get_target_hit_action(
            targets_hit_so_far=2,
            remaining_targets_including_this=2,
            entry_price=Decimal("100"),
            take_profits=[Decimal("110"), Decimal("120"), Decimal("130")],
        )
        assert action.new_stop_loss == Decimal("120")


class TestSyntheticTakeProfits:
    def test_long_synthetic_targets_use_notional_profit_steps(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_tp_profit_pct_steps=[Decimal("2"), Decimal("4"), Decimal("6")]
        )
        targets = strategy.get_synthetic_take_profits(
            side=TradeSide.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            notional_value=Decimal("120"),
        )
        assert targets == [Decimal("102"), Decimal("104"), Decimal("106")]

    def test_short_synthetic_targets_use_notional_profit_steps(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_tp_profit_pct_steps=[Decimal("2"), Decimal("4")]
        )
        targets = strategy.get_synthetic_take_profits(
            side=TradeSide.SHORT,
            entry_price=Decimal("100"),
            stop_loss=Decimal("105"),
            notional_value=Decimal("120"),
        )
        assert targets == [Decimal("98"), Decimal("96")]

    def test_sell_side_treated_same_as_short(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_tp_profit_pct_steps=[Decimal("2"), Decimal("4")]
        )
        targets = strategy.get_synthetic_take_profits(
            side=TradeSide.SELL,
            entry_price=Decimal("100"),
            stop_loss=Decimal("105"),
            notional_value=Decimal("120"),
        )
        assert targets == [Decimal("98"), Decimal("96")]

    def test_buy_side_treated_same_as_long(self):
        strategy = DefaultRiskManagedStrategy(
            synthetic_tp_profit_pct_steps=[Decimal("2"), Decimal("4"), Decimal("6")]
        )
        targets = strategy.get_synthetic_take_profits(
            side=TradeSide.BUY,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            notional_value=Decimal("120"),
        )
        assert targets == [Decimal("102"), Decimal("104"), Decimal("106")]


class TestTradeSideProperties:
    def test_short_is_short(self):
        assert TradeSide.SHORT.is_short is True
        assert TradeSide.SHORT.is_long is False

    def test_sell_is_short(self):
        assert TradeSide.SELL.is_short is True
        assert TradeSide.SELL.is_long is False

    def test_long_is_long(self):
        assert TradeSide.LONG.is_long is True
        assert TradeSide.LONG.is_short is False

    def test_buy_is_long(self):
        assert TradeSide.BUY.is_long is True
        assert TradeSide.BUY.is_short is False

    def test_unknown_is_neither(self):
        assert TradeSide.UNKNOWN.is_short is False
        assert TradeSide.UNKNOWN.is_long is False


# ------------------------------------------------------------------ #
# Protocol compliance                                                  #
# ------------------------------------------------------------------ #


def test_default_risk_implements_protocol():
    strategy = DefaultRiskManagedStrategy()
    assert isinstance(strategy, TradeStrategy)

    trailing = TrailingTakeProfitStrategy()
    assert isinstance(trailing, TradeStrategy)


# ------------------------------------------------------------------ #
# Registry / config loader                                             #
# ------------------------------------------------------------------ #


class TestLoadStrategyFromDict:
    def test_default_config_loads(self):
        cfg = {
            "active_strategy": "default_risk_managed",
            "strategies": {
                "default_risk_managed": {
                    "synthetic_stop_max_loss_pct_of_balance": "5",
                    "risk_free_on_first_tp": True,
                    "tp_close_fractions": ["0.35", "0.40", "0.50"],
                    "synthetic_tp_profit_pct_steps": ["2", "4", "6", "8", "10"],
                }
            },
        }
        strategy = load_strategy_from_dict(cfg)
        assert isinstance(strategy, DefaultRiskManagedStrategy)
        assert strategy.synthetic_stop_max_loss_pct_of_balance == Decimal("5")
        assert strategy.risk_free_on_first_tp is True
        assert strategy.tp_close_fractions == [
            Decimal("0.35"),
            Decimal("0.40"),
            Decimal("0.50"),
        ]
        assert strategy.synthetic_tp_profit_pct_steps == [
            Decimal("2"),
            Decimal("4"),
            Decimal("6"),
            Decimal("8"),
            Decimal("10"),
        ]

    def test_unknown_strategy_raises(self):
        cfg = {"active_strategy": "nonexistent"}
        with pytest.raises(ValueError, match="Unknown strategy"):
            load_strategy_from_dict(cfg)

    def test_missing_active_defaults_to_default_risk_managed(self):
        # When active_strategy key is absent, defaults to default_risk_managed
        strategy = load_strategy_from_dict({})
        assert isinstance(strategy, DefaultRiskManagedStrategy)

    def test_trailing_strategy_loads(self):
        cfg = {
            "active_strategy": "tp_trailing_risk_managed",
            "strategies": {
                "tp_trailing_risk_managed": {
                    "synthetic_stop_max_loss_pct_of_balance": "7",
                    "risk_free_on_first_tp": True,
                    "tp_close_fractions": ["0.35", "0.40", "0.50"],
                    "synthetic_tp_profit_pct_steps": ["2", "4", "6"],
                }
            },
        }
        strategy = load_strategy_from_dict(cfg)
        assert isinstance(strategy, TrailingTakeProfitStrategy)
        assert strategy.synthetic_stop_max_loss_pct_of_balance == Decimal("7")


# ------------------------------------------------------------------ #
# Integration: simulator uses strategy fractions and risk-free         #
# ------------------------------------------------------------------ #


def _make_signal(
    side=TradeSide.LONG,
    stop_loss=Decimal("49000"),
    take_profits=None,
) -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=side,
        entry_type=EntryType.MARKET,
        entry_low=Decimal("50000"),
        entry_high=Decimal("50000"),
        stop_loss=stop_loss,
        take_profits=take_profits or [Decimal("51000"), Decimal("52000"), Decimal("53000")],
        leverage=1,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="test",
        source_message_id=1,
        parser_version="test",
    )


def _make_candle(t, open_, high, low, close, symbol="BTCUSDT"):
    return Candle(
        symbol=symbol,
        interval="1h",
        open_time=t,
        close_time=t + timedelta(hours=1),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
        source=CandleSource.FIXTURE,
    )


def _make_open_event(base, signal, signal_id="sig1"):
    return BacktestEvent(
        timestamp=base,
        action=SignalAction.OPEN,
        signal_id=signal_id,
        related_signal_id=None,
        parsed_signal=signal,
        source_message_id=1,
        close_fraction=None,
        move_stop_to_entry=False,
        leverage=None,
        debug_notes=[],
    )


def test_simulator_uses_strategy_tp_fractions():
    """Strategy fractions override equal-split TP logic."""

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(take_profits=[Decimal("51000"), Decimal("52000"), Decimal("53000")])
    event = _make_open_event(base, signal)

    candles = [
        _make_candle(base, 50000, 50100, 49900, 50050),
        _make_candle(base + timedelta(hours=1), 50100, 51200, 50000, 51100),  # TP1
        _make_candle(base + timedelta(hours=2), 51100, 52200, 51000, 52100),  # TP2
        _make_candle(base + timedelta(hours=3), 52100, 53200, 52000, 53100),  # TP3 (final)
    ]

    strategy = DefaultRiskManagedStrategy(
        risk_free_on_first_tp=True,
        tp_close_fractions=[Decimal("0.35"), Decimal("0.40"), Decimal("0.50")],
    )
    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[event],
        candles=candles,
        initial_balance=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        strategy=strategy,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.status == "partial_tp_complete"
    assert trade.pnl > Decimal("0")
    assert "stop_loss_moved_to_entry_by_strategy" in " ".join(trade.notes)


def test_simulator_strategy_risk_free_prevents_loss():
    """After first TP, SL moves to entry; subsequent SL hit yields breakeven."""

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(take_profits=[Decimal("51000"), Decimal("52000")])
    event = _make_open_event(base, signal)

    candles = [
        _make_candle(base, 50000, 50100, 49900, 50050),                        # entry
        _make_candle(base + timedelta(hours=1), 50100, 51200, 50000, 51100),   # TP1, SL→entry
        _make_candle(base + timedelta(hours=2), 50100, 50500, 49500, 49600),   # SL@entry hit
    ]

    strategy = DefaultRiskManagedStrategy(risk_free_on_first_tp=True)
    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[event],
        candles=candles,
        initial_balance=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        strategy=strategy,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.status == "partial_tp_then_sl"
    # Remaining portion closed at entry → net profit from TP1 hit
    assert trade.pnl > Decimal("0"), "partial profit from TP1 should make trade net-positive"


def test_simulator_trailing_strategy_moves_stop_to_previous_target() -> None:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(take_profits=[Decimal("51000"), Decimal("52000"), Decimal("53000")])
    event = _make_open_event(base, signal)

    candles = [
        _make_candle(base, 50000, 50100, 49900, 50050),
        _make_candle(base + timedelta(hours=1), 50100, 51200, 50000, 51100),   # TP1
        _make_candle(base + timedelta(hours=2), 51100, 52200, 51050, 52100),   # TP2 => SL to TP1
        _make_candle(
            base + timedelta(hours=3), 52100, 52150, 50900, 51020
        ),   # hits moved SL at TP1
    ]

    strategy = TrailingTakeProfitStrategy(risk_free_on_first_tp=True)
    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[event],
        candles=candles,
        initial_balance=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        strategy=strategy,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.status == "partial_tp_then_sl"
    assert "stop_loss_moved_to_target_by_strategy=51000" in " ".join(trade.notes)


def test_simulator_sell_side_treated_as_short():
    """Regression: TradeSide.SELL must behave identically to TradeSide.SHORT.

    Previously, the simulator compared `side is TradeSide.SHORT` which failed
    for SELL, causing direction filters, synthetic TPs, SL hit logic, and PnL
    calculations all to use LONG logic for a SELL signal.
    """
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(
        side=TradeSide.SELL,
        stop_loss=Decimal("105"),
        take_profits=[Decimal("95"), Decimal("90")],
    )
    event = _make_open_event(base, signal)

    candles = [
        _make_candle(base, 100, 102, 98, 100),
        _make_candle(base + timedelta(hours=1), 100, 101, 94, 95),
        _make_candle(base + timedelta(hours=2), 95, 96, 88, 89),
    ]

    strategy = DefaultRiskManagedStrategy(risk_free_on_first_tp=False)
    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[event],
        candles=candles,
        initial_balance=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        strategy=strategy,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.pnl > Decimal("0"), f"SELL TPs should profit; got pnl={trade.pnl}"
    assert trade.status in {"partial_tp_complete", "tp_hit", "tp_hit_same_candle"}


def test_simulator_sell_sl_hit_is_loss():
    """A SELL signal whose SL (above entry) is hit must produce a loss, not a gain."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(
        side=TradeSide.SELL,
        stop_loss=Decimal("105"),
        take_profits=[Decimal("90")],
    )
    event = _make_open_event(base, signal)

    candles = [
        _make_candle(base, 100, 102, 98, 100),
        _make_candle(base + timedelta(hours=1), 102, 107, 101, 106),
    ]

    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[event],
        candles=candles,
        initial_balance=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.pnl < Decimal("0"), f"SELL SL hit should be a loss; got pnl={trade.pnl}"
    assert trade.status in {"sl_hit", "sl_hit_same_candle"}
