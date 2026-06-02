"""Backtest report formatting."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from triak_trade.domain.models import BacktestReport


def report_to_json(report: BacktestReport, score: Decimal) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["channel_score"] = str(score)
    return payload


def report_to_telegram_summary(report: BacktestReport, score: Decimal) -> str:
    metrics = report.metrics
    interval = "n/a"
    if report.trades and report.trades[0].notes:
        interval = report.trades[0].notes[0]
    return (
        "📊 Backtest Report\n\n"
        f"Channel: {report.channel_id}\n"
        f"Range: {report.from_date.date()} → {report.to_date.date()}\n"
        f"Interval: {interval}\n"
        f"Initial Balance: {report.initial_balance} USDT\n"
        f"Final Balance: {report.final_balance} USDT\n\n"
        "Signals:\n"
        f"• Messages: {metrics.total_messages}\n"
        f"• Parsed signals: {metrics.parsed_signals}\n"
        f"• Valid signals: {metrics.valid_signals}\n"
        f"• Trades filled: {sum(1 for t in report.trades if t.status != 'not_filled')}\n\n"
        "Performance:\n"
        f"• PnL: {metrics.total_pnl}\n"
        f"• Win rate: {(metrics.win_rate * Decimal('100')).quantize(Decimal('0.1'))}%\n"
        f"• Profit factor: {metrics.profit_factor}\n"
        f"• Max drawdown: {metrics.max_drawdown}\n"
        f"• Conservative PnL: {metrics.conservative_pnl}\n"
        f"• Optimistic PnL: {metrics.optimistic_pnl}\n\n"
        f"Score: {score.quantize(Decimal('1'))}/100"
    )


def report_to_markdown_summary(report: BacktestReport, score: Decimal) -> str:
    metrics = report.metrics
    return "\n".join(
        [
            "# Backtest Report",
            "",
            f"- Channel: `{report.channel_id}`",
            f"- Range: `{report.from_date.isoformat()} -> {report.to_date.isoformat()}`",
            f"- Initial Balance: `{report.initial_balance}`",
            f"- Final Balance: `{report.final_balance}`",
            f"- Parsed Signals: `{metrics.parsed_signals}`",
            f"- Valid Signals: `{metrics.valid_signals}`",
            f"- Total PnL: `{metrics.total_pnl}`",
            f"- Win Rate: `{metrics.win_rate}`",
            f"- Profit Factor: `{metrics.profit_factor}`",
            f"- Max Drawdown: `{metrics.max_drawdown}`",
            f"- Conservative PnL: `{metrics.conservative_pnl}`",
            f"- Optimistic PnL: `{metrics.optimistic_pnl}`",
            f"- Score: `{score}`",
        ]
    )
