from __future__ import annotations

from datetime import datetime, timedelta, timezone

from triak_trade.backtesting.backtest_analytics import (
    BacktestAnalytics,
    BacktestAnalyticsFilters,
    BacktestAnalyticsRun,
)

NOW = datetime(2026, 7, 20, 10, tzinfo=timezone.utc)


def _signal(
    index: int,
    pnl: str,
    *,
    initial_balance: str = "100",
    status: str = "tp1_hit",
) -> dict[str, object]:
    return {
        "signal_id": f"signal_{index}",
        "symbol": "BTCUSDT" if index % 2 else "ETHUSDT",
        "side": "long" if index % 2 else "short",
        "status": status,
        "status_group": "inactive",
        "entry_time": (NOW + timedelta(minutes=index)).isoformat(),
        "exit_time": (NOW + timedelta(minutes=index + 5)).isoformat(),
        "initial_balance": initial_balance,
        "total_pnl": pnl,
    }


def _run(
    run_id: str,
    *,
    channel: str,
    strategy: str,
    pnls: list[str],
    created_offset: int = 0,
    fee_rate: str = "0.01",
    status: str = "completed",
) -> BacktestAnalyticsRun:
    signals = [_signal(index, pnl) for index, pnl in enumerate(pnls, start=1)]
    total = sum(int(pnl) for pnl in pnls)
    wins = sum(1 for pnl in pnls if int(pnl) > 0)
    losses = sum(1 for pnl in pnls if int(pnl) < 0)
    return BacktestAnalyticsRun(
        run_id=run_id,
        channel_resolved=channel,
        from_date=NOW - timedelta(days=30),
        to_date=NOW,
        interval="1m",
        strategy_key=strategy,
        status=status,
        created_at=NOW + timedelta(minutes=created_offset),
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        request_payload={
            "capital_per_signal": "100",
            "fill_policy": "conservative",
            "risk_per_trade_pct": "3",
            "fee_rate_pct": fee_rate,
            "leverage_source": "signal_or_default",
            "max_effective_leverage": "20",
            "close_open_positions_at_end": True,
            "include_not_filled_signals": True,
        },
        backtest_aggregate=(
            {
                "total_signals": len(signals),
                "filled_signals": len(signals),
                "closed_signals": len(signals),
                "wins": wins,
                "losses": losses,
                "total_pnl": str(total),
                "total_initial_balance": str(100 * len(signals)),
                "max_drawdown": str(abs(min(total, 0))),
                "period_pnl": {
                    "daily": [
                        {"period": "2026-07-20", "pnl": str(total)},
                    ]
                },
            }
            if status == "completed"
            else {}
        ),
        signals=signals if status == "completed" else [],
        errors=["safe failure"] if status == "failed" else [],
    )


def test_analytics_aggregates_channels_runs_and_signal_extremes() -> None:
    runs = [
        _run(
            "run_alpha_a",
            channel="https://t.me/alpha",
            strategy="default_risk_managed",
            pnls=["10", "8", "-2", "4", "5"],
        ),
        _run(
            "run_alpha_b",
            channel="https://t.me/alpha",
            strategy="tp_trailing_risk_managed",
            pnls=["7", "6", "-1", "3", "2"],
            created_offset=1,
            fee_rate="0.02",
        ),
        _run(
            "run_beta",
            channel="https://t.me/beta",
            strategy="default_risk_managed",
            pnls=["2", "-8", "-4", "1", "-3"],
            created_offset=2,
        ),
        _run(
            "run_beta_failed",
            channel="https://t.me/beta",
            strategy="default_risk_managed",
            pnls=[],
            created_offset=3,
            status="failed",
        ),
    ]

    result = BacktestAnalytics().analyze(runs)

    assert result["overview"]["runs"] == 4
    assert result["overview"]["completed_runs"] == 3
    assert result["overview"]["failed_runs"] == 1
    assert result["overview"]["signals"] == 15
    assert result["channel_rankings"][0]["channel"] == "https://t.me/alpha"
    assert result["channel_rankings"][0]["average_profit"] == "5.625"
    assert result["channel_rankings"][0]["average_loss"] == "-1.5"
    assert result["channel_rankings"][0]["maximum_profit"] == "10"
    assert result["channel_rankings"][0]["maximum_loss"] == "-2"
    assert result["channel_rankings"][1]["failed_runs"] == 1
    assert result["best_signals"][0]["total_pnl"] == "10"
    assert result["worst_signals"][0]["total_pnl"] == "-8"
    assert result["signal_return_distribution"]["samples"] == 15


