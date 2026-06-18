"""Channel scoring and metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import ChannelMetrics, SimulatedTrade


@dataclass(frozen=True)
class ScoreBreakdown:
    final_score: Decimal
    profitability_score: Decimal
    win_rate_score: Decimal
    profit_factor_score: Decimal
    drawdown_control_score: Decimal
    fill_rate_score: Decimal
    consistency_score: Decimal
    sample_confidence_score: Decimal
    return_pct: Decimal
    drawdown_pct: Decimal
    fill_rate: Decimal
    consistency: Decimal
    sample_confidence: Decimal
    filled_trades: int
    total_trades: int
    wins: int
    losses: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "final_score": str(self.final_score),
            "profitability_score": str(self.profitability_score),
            "win_rate_score": str(self.win_rate_score),
            "profit_factor_score": str(self.profit_factor_score),
            "drawdown_control_score": str(self.drawdown_control_score),
            "fill_rate_score": str(self.fill_rate_score),
            "consistency_score": str(self.consistency_score),
            "sample_confidence_score": str(self.sample_confidence_score),
            "return_pct": str(self.return_pct),
            "drawdown_pct": str(self.drawdown_pct),
            "fill_rate": str(self.fill_rate),
            "consistency": str(self.consistency),
            "sample_confidence": str(self.sample_confidence),
            "filled_trades": self.filled_trades,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
        }


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
        initial_balance: Decimal = Decimal("1000"),
    ) -> tuple[ChannelMetrics, Decimal]:
        metrics, score, _ = self.score_with_breakdown(
            channel_id=channel_id,
            events=events,
            trades=trades,
            total_pnl=total_pnl,
            conservative_pnl=conservative_pnl,
            optimistic_pnl=optimistic_pnl,
            from_date=from_date,
            to_date=to_date,
            initial_balance=initial_balance,
        )
        return metrics, score

    def score_with_breakdown(
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
        initial_balance: Decimal = Decimal("1000"),
    ) -> tuple[ChannelMetrics, Decimal, ScoreBreakdown]:
        parsed_signals = sum(1 for e in events if e.action is SignalAction.OPEN)
        ignored = sum(1 for e in events if e.action is SignalAction.IGNORE)
        invalid = sum(1 for e in events if e.action is SignalAction.UNKNOWN)
        wins = sum(1 for t in trades if t.pnl > 0)
        filled_trades = [trade for trade in trades if trade.status != "not_filled"]
        gross_win = sum((t.pnl for t in filled_trades if t.pnl > 0), Decimal("0"))
        gross_loss = sum((-t.pnl for t in filled_trades if t.pnl < 0), Decimal("0"))
        win_rate = Decimal(wins) / Decimal(len(trades)) if trades else Decimal("0")
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
        expectancy = total_pnl / Decimal(len(trades)) if trades else Decimal("0")
        max_drawdown = _max_drawdown(trades)

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
        breakdown = self.build_score_breakdown(
            metrics=metrics,
            trades=trades,
            initial_balance=initial_balance,
        )
        return metrics, breakdown.final_score, breakdown

    def build_score_breakdown(
        self,
        *,
        metrics: ChannelMetrics,
        trades: list[SimulatedTrade],
        initial_balance: Decimal,
    ) -> ScoreBreakdown:
        total_trades = len(trades)
        filled_trades = [trade for trade in trades if trade.status != "not_filled"]
        filled_count = len(filled_trades)
        wins = sum(1 for trade in filled_trades if trade.pnl > 0)
        losses = sum(1 for trade in filled_trades if trade.pnl < 0)
        safe_initial = initial_balance if initial_balance > Decimal("0") else Decimal("1")
        parsed_signals = metrics.parsed_signals
        fill_rate = (
            Decimal(filled_count) / Decimal(parsed_signals)
            if parsed_signals > 0
            else Decimal("0")
        )
        return_pct = metrics.total_pnl / safe_initial
        drawdown_pct = metrics.max_drawdown / safe_initial
        pnl_reference = max(
            abs(metrics.optimistic_pnl),
            abs(metrics.conservative_pnl),
            Decimal("1"),
        )
        consistency = Decimal("1") - min(
            abs(metrics.optimistic_pnl - metrics.conservative_pnl) / pnl_reference,
            Decimal("1"),
        )
        sample_confidence = min(Decimal(filled_count) / Decimal("12"), Decimal("1"))

        profitability_score = _scale_clamped(return_pct, ceiling=Decimal("0.30"))
        win_rate_score = metrics.win_rate * Decimal("100")
        if metrics.profit_factor is None:
            if wins > 0 and losses == 0:
                profit_factor_score = Decimal("100")
            else:
                profit_factor_score = Decimal("0")
        else:
            profit_factor_score = _scale_clamped(metrics.profit_factor, ceiling=Decimal("3"))
        drawdown_control_score = (
            Decimal("1") - min(drawdown_pct / Decimal("0.20"), Decimal("1"))
        ) * Decimal("100")
        fill_rate_score = fill_rate * Decimal("100")
        consistency_score = consistency * Decimal("100")
        sample_confidence_score = sample_confidence * Decimal("100")

        weighted = (
            (profitability_score * Decimal("0.24"))
            + (win_rate_score * Decimal("0.18"))
            + (profit_factor_score * Decimal("0.17"))
            + (drawdown_control_score * Decimal("0.15"))
            + (fill_rate_score * Decimal("0.10"))
            + (consistency_score * Decimal("0.10"))
            + (sample_confidence_score * Decimal("0.06"))
        )
        confidence_multiplier = Decimal("0.70") + (sample_confidence * Decimal("0.30"))
        final_score = max(
            Decimal("0"),
            min(weighted * confidence_multiplier, Decimal("100")),
        )
        return ScoreBreakdown(
            final_score=final_score.quantize(Decimal("0.01")),
            profitability_score=profitability_score.quantize(Decimal("0.01")),
            win_rate_score=win_rate_score.quantize(Decimal("0.01")),
            profit_factor_score=profit_factor_score.quantize(Decimal("0.01")),
            drawdown_control_score=drawdown_control_score.quantize(Decimal("0.01")),
            fill_rate_score=fill_rate_score.quantize(Decimal("0.01")),
            consistency_score=consistency_score.quantize(Decimal("0.01")),
            sample_confidence_score=sample_confidence_score.quantize(Decimal("0.01")),
            return_pct=return_pct.quantize(Decimal("0.0001")),
            drawdown_pct=drawdown_pct.quantize(Decimal("0.0001")),
            fill_rate=fill_rate.quantize(Decimal("0.0001")),
            consistency=consistency.quantize(Decimal("0.0001")),
            sample_confidence=sample_confidence.quantize(Decimal("0.0001")),
            filled_trades=filled_count,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
        )


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


def _scale_clamped(value: Decimal, *, ceiling: Decimal) -> Decimal:
    if ceiling <= Decimal("0"):
        return Decimal("0")
    normalized = max(Decimal("0"), min(value / ceiling, Decimal("1")))
    return normalized * Decimal("100")
