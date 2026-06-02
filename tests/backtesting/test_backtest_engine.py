from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.backtesting import BacktestEngine, BacktestRequest
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