def test_analytics_score_exposes_evidence_selection_penalty_and_methodology() -> None:
    runs = [
        _run(
            "run_one",
            channel="https://t.me/alpha",
            strategy="default_risk_managed",
            pnls=["10", "8", "-2", "4", "5"],
        ),
        _run(
            "run_two",
            channel="https://t.me/alpha",
            strategy="tp_trailing_risk_managed",
            pnls=["7", "6", "-1", "3", "2"],
            created_offset=1,
            fee_rate="0.02",
        ),
    ]

    result = BacktestAnalytics().analyze(runs)
    channel = result["channel_rankings"][0]

    assert int(channel["configuration_count"]) == 2
    assert channel["selection_penalty"] != "0"
    assert set(channel["score_breakdown"]) == {
        "run_performance",
        "downside_control",
        "cross_scenario_robustness",
        "sample_evidence",
        "execution_fill",
    }
    assert result["methodology"]["version"] == "backtest-score-v1"
    assert "annualized Sharpe" in result["methodology"]["why_not_annualized_sharpe"]
    assert len(result["methodology"]["sources"]) == 3


def test_analytics_filters_and_parameter_impact_use_only_matching_runs() -> None:
    runs = [
        _run(
            "run_alpha",
            channel="https://t.me/alpha",
            strategy="default_risk_managed",
            pnls=["10", "8", "-2", "4", "5"],
            fee_rate="0.01",
        ),
        _run(
            "run_beta",
            channel="https://t.me/beta",
            strategy="tp_trailing_risk_managed",
            pnls=["2", "-8", "-4", "1", "-3"],
            created_offset=1,
            fee_rate="0.02",
        ),
    ]

    result = BacktestAnalytics().analyze(
        runs,
        filters=BacktestAnalyticsFilters(channel="https://t.me/alpha", min_signals=5),
    )

    assert result["overview"]["runs"] == 1
    assert [row["run_id"] for row in result["run_comparison"]] == ["run_alpha"]
    assert result["filter_options"]["channels"] == [
        "https://t.me/alpha",
        "https://t.me/beta",
    ]
    fee_dimension = next(
        item for item in result["parameter_impact"] if item["key"] == "fee_rate_pct"
    )
    assert fee_dimension["variation_count"] == 1
    assert fee_dimension["values"][0]["value"] == "0.01"


def test_bootstrap_is_deterministic_and_marks_small_samples() -> None:
    sufficient = _run(
        "run_alpha",
        channel="https://t.me/alpha",
        strategy="default_risk_managed",
        pnls=["10", "8", "-2", "4", "5"],
    )
    insufficient = _run(
        "run_beta",
        channel="https://t.me/beta",
        strategy="default_risk_managed",
        pnls=["2", "-1", "3", "-2"],
    )
    analytics = BacktestAnalytics()

    first = analytics.analyze([sufficient, insufficient])["bootstrap"]
    second = analytics.analyze([sufficient, insufficient])["bootstrap"]

    assert first == second
    alpha = next(item for item in first if item["channel"] == "https://t.me/alpha")
    beta = next(item for item in first if item["channel"] == "https://t.me/beta")
    assert alpha["available"] is True
    assert alpha["resamples"] == 500
    assert beta == {
        "channel": "https://t.me/beta",
        "available": False,
        "samples": 4,
        "reason": "At least 5 persisted signal outcomes are required.",
    }
