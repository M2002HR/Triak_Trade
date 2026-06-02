from __future__ import annotations

from decimal import Decimal

from triak_trade.backtesting.engine import run_fixture_backtest


def test_report_json_and_telegram_summary() -> None:
    report_json, summary = run_fixture_backtest()
    assert "channel_score" in report_json
    assert "Backtest Report" in summary
    assert "Win rate" in summary
    assert "Score:" in summary
    assert "replace_me" not in summary
    assert isinstance(Decimal(report_json["channel_score"]), Decimal)
