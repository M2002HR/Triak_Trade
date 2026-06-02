"""Channel scoring and metrics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import ChannelMetrics, SimulatedTrade


class ChannelScorer:
    def score(
        self,
        *,
        channel_id: str,
        events: list[BacktestEvent],
        trades: list[SimulatedTrade],
        total_pnl: Decimal,
        conservative_pnl: Decimal,
        optimistic_pnl: Decimal,
        from_date: datetime,
        to_date: datetime,
    ) -> tuple[ChannelMetrics, Decimal]:
        parsed_signals = sum(1 for e in events if e.action is SignalAction.OPEN)
        ignored = sum(1 for e in events if e.action is SignalAction.IGNORE)
        invalid = sum(1 for e in events if e.action is SignalAction.UNKNOWN)
        wins = sum(1 for t in trades if t.pnl > 0)
        gross_win = sum((t.pnl for t in trades if t.pnl > 0), Decimal("0"))
        gross_loss = sum((-t.pnl for t in trades if t.pnl < 0), Decimal("0"))
        win_rate = Decimal(wins) / Decimal(len(trades)) if trades else Decimal("0")
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
        expectancy = total_pnl / Decimal(len(trades)) if trades else Decimal("0")
        max_drawdown = _max_drawdown(trades)
        consistency_penalty = abs(optimistic_pnl - conservative_pnl)

        score = Decimal("50")
        score += min(Decimal("20"), max(Decimal("0"), win_rate * Decimal("20")))
        if profit_factor is not None:
            score += min(Decimal("15"), profit_factor * Decimal("5"))
        score -= min(Decimal("20"), max_drawdown * Decimal("2"))
        score -= min(Decimal("10"), consistency_penalty / Decimal("10"))
        score = max(Decimal("0"), min(Decimal("100"), score))

        metrics = ChannelMetrics(
            channel_id=channel_id,
            from_date=from_date,
            to_date=to_date,
            total_messages=len(events),
            parsed_signals=parsed_signals,
            valid_signals=parsed_signals,
            ignored_messages=ignored,
            invalid_signals=invalid,
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            max_drawdown=max_drawdown,
            total_pnl=total_pnl,
            conservative_pnl=conservative_pnl,
            optimistic_pnl=optimistic_pnl,
            edit_delete_penalty=Decimal("0"),
        )
        return metrics, score


def _max_drawdown(trades: list[SimulatedTrade]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_dd = max(max_dd, drawdown)
    return max_dd
