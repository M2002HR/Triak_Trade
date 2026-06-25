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
from triak_trade.backtesting.strategies.registry import load_strategy
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
        self.strategy = strategy or load_strategy()

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
        min_allocation_pct: Decimal = Decimal("2"),
        max_allocation_pct: Decimal = Decimal("20"),
        default_stop_pct: Decimal = Decimal("5"),
        synthetic_stop_max_loss_pct_of_balance: Decimal = Decimal("5"),
        strategy: TradeStrategy | None = None,
        fee_rate_pct: Decimal = Decimal("0"),
        default_signal_leverage: Decimal = Decimal("1"),
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
            min_allocation_pct=min_allocation_pct,
            max_allocation_pct=max_allocation_pct,
            default_stop_pct=default_stop_pct,
            synthetic_stop_max_loss_pct_of_balance=synthetic_stop_max_loss_pct_of_balance,
            strategy=effective_strategy,
            fee_rate_pct=fee_rate_pct,
            default_signal_leverage=default_signal_leverage,
        )
        optimistic_trades, optimistic_final = self.simulator.simulate(
            events=events,
            candles=candles,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            fill_policy=BacktestFillPolicy.OPTIMISTIC,
            active_signal_hours=active_signal_hours,
            max_effective_leverage=max_effective_leverage,
            min_allocation_pct=min_allocation_pct,
            max_allocation_pct=max_allocation_pct,
            default_stop_pct=default_stop_pct,
            synthetic_stop_max_loss_pct_of_balance=synthetic_stop_max_loss_pct_of_balance,
            strategy=effective_strategy,
            fee_rate_pct=fee_rate_pct,
            default_signal_leverage=default_signal_leverage,
        )
        # Use primary trades/balance from the simulation matching the requested
        # fill_policy so that report.trades and total_pnl are always consistent
        # (sum of trade pnl == total_pnl, equity curve ends at final_balance).
        if request.fill_policy is BacktestFillPolicy.CONSERVATIVE:
            primary_trades, primary_final = conservative_trades, conservative_final
        else:
            primary_trades, primary_final = optimistic_trades, optimistic_final
        raw_final_balance = primary_final
        final_balance = max(raw_final_balance, Decimal("0"))
        total_pnl = final_balance - request.initial_balance
        if raw_final_balance < Decimal("0"):
            warnings_list = ["account_blown_up=true"]
        else:
            warnings_list = []

        metrics, score, _breakdown = self.scorer.score_with_breakdown(
            channel_id=request.channel,
            events=events,
            trades=primary_trades,
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
            interval=request.interval,
            metrics=metrics,
            trades=primary_trades,
            fill_policy=request.fill_policy,
            generated_at=datetime.now(timezone.utc),
            warnings=warnings_list,
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
