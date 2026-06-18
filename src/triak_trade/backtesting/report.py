"""Backtest report formatting."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from triak_trade.backtesting.scoring import ChannelScorer
from triak_trade.domain.models import BacktestReport


def report_to_json(report: BacktestReport, score: Decimal) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["channel_score"] = str(score)
    payload["score_breakdown"] = ChannelScorer().build_score_breakdown(
        metrics=report.metrics,
        trades=report.trades,
        initial_balance=report.initial_balance,
    ).as_dict()
    payload["trade_status_counts"] = _trade_status_counts(report)
    payload["symbol_summary"] = _symbol_summary(report)
    payload["equity_curve"] = _equity_curve(report)
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


def extract_channel_score(warnings: list[str]) -> Decimal:
    for warning in warnings:
        if warning.startswith("channel_score="):
            try:
                return Decimal(warning.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
        if warning.startswith("score="):
            try:
                return Decimal(warning.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
    return Decimal("0")


def _trade_status_counts(report: BacktestReport) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for trade in report.trades:
        counts[trade.status] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _symbol_summary(report: BacktestReport) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for trade in report.trades:
        item = summary.setdefault(
            trade.symbol,
            {
                "symbol": trade.symbol,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "not_filled": 0,
                "pnl": Decimal("0"),
            },
        )
        item["trades"] += 1
        item["pnl"] += trade.pnl
        if trade.status == "not_filled":
            item["not_filled"] += 1
        elif trade.pnl > 0:
            item["wins"] += 1
        elif trade.pnl < 0:
            item["losses"] += 1
    ranked = sorted(
        summary.values(),
        key=lambda item: (item["pnl"], item["trades"]),
        reverse=True,
    )
    return [
        {
            "symbol": item["symbol"],
            "trades": item["trades"],
            "wins": item["wins"],
            "losses": item["losses"],
            "not_filled": item["not_filled"],
            "pnl": str(item["pnl"]),
        }
        for item in ranked
    ]


def _equity_curve(report: BacktestReport) -> list[dict[str, Any]]:
    equity = report.initial_balance
    points: list[dict[str, Any]] = []
    for index, trade in enumerate(report.trades, start=1):
        equity += trade.pnl
        points.append(
            {
                "index": index,
                "signal_id": trade.signal_id,
                "symbol": trade.symbol,
                "status": trade.status,
                "pnl": str(trade.pnl),
                "equity": str(equity),
                "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
            }
        )
    return points
