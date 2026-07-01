from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.backtesting import BacktestEngine, BacktestRequest
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy
from triak_trade.backtesting.strategies.registry import load_strategy
from triak_trade.domain.enums import BacktestFillPolicy


def test_backtest_engine_run_fixture_path() -> None:
    req = BacktestRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
        initial_balance=Decimal("1000"),
        interval="1m",
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        risk_per_trade_pct=Decimal("1"),
        use_ai_classifier=False,
        use_regex_fallback=True,
        max_messages=100,
        symbols=None,
    )
    report = BacktestEngine().run(req)
    assert report.channel_id == "https://t.me/Tofan_Trade"
    assert report.final_balance >= Decimal("0")


def test_backtest_engine_loads_strategy_from_config_by_default() -> None:
    engine = BacktestEngine()
    assert isinstance(engine.strategy, DefaultRiskManagedStrategy)
    assert (
        engine.strategy.synthetic_stop_max_loss_pct_of_balance
        == load_strategy().synthetic_stop_max_loss_pct_of_balance
    )


def test_backtest_engine_emits_run_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="triak_trade.backtesting.engine")
    req = BacktestRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
        initial_balance=Decimal("1000"),
        interval="1m",
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        risk_per_trade_pct=Decimal("1"),
        use_ai_classifier=False,
        use_regex_fallback=True,
        max_messages=100,
        symbols=None,
    )

    BacktestEngine().run(req)

    messages = [record.message for record in caplog.records]
    assert "backtest_engine.run.started" in messages
    assert "backtest_engine.run_from_events.completed" in messages
