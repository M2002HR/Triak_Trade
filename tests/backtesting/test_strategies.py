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
    def test_long_100pct_stop_is_zero(self):
        strategy = DefaultRiskManagedStrategy(no_sl_loss_pct=Decimal("100"))
        stop = strategy.get_synthetic_stop(side=TradeSide.LONG, entry_price=Decimal("50000"))
        assert stop == Decimal("0")

    def test_long_50pct_stop_halves_entry(self):
        strategy = DefaultRiskManagedStrategy(no_sl_loss_pct=Decimal("50"))
        stop = strategy.get_synthetic_stop(side=TradeSide.LONG, entry_price=Decimal("1000"))
        assert stop == Decimal("500")

    def test_short_100pct_stop_doubles_entry(self):
        strategy = DefaultRiskManagedStrategy(no_sl_loss_pct=Decimal("100"))
        stop = strategy.get_synthetic_stop(side=TradeSide.SHORT, entry_price=Decimal("1000"))
        assert stop == Decimal("2000")

    def test_short_50pct_stop(self):
        strategy = DefaultRiskManagedStrategy(no_sl_loss_pct=Decimal("50"))
        stop = strategy.get_synthetic_stop(side=TradeSide.SHORT, entry_price=Decimal("1000"))
        assert stop == Decimal("1500")

    def test_long_stop_never_negative(self):
        strategy = DefaultRiskManagedStrategy(no_sl_loss_pct=Decimal("200"))
        stop = strategy.get_synthetic_stop(side=TradeSide.LONG, entry_price=Decimal("100"))
        assert stop == Decimal("0")


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
        )
        assert action.close_fraction == Decimal("1")

    def test_last_target_closes_all_even_with_many_hit_before(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=5,
            remaining_targets_including_this=1,
        )
        assert action.close_fraction == Decimal("1")

    def test_first_tp_uses_first_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=4,
        )
        assert action.close_fraction == Decimal("0.35")

    def test_second_tp_uses_second_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=1,
            remaining_targets_including_this=3,
        )
        assert action.close_fraction == Decimal("0.40")

    def test_third_tp_uses_third_fraction(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=2,
            remaining_targets_including_this=2,
        )
        assert action.close_fraction == Decimal("0.50")

    def test_fourth_tp_repeats_last_fraction_when_not_last(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=3,
            remaining_targets_including_this=2,
        )
        # Index 3 exceeds list length (3), so last entry (0.50) is used
        assert action.close_fraction == Decimal("0.50")

    def test_risk_free_triggered_on_first_tp(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=3,
        )
        assert action.move_sl_to_entry is True

    def test_risk_free_not_triggered_after_first_tp(self):
        action = self.strategy.get_target_hit_action(
            targets_hit_so_far=1,
            remaining_targets_including_this=2,
        )
        assert action.move_sl_to_entry is False

    def test_risk_free_disabled(self):
        strategy = DefaultRiskManagedStrategy(risk_free_on_first_tp=False)
        action = strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=3,
        )
        assert action.move_sl_to_entry is False

    def test_empty_fractions_uses_equal_split(self):
        strategy = DefaultRiskManagedStrategy(tp_close_fractions=[])
        action = strategy.get_target_hit_action(
            targets_hit_so_far=0,
            remaining_targets_including_this=4,
        )
        assert action.close_fraction == Decimal("1") / Decimal("4")


# ------------------------------------------------------------------ #
# Protocol compliance                                                  #
# ------------------------------------------------------------------ #


def test_default_risk_implements_protocol():
    strategy = DefaultRiskManagedStrategy()
    assert isinstance(strategy, TradeStrategy)


# ------------------------------------------------------------------ #
# Registry / config loader                                             #
# ------------------------------------------------------------------ #


class TestLoadStrategyFromDict:
    def test_default_config_loads(self):
        cfg = {
            "active_strategy": "default_risk_managed",
            "strategies": {
                "default_risk_managed": {
                    "no_sl_loss_pct": "100",
                    "risk_free_on_first_tp": True,
                    "tp_close_fractions": ["0.35", "0.40", "0.50"],
                }
            },
        }
        strategy = load_strategy_from_dict(cfg)
        assert isinstance(strategy, DefaultRiskManagedStrategy)
        assert strategy.no_sl_loss_pct == Decimal("100")
        assert strategy.risk_free_on_first_tp is True
        assert strategy.tp_close_fractions == [
            Decimal("0.35"),
            Decimal("0.40"),
            Decimal("0.50"),
        ]

    def test_unknown_strategy_raises(self):
        cfg = {"active_strategy": "nonexistent"}
        with pytest.raises(ValueError, match="Unknown strategy"):
            load_strategy_from_dict(cfg)

    def test_missing_active_defaults_to_default_risk_managed(self):
        # When active_strategy key is absent, defaults to default_risk_managed
        strategy = load_strategy_from_dict({})
        assert isinstance(strategy, DefaultRiskManagedStrategy)


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
