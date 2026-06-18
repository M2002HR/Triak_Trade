"""Backtest engine orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from triak_trade.agents.classifier import MessageClassifier, RegexMessageClassifier
from triak_trade.backtesting.fixtures import fixture_candles, fixture_messages
from triak_trade.backtesting.models import BacktestEvent, BacktestRequest
from triak_trade.backtesting.report import (
    extract_channel_score,
    report_to_json,
    report_to_telegram_summary,
)
from triak_trade.backtesting.scoring import ChannelScorer
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.backtesting.timeline import BacktestTimelineBuilder
from triak_trade.domain.enums import BacktestFillPolicy
from triak_trade.domain.models import BacktestReport, Candle, RawTelegramMessage


class BacktestEngine:
    def __init__(
        self,
        *,
        classifier: MessageClassifier | None = None,
        strategy: TradeStrategy | None = None,
    ) -> None:
        self.classifier = classifier or RegexMessageClassifier()
        self.simulator = BacktestSimulator()
        self.scorer = ChannelScorer()
        self.strategy = strategy

    def run(self, request: BacktestRequest) -> BacktestReport:
        messages = fixture_messages(request.channel)
        candles = fixture_candles(interval=request.interval)
        return self.run_from_messages(request=request, messages=messages, candles=candles)

    def build_events(
        self,
        *,
        channel_id: str,
        messages: list[RawTelegramMessage],
    ) -> list[BacktestEvent]:
        timeline = BacktestTimelineBuilder(classifier=self.classifier, channel_id=channel_id)
        return timeline.build(messages)

    def run_from_messages(
        self,
        *,
        request: BacktestRequest,
        messages: list[RawTelegramMessage],
        candles: list[Candle],
    ) -> BacktestReport:
        events = self.build_events(channel_id=request.channel, messages=messages)
        return self.run_from_events(request=request, events=events, candles=candles)

    def run_from_events(
        self,
        *,
        request: BacktestRequest,
        events: list[BacktestEvent],
        candles: list[Candle],
        active_signal_hours: int | None = None,
        max_effective_leverage: Decimal | None = None,
        default_stop_pct: Decimal = Decimal("5"),
        strategy: TradeStrategy | None = None,
    ) -> BacktestReport:
        effective_strategy = strategy or self.strategy
        conservative_trades, conservative_final = self.simulator.simulate(
            events=events,
            candles=candles,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            fill_policy=BacktestFillPolicy.CONSERVATIVE,
            active_signal_hours=active_signal_hours,
            max_effective_leverage=max_effective_leverage,
            default_stop_pct=default_stop_pct,
            strategy=effective_strategy,
        )
        _optimistic_trades, optimistic_final = self.simulator.simulate(
            events=events,
            candles=candles,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            fill_policy=BacktestFillPolicy.OPTIMISTIC,
            active_signal_hours=active_signal_hours,
            max_effective_leverage=max_effective_leverage,
            default_stop_pct=default_stop_pct,
            strategy=effective_strategy,
        )
        final_balance = max(
            conservative_final
            if request.fill_policy is BacktestFillPolicy.CONSERVATIVE
            else optimistic_final,
            Decimal("0"),
        )
        total_pnl = final_balance - request.initial_balance

        metrics, score, _breakdown = self.scorer.score_with_breakdown(
            channel_id=request.channel,
            events=events,
            trades=conservative_trades,
            total_pnl=total_pnl,
            conservative_pnl=conservative_final - request.initial_balance,
            optimistic_pnl=optimistic_final - request.initial_balance,
            from_date=request.from_date,
            to_date=request.to_date,
            initial_balance=request.initial_balance,
        )
        report = BacktestReport(
            channel_id=request.channel,
            from_date=request.from_date,
            to_date=request.to_date,
            initial_balance=request.initial_balance,
            final_balance=final_balance,
            metrics=metrics,
            trades=conservative_trades,
            fill_policy=request.fill_policy,
            generated_at=datetime.now(timezone.utc),
            warnings=[],
        )
        report.warnings.append(f"channel_score={score}")
        return report


def run_fixture_backtest() -> tuple[dict[str, Any], str]:
    request = BacktestRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
        initial_balance=Decimal("1000"),
        interval="1m",
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        risk_per_trade_pct=Decimal("1"),
        use_ai_classifier=False,
        use_regex_fallback=True,
        max_messages=5000,
        symbols=None,
    )
    engine = BacktestEngine()
    report = engine.run(request)
    score = extract_channel_score(report.warnings)
    return report_to_json(report, score), report_to_telegram_summary(report, score)
