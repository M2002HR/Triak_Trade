"""Backtest report formatting."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any

from triak_trade.backtesting.scoring import ChannelScorer
from triak_trade.core.formatting import format_decimal
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
    payload["period_pnl"] = _period_pnl(report)
    payload["trade_outcome_summary"] = _trade_outcome_summary(report)
    payload["per_signal_summary"] = _per_signal_summary(report)
    payload["comparison_profile"] = _comparison_profile(report)
    return payload


def report_to_telegram_summary(report: BacktestReport, score: Decimal) -> str:
    metrics = report.metrics
    interval = report.interval
    profit_factor_line = _telegram_profit_factor_line(metrics.profit_factor)
    return (
        "📊 Backtest Report\n\n"
        f"Channel: {report.channel_id}\n"
        f"Range: {report.from_date.date()} → {report.to_date.date()}\n"
        f"Interval: {interval}\n"
        f"Initial Balance: {format_decimal(report.initial_balance)} USDT\n"
        f"Final Balance: {format_decimal(report.final_balance)} USDT\n\n"
        "Signals:\n"
        f"• Messages: {metrics.total_messages}\n"
        f"• Parsed signals: {metrics.parsed_signals}\n"
        f"• Valid signals: {metrics.valid_signals}\n"
        f"• Trades filled: {sum(1 for t in report.trades if t.status != 'not_filled')}\n\n"
        "Performance:\n"
        f"• PnL: {format_decimal(metrics.total_pnl)}\n"
        f"• Win rate: {(metrics.win_rate * Decimal('100')).quantize(Decimal('0.1'))}%\n"
        f"{profit_factor_line}"
        f"• Max drawdown: {format_decimal(metrics.max_drawdown)}\n"
        f"• Conservative PnL: {format_decimal(metrics.conservative_pnl)}\n"
        f"• Optimistic PnL: {format_decimal(metrics.optimistic_pnl)}\n\n"
        f"Score: {score.quantize(Decimal('1'))}/100"
    )


def _telegram_profit_factor_line(value: Decimal | None) -> str:
    if value is None:
        return "• Profit factor: ∞\n"
    return f"• Profit factor: {format_decimal(value)}\n"


def report_to_markdown_summary(report: BacktestReport, score: Decimal) -> str:
    metrics = report.metrics
    outcome_summary = _trade_outcome_summary(report)
    comparison_profile = _comparison_profile(report)
    return "\n".join(
        [
            "# Backtest Report",
            "",
            f"- Channel: `{report.channel_id}`",
            f"- Range: `{report.from_date.isoformat()} -> {report.to_date.isoformat()}`",
            f"- Initial Balance: `{format_decimal(report.initial_balance)}`",
            f"- Final Balance: `{format_decimal(report.final_balance)}`",
            f"- Parsed Signals: `{metrics.parsed_signals}`",
            f"- Valid Signals: `{metrics.valid_signals}`",
            f"- Total PnL: `{format_decimal(metrics.total_pnl)}`",
            f"- Win Rate: `{format_decimal(metrics.win_rate)}`",
            f"- Profit Factor: `{format_decimal(metrics.profit_factor)}`",
            f"- Max Drawdown: `{format_decimal(metrics.max_drawdown)}`",
            f"- Conservative PnL: `{format_decimal(metrics.conservative_pnl)}`",
            f"- Optimistic PnL: `{format_decimal(metrics.optimistic_pnl)}`",
            f"- Score: `{format_decimal(score)}`",
            "",
            "## Comparison Metrics",
            "",
            f"- Fill Rate: `{comparison_profile['fill_rate_pct']}%`",
            f"- PnL Per Valid Signal: `{comparison_profile['pnl_per_valid_signal']}`",
            f"- PnL Per Filled Trade: `{comparison_profile['pnl_per_filled_trade']}`",
            f"- Best Trade: `{outcome_summary['best_trade_pnl']}`",
            f"- Worst Trade: `{outcome_summary['worst_trade_pnl']}`",
            f"- Average Filled PnL: `{outcome_summary['avg_filled_pnl']}`",
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
            "pnl": format_decimal(item["pnl"]),
        }
        for item in ranked
    ]


def _equity_curve(report: BacktestReport) -> list[dict[str, Any]]:
    equity = report.initial_balance
    points: list[dict[str, Any]] = []
    # Sort chronologically so the curve reflects real time progression.
    # Trades without exit_time sort to the end (they contribute zero net change).
    sorted_trades = sorted(
        report.trades,
        key=lambda t: t.exit_time or datetime(9999, 12, 31, tzinfo=timezone.utc),
    )
    for index, trade in enumerate(sorted_trades, start=1):
        equity += trade.pnl
        points.append(
            {
                "index": index,
                "signal_id": trade.signal_id,
                "symbol": trade.symbol,
                "status": trade.status,
                "pnl": format_decimal(trade.pnl),
                "equity": format_decimal(equity),
                "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
            }
        )
    return points


def _period_pnl(report: BacktestReport) -> dict[str, list[dict[str, Any]]]:
    closed_trades = [trade for trade in report.trades if trade.exit_time is not None]
    return {
        "daily": _bucket_pnl(closed_trades, "%Y-%m-%d"),
        "weekly": _bucket_pnl(closed_trades, "%G-W%V"),
        "monthly": _bucket_pnl(closed_trades, "%Y-%m"),
    }


def _bucket_pnl(trades: list[Any], pattern: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        assert trade.exit_time is not None
        key = trade.exit_time.astimezone(timezone.utc).strftime(pattern)
        bucket = buckets.setdefault(
            key,
            {
                "period": key,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": Decimal("0"),
            },
        )
        bucket["trades"] += 1
        bucket["pnl"] += trade.pnl
        if trade.pnl > 0:
            bucket["wins"] += 1
        elif trade.pnl < 0:
            bucket["losses"] += 1
    return [
        {
            "period": item["period"],
            "trades": item["trades"],
            "wins": item["wins"],
            "losses": item["losses"],
            "pnl": format_decimal(item["pnl"]),
        }
        for item in sorted(buckets.values(), key=lambda row: row["period"])
    ]


def _trade_outcome_summary(report: BacktestReport) -> dict[str, Any]:
    trades = list(report.trades)
    filled = [trade for trade in trades if trade.status != "not_filled"]
    filled_pnls = [trade.pnl for trade in filled]
    all_pnls = [trade.pnl for trade in trades]
    holding_seconds = [
        Decimal(str((trade.exit_time - trade.entry_time).total_seconds()))
        for trade in filled
        if trade.entry_time is not None and trade.exit_time is not None
    ]
    best_trade = max(filled, key=lambda item: item.pnl, default=None)
    worst_trade = min(filled, key=lambda item: item.pnl, default=None)
    return {
        "total_trades": len(trades),
        "filled_trades": len(filled),
        "not_filled_trades": sum(1 for trade in trades if trade.status == "not_filled"),
        "avg_trade_pnl": _avg_decimal(all_pnls),
        "avg_filled_pnl": _avg_decimal(filled_pnls),
        "median_filled_pnl": _median_decimal(filled_pnls),
        "best_trade_id": best_trade.trade_id if best_trade is not None else None,
        "best_trade_symbol": best_trade.symbol if best_trade is not None else None,
        "best_trade_pnl": format_decimal(best_trade.pnl) if best_trade else "0",
        "worst_trade_id": worst_trade.trade_id if worst_trade is not None else None,
        "worst_trade_symbol": worst_trade.symbol if worst_trade is not None else None,
        "worst_trade_pnl": format_decimal(worst_trade.pnl) if worst_trade else "0",
        "avg_holding_seconds": _avg_decimal(holding_seconds),
        "median_holding_seconds": _median_decimal(holding_seconds),
    }


def _per_signal_summary(report: BacktestReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, trade in enumerate(
        sorted(
            report.trades,
            key=lambda item: item.entry_time or item.exit_time or report.generated_at,
        ),
        start=1,
    ):
        rows.append(
            {
                "index": index,
                "signal_id": trade.signal_id,
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "side": str(trade.side.value if hasattr(trade.side, "value") else trade.side),
                "status": trade.status,
                "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
                "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
                "entry_price": format_decimal(trade.entry_price) if trade.entry_price else None,
                "exit_price": format_decimal(trade.exit_price) if trade.exit_price else None,
                "quantity": format_decimal(trade.quantity),
                "pnl": format_decimal(trade.pnl),
                "pnl_pct": format_decimal(trade.pnl_pct),
                "fees": format_decimal(trade.fees),
                "notes": list(trade.notes),
            }
        )
    return rows


def _comparison_profile(report: BacktestReport) -> dict[str, Any]:
    total_trades = len(report.trades)
    filled_trades = sum(1 for trade in report.trades if trade.status != "not_filled")
    valid_signals = report.metrics.valid_signals
    fill_rate = (
        Decimal(filled_trades) / Decimal(total_trades) * Decimal("100")
        if total_trades
        else Decimal("0")
    )
    pnl_per_valid_signal = (
        report.metrics.total_pnl / Decimal(valid_signals)
        if valid_signals
        else Decimal("0")
    )
    pnl_per_filled_trade = (
        report.metrics.total_pnl / Decimal(filled_trades)
        if filled_trades
        else Decimal("0")
    )
    risk_adjusted_return = (
        report.metrics.total_pnl / abs(report.metrics.max_drawdown)
        if report.metrics.max_drawdown != 0
        else None
    )
    return {
        "fill_rate_pct": format_decimal(fill_rate),
        "pnl_per_valid_signal": format_decimal(pnl_per_valid_signal),
        "pnl_per_filled_trade": format_decimal(pnl_per_filled_trade),
        "risk_adjusted_return": (
            format_decimal(risk_adjusted_return)
            if risk_adjusted_return is not None
            else None
        ),
        "messages_per_valid_signal": format_decimal(
            Decimal(report.metrics.total_messages) / Decimal(valid_signals)
            if valid_signals
            else Decimal("0")
        ),
        "valid_signal_rate_pct": format_decimal(
            Decimal(valid_signals) / Decimal(report.metrics.total_messages) * Decimal("100")
            if report.metrics.total_messages
            else Decimal("0")
        ),
    }


def _avg_decimal(values: list[Decimal]) -> str:
    if not values:
        return "0"
    return format_decimal(sum(values, Decimal("0")) / Decimal(len(values))) or "0"


def _median_decimal(values: list[Decimal]) -> str:
    if not values:
        return "0"
    return format_decimal(Decimal(str(median(values)))) or "0"
