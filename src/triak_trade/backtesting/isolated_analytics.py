"""Cross-run analytics for isolated backtests.

The isolated runner owns simulation.  This module only reads persisted, safe
run outputs and turns them into comparable decision-support statistics.  It
never calls Telegram, market-data, AI, or execution services.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
SCORE_QUANTUM = Decimal("0.01")
RATIO_QUANTUM = Decimal("0.0001")


class IsolatedAnalyticsRun(BaseModel):
    """Minimal persisted run shape required by the analytics engine."""

    run_id: str
    run_type: str = "isolated"
    channel_resolved: str
    from_date: datetime
    to_date: datetime
    interval: str
    strategy_key: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    use_ai: bool = False
    request_payload: dict[str, Any] = Field(default_factory=dict)
    isolated_aggregate: dict[str, Any] = Field(default_factory=dict)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source: Literal["dashboard", "report"] = "dashboard"
    report_path: str | None = None


class IsolatedAnalyticsFilters(BaseModel):
    """Server-side filters shared by the API and analytics engine."""

    channel: str | None = None
    strategy: str | None = None
    fill_policy: str | None = None
    interval: str | None = None
    status: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_signals: int = 0
    sort_by: Literal[
        "score",
        "created_at",
        "total_pnl",
        "return_pct",
        "max_drawdown_pct",
        "signals",
    ] = "score"
    sort_order: Literal["asc", "desc"] = "desc"


class IsolatedBacktestAnalytics:
    """Build deterministic channel, run, parameter, and uncertainty analytics."""

    _PARAMETER_DIMENSIONS: tuple[tuple[str, str], ...] = (
        ("strategy_key", "Strategy"),
        ("interval", "Market interval"),
        ("fill_policy", "Fill policy"),
        ("risk_per_trade_pct", "Risk per trade"),
        ("capital_per_signal", "Capital per signal"),
        ("leverage_source", "Leverage source"),
        ("fixed_leverage", "Fixed leverage"),
        ("max_effective_leverage", "Maximum effective leverage"),
        ("default_signal_leverage", "Default signal leverage"),
        ("min_allocation_pct", "Minimum allocation"),
        ("max_allocation_pct", "Maximum allocation"),
        (
            "synthetic_stop_max_loss_pct_of_balance",
            "Synthetic stop maximum loss",
        ),
        ("fee_rate_pct", "Fee rate"),
        ("close_open_positions_at_end", "Close open positions at end"),
        ("include_not_filled_signals", "Include unfilled signals"),
        ("use_ai", "AI classification"),
    )

    def analyze(
        self,
        runs: Sequence[IsolatedAnalyticsRun],
        *,
        filters: IsolatedAnalyticsFilters | None = None,
    ) -> dict[str, Any]:
        selected_filters = filters or IsolatedAnalyticsFilters()
        isolated_runs = [run for run in runs if run.run_type == "isolated"]
        filter_options = self._filter_options(isolated_runs)
        filtered_runs = [
            run for run in isolated_runs if self._matches(run, selected_filters)
        ]
        completed_runs = [
            run
            for run in filtered_runs
            if run.status == "completed" and bool(run.isolated_aggregate)
        ]
        run_rows = [self._run_row(run) for run in completed_runs]
        run_rows = self._sort_run_rows(run_rows, selected_filters)
        channels = self._channel_rankings(filtered_runs, completed_runs, run_rows)
        signal_rows = self._signal_rows(completed_runs)
        statuses = Counter(run.status for run in filtered_runs)
        total_signals = sum(int(row["signals"]) for row in run_rows)
        total_pnl = sum((_decimal(row["total_pnl"]) for row in run_rows), ZERO)
        deployed_capital = sum(
            (_decimal(row["total_initial_balance"]) for row in run_rows),
            ZERO,
        )
        return_pct = _safe_div(total_pnl, deployed_capital) * HUNDRED
        latest_active = next(
            (
                run
                for run in sorted(filtered_runs, key=lambda item: item.created_at, reverse=True)
                if run.status in {"queued", "running", "cancelling"}
            ),
            None,
        )
        score_values = [_decimal(row["score"]) for row in run_rows]
        overview = {
            "runs": len(filtered_runs),
            "completed_runs": len(completed_runs),
            "failed_runs": statuses.get("failed", 0),
            "active_runs": sum(
                statuses.get(status, 0) for status in ("queued", "running", "cancelling")
            ),
            "channels": len({run.channel_resolved for run in filtered_runs}),
            "strategies": len({run.strategy_key for run in completed_runs}),
            "configurations": len(
                {self._configuration_fingerprint(run) for run in completed_runs}
            ),
            "signals": total_signals,
            "total_pnl": _plain(total_pnl),
            "deployed_capital": _plain(deployed_capital),
            "return_pct": _plain(return_pct),
            "average_run_score": _plain(_mean(score_values)),
            "latest_active_run_id": latest_active.run_id if latest_active else None,
            "dashboard_runs": sum(1 for run in filtered_runs if run.source == "dashboard"),
            "report_runs": sum(1 for run in filtered_runs if run.source == "report"),
        }
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": selected_filters.model_dump(mode="json"),
            "filter_options": filter_options,
            "overview": overview,
            "status_counts": dict(sorted(statuses.items())),
            "channel_rankings": channels,
            "run_comparison": run_rows,
            "strategy_comparison": self._strategy_comparison(run_rows),
            "parameter_impact": self._parameter_impact(completed_runs, run_rows),
            "time_series": sorted(
                (
                    {
                        "run_id": row["run_id"],
                        "channel": row["channel"],
                        "strategy": row["strategy"],
                        "created_at": row["created_at"],
                        "score": row["score"],
                        "total_pnl": row["total_pnl"],
                        "return_pct": row["return_pct"],
                        "max_drawdown_pct": row["max_drawdown_pct"],
                    }
                    for row in run_rows
                ),
                key=lambda row: str(row["created_at"]),
            ),
            "signal_return_distribution": self._histogram(
                [_decimal(row["return_pct"]) for row in signal_rows]
            ),
            "best_signals": sorted(
                signal_rows,
                key=lambda row: _decimal(row["total_pnl"]),
                reverse=True,
            )[:10],
            "worst_signals": sorted(
                signal_rows,
                key=lambda row: _decimal(row["total_pnl"]),
            )[:10],
            "bootstrap": [self._bootstrap_channel(channel, completed_runs) for channel in channels],
            "methodology": self.methodology(),
            "empty": not filtered_runs,
        }

    @staticmethod
    def methodology() -> dict[str, Any]:
        return {
            "version": "isolated-score-v1",
            "purpose": (
                "Decision support for comparing persisted isolated simulations; "
                "it is not a promise of future returns or a live-trading instruction."
            ),
            "run_score_weights": {
                "profitability": "25%",
                "downside_control": "25%",
                "trade_quality": "20%",
                "time_consistency": "15%",
                "execution_fill": "5%",
                "sample_evidence": "10%",
            },
            "channel_score_weights": {
                "run_performance": "30%",
                "downside_control": "25%",
                "cross_scenario_robustness": "20%",
                "sample_evidence": "15%",
                "execution_fill": "10%",
            },
            "selection_penalty": (
                "Up to 20 points when many tested configurations and a large best-vs-median "
                "gap indicate selection or overfitting risk."
            ),
            "bootstrap": (
                "Deterministic 500-resample percentile interval of mean per-signal return. "
                "It assumes the observed signals are representative and does not remove regime "
                "or serial-dependence risk."
            ),
            "why_not_annualized_sharpe": (
                "Isolated signals are irregular event samples rather than equally spaced periodic "
                "returns, so an annualized Sharpe ratio would imply unsupported timing assumptions."
            ),
            "sources": [
                {
                    "label": "William F. Sharpe — The Sharpe Ratio",
                    "url": "https://web.stanford.edu/~wfsharpe/art/sr/SR.htm",
                },
                {
                    "label": "NIST — Bootstrap Plot and Uncertainty",
                    "url": "https://www.itl.nist.gov/div898/handbook/eda/section3/bootplot.htm",
                },
                {
                    "label": "Bailey & López de Prado — Deflated Sharpe Ratio",
                    "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551",
                },
            ],
        }

    def _matches(self, run: IsolatedAnalyticsRun, filters: IsolatedAnalyticsFilters) -> bool:
        payload = run.request_payload
        if filters.channel and run.channel_resolved != filters.channel:
            return False
        if filters.strategy and run.strategy_key != filters.strategy:
            return False
        if filters.fill_policy and str(payload.get("fill_policy") or "") != filters.fill_policy:
            return False
        if filters.interval and run.interval != filters.interval:
            return False
        if filters.status and filters.status != "all" and run.status != filters.status:
            return False
        if filters.date_from and run.created_at < filters.date_from:
            return False
        if filters.date_to and run.created_at > filters.date_to:
            return False
        if int(run.isolated_aggregate.get("total_signals") or 0) < filters.min_signals:
            return False
        return True

    @staticmethod
    def _filter_options(runs: Sequence[IsolatedAnalyticsRun]) -> dict[str, list[str]]:
        return {
            "channels": sorted({run.channel_resolved for run in runs}),
            "strategies": sorted({run.strategy_key for run in runs}),
            "fill_policies": sorted(
                {
                    str(run.request_payload.get("fill_policy"))
                    for run in runs
                    if run.request_payload.get("fill_policy") is not None
                }
            ),
            "intervals": sorted({run.interval for run in runs}),
            "statuses": sorted({run.status for run in runs}),
        }

    def _run_row(self, run: IsolatedAnalyticsRun) -> dict[str, Any]:
        aggregate = run.isolated_aggregate
        signals = run.signals
        pnls = [_decimal(signal.get("total_pnl")) for signal in signals]
        filled = [
            signal for signal in signals if str(signal.get("status") or "") != "not_filled"
        ]
        closed = [
            signal for signal in filled if str(signal.get("status_group") or "") != "active"
        ]
        winning_pnls = [value for value in pnls if value > ZERO]
        losing_pnls = [value for value in pnls if value < ZERO]
        total_signals = int(aggregate.get("total_signals") or len(signals))
        filled_count = int(aggregate.get("filled_signals") or len(filled))
        closed_count = int(aggregate.get("closed_signals") or len(closed))
        wins = int(aggregate.get("wins") or len(winning_pnls))
        losses = int(aggregate.get("losses") or len(losing_pnls))
        total_pnl = _decimal(aggregate.get("total_pnl"), default=sum(pnls, ZERO))
        capital_per_signal = _decimal(
            run.request_payload.get("capital_per_signal"),
            default=_decimal(aggregate.get("total_initial_balance")),
        )
        total_initial = _decimal(
            aggregate.get("total_initial_balance"),
            default=capital_per_signal * Decimal(total_signals),
        )
        if total_initial <= ZERO and total_signals > 0:
            total_initial = capital_per_signal * Decimal(total_signals)
        per_signal_returns = [
            _safe_div(
                pnl,
                _decimal(signal.get("initial_balance"), default=capital_per_signal),
            )
            for pnl, signal in zip(pnls, signals, strict=False)
        ]
        gross_profit = sum(winning_pnls, ZERO)
        gross_loss = abs(sum(losing_pnls, ZERO))
        profit_factor = _safe_div(gross_profit, gross_loss) if gross_loss > ZERO else None
        win_rate = _safe_div(Decimal(wins), Decimal(closed_count))
        fill_rate = _safe_div(Decimal(filled_count), Decimal(total_signals))
        max_drawdown = _decimal(aggregate.get("max_drawdown"))
        return_ratio = _safe_div(total_pnl, total_initial)
        drawdown_ratio = _safe_div(max_drawdown, total_initial)
        expectancy = _safe_div(total_pnl, Decimal(filled_count))
        expectancy_ratio = _safe_div(expectancy, capital_per_signal)
        positive_period_ratio = self._positive_period_ratio(aggregate)
        return_stddev = _stddev(per_signal_returns)
        downside_deviation = _downside_deviation(per_signal_returns)
        expected_shortfall = _expected_shortfall(per_signal_returns)
        score = self._score_run(
            return_ratio=return_ratio,
            expectancy_ratio=expectancy_ratio,
            profit_factor=profit_factor,
            win_rate=win_rate,
            drawdown_ratio=drawdown_ratio,
            expected_shortfall=expected_shortfall,
            downside_deviation=downside_deviation,
            positive_period_ratio=positive_period_ratio,
            fill_rate=fill_rate,
            closed_signals=closed_count,
            duration_days=max(Decimal((run.to_date - run.from_date).days), ZERO),
        )
        runtime_ms = None
        if run.started_at and run.finished_at:
            runtime_ms = max(
                0,
                int((run.finished_at - run.started_at).total_seconds() * 1000),
            )
        parameters = self._safe_parameters(run)
        return {
            "run_id": run.run_id,
            "source": run.source,
            "report_path": run.report_path,
            "channel": run.channel_resolved,
            "strategy": run.strategy_key,
            "status": run.status,
            "created_at": run.created_at.isoformat(),
            "from_date": run.from_date.isoformat(),
            "to_date": run.to_date.isoformat(),
            "interval": run.interval,
            "runtime_duration_ms": runtime_ms,
            "signals": total_signals,
            "filled_signals": filled_count,
            "closed_signals": closed_count,
            "wins": wins,
            "losses": losses,
            "win_rate": _plain(win_rate * HUNDRED),
            "fill_rate": _plain(fill_rate * HUNDRED),
            "total_pnl": _plain(total_pnl),
            "total_initial_balance": _plain(total_initial),
            "return_pct": _plain(return_ratio * HUNDRED),
            "average_pnl": _plain(_mean(pnls)),
            "median_pnl": _plain(_median(pnls)),
            "average_profit": _plain(_mean(winning_pnls)),
            "average_loss": _plain(_mean(losing_pnls)),
            "maximum_profit": _plain(max(winning_pnls, default=ZERO)),
            "maximum_loss": _plain(min(losing_pnls, default=ZERO)),
            "expectancy": _plain(expectancy),
            "profit_factor": _plain(profit_factor) if profit_factor is not None else None,
            "max_drawdown": _plain(max_drawdown),
            "max_drawdown_pct": _plain(drawdown_ratio * HUNDRED),
            "signal_return_stddev_pct": _plain(return_stddev * HUNDRED),
            "downside_deviation_pct": _plain(downside_deviation * HUNDRED),
            "expected_shortfall_pct": _plain(expected_shortfall * HUNDRED),
            "positive_period_rate": _plain(positive_period_ratio * HUNDRED),
            "score": _plain(score["final_score"]),
            "score_grade": _score_grade(score["final_score"]),
            "score_breakdown": {key: _plain(value) for key, value in score.items()},
            "parameters": parameters,
            "configuration_id": self._configuration_fingerprint(run),
            "warnings_count": len(run.warnings),
            "errors_count": len(run.errors),
        }

    @staticmethod
    def _score_run(
        *,
        return_ratio: Decimal,
        expectancy_ratio: Decimal,
        profit_factor: Decimal | None,
        win_rate: Decimal,
        drawdown_ratio: Decimal,
        expected_shortfall: Decimal,
        downside_deviation: Decimal,
        positive_period_ratio: Decimal,
        fill_rate: Decimal,
        closed_signals: int,
        duration_days: Decimal,
    ) -> dict[str, Decimal]:
        profitability = _mean(
            [
                _signed_score(return_ratio, target=Decimal("0.20")),
                _signed_score(expectancy_ratio, target=Decimal("0.10")),
            ]
        )
        if profit_factor is None:
            profit_factor_score = HUNDRED if return_ratio > ZERO else Decimal("50")
        else:
            profit_factor_score = _clamp(profit_factor / Decimal("2") * HUNDRED)
        trade_quality = _mean(
            [
                profit_factor_score,
                _clamp(win_rate * HUNDRED),
                _signed_score(expectancy_ratio, target=Decimal("0.10")),
            ]
        )
        downside_control = _mean(
            [
                HUNDRED - _clamp(drawdown_ratio / Decimal("0.25") * HUNDRED),
                HUNDRED
                - _clamp(abs(min(expected_shortfall, ZERO)) / Decimal("0.20") * HUNDRED),
                HUNDRED - _clamp(downside_deviation / Decimal("0.20") * HUNDRED),
            ]
        )
        time_consistency = _mean(
            [
                _clamp(positive_period_ratio * HUNDRED),
                HUNDRED - _clamp(downside_deviation / Decimal("0.15") * HUNDRED),
            ]
        )
        execution_fill = _clamp(fill_rate * HUNDRED)
        trade_evidence = min(_safe_div(Decimal(closed_signals), Decimal("50")), ONE)
        duration_evidence = min(_safe_div(duration_days, Decimal("60")), ONE)
        sample_evidence = (
            trade_evidence * Decimal("75")
            + duration_evidence * Decimal("25")
        )
        weighted = (
            profitability * Decimal("0.25")
            + downside_control * Decimal("0.25")
            + trade_quality * Decimal("0.20")
            + time_consistency * Decimal("0.15")
            + execution_fill * Decimal("0.05")
            + sample_evidence * Decimal("0.10")
        )
        confidence_multiplier = Decimal("0.65") + (
            sample_evidence / HUNDRED * Decimal("0.35")
        )
        final_score = _clamp(weighted * confidence_multiplier)
        return {
            "final_score": final_score.quantize(SCORE_QUANTUM),
            "profitability": profitability.quantize(SCORE_QUANTUM),
            "downside_control": downside_control.quantize(SCORE_QUANTUM),
            "trade_quality": trade_quality.quantize(SCORE_QUANTUM),
            "time_consistency": time_consistency.quantize(SCORE_QUANTUM),
            "execution_fill": execution_fill.quantize(SCORE_QUANTUM),
            "sample_evidence": sample_evidence.quantize(SCORE_QUANTUM),
        }

    @staticmethod
    def _positive_period_ratio(aggregate: dict[str, Any]) -> Decimal:
        periods = aggregate.get("period_pnl") or {}
        daily = periods.get("daily") if isinstance(periods, dict) else []
        if not isinstance(daily, list) or not daily:
            return ZERO
        positive = sum(1 for item in daily if _decimal(item.get("pnl")) > ZERO)
        return _safe_div(Decimal(positive), Decimal(len(daily)))

    def _channel_rankings(
        self,
        filtered_runs: Sequence[IsolatedAnalyticsRun],
        completed_runs: Sequence[IsolatedAnalyticsRun],
        run_rows: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows_by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        runs_by_channel: dict[str, list[IsolatedAnalyticsRun]] = defaultdict(list)
        completed_by_channel: dict[str, list[IsolatedAnalyticsRun]] = defaultdict(list)
        for row in run_rows:
            rows_by_channel[str(row["channel"])].append(row)
        for run in filtered_runs:
            runs_by_channel[run.channel_resolved].append(run)
        for run in completed_runs:
            completed_by_channel[run.channel_resolved].append(run)

        rankings: list[dict[str, Any]] = []
        for channel, all_channel_runs in runs_by_channel.items():
            rows = rows_by_channel.get(channel, [])
            channel_completed = completed_by_channel.get(channel, [])
            if not rows:
                rankings.append(
                    {
                        "channel": channel,
                        "score": "0",
                        "score_grade": "insufficient",
                        "confidence": "insufficient",
                        "run_count": len(all_channel_runs),
                        "completed_runs": 0,
                        "failed_runs": sum(
                            1 for run in all_channel_runs if run.status == "failed"
                        ),
                        "signals": 0,
                        "total_pnl": "0",
                        "return_pct": "0",
                        "selection_penalty": "0",
                    }
                )
                continue
            run_scores = [_decimal(row["score"]) for row in rows]
            returns = [_decimal(row["return_pct"]) / HUNDRED for row in rows]
            drawdowns = [_decimal(row["max_drawdown_pct"]) / HUNDRED for row in rows]
            total_pnl = sum((_decimal(row["total_pnl"]) for row in rows), ZERO)
            total_initial = sum(
                (_decimal(row["total_initial_balance"]) for row in rows),
                ZERO,
            )
            all_signal_pnls = [
                _decimal(signal.get("total_pnl"))
                for run in channel_completed
                for signal in run.signals
            ]
            winning = [value for value in all_signal_pnls if value > ZERO]
            losing = [value for value in all_signal_pnls if value < ZERO]
            completed_count = len(rows)
            failed_count = sum(1 for run in all_channel_runs if run.status == "failed")
            signal_count = sum(int(row["signals"]) for row in rows)
            filled_count = sum(int(row["filled_signals"]) for row in rows)
            closed_count = sum(int(row["closed_signals"]) for row in rows)
            wins = sum(int(row["wins"]) for row in rows)
            configuration_count = len(
                {self._configuration_fingerprint(run) for run in channel_completed}
            )
            performance = _mean(run_scores)
            downside_control = _mean(
                [
                    _decimal(row["score_breakdown"]["downside_control"])
                    for row in rows
                ]
            )
            execution_fill = _safe_div(Decimal(filled_count), Decimal(signal_count)) * HUNDRED
            positive_run_rate = (
                _safe_div(
                    Decimal(sum(1 for value in returns if value > ZERO)),
                    Decimal(completed_count),
                )
                * HUNDRED
            )
            return_dispersion = _stddev(returns)
            dispersion_control = HUNDRED - _clamp(
                return_dispersion / Decimal("0.20") * HUNDRED
            )
            best_return = max(returns, default=ZERO)
            median_return = _median(returns)
            best_median_gap = max(best_return - median_return, ZERO)
            gap_reference = max(abs(best_return), Decimal("0.05"))
            median_capture = HUNDRED - _clamp(
                best_median_gap / gap_reference * HUNDRED
            )
            robustness = _mean(
                [positive_run_rate, dispersion_control, median_capture]
            )
            earliest = min(run.from_date for run in channel_completed)
            latest = max(run.to_date for run in channel_completed)
            coverage_days = max(Decimal((latest - earliest).days), ZERO)
            sample_evidence = (
                min(_safe_div(Decimal(signal_count), Decimal("100")), ONE)
                * Decimal("60")
                + min(_safe_div(Decimal(completed_count), Decimal("10")), ONE)
                * Decimal("25")
                + min(_safe_div(coverage_days, Decimal("90")), ONE)
                * Decimal("15")
            )
            trial_penalty = min(
                Decimal(max(configuration_count - 1, 0)) * Decimal("1.5"),
                Decimal("12"),
            )
            gap_penalty = min(
                best_median_gap / gap_reference * Decimal("8"),
                Decimal("8"),
            )
            selection_penalty = trial_penalty + gap_penalty
            weighted = (
                performance * Decimal("0.30")
                + downside_control * Decimal("0.25")
                + robustness * Decimal("0.20")
                + sample_evidence * Decimal("0.15")
                + execution_fill * Decimal("0.10")
            )
            final_score = _clamp(weighted - selection_penalty)
            confidence = _confidence_label(sample_evidence)
            rankings.append(
                {
                    "channel": channel,
                    "score": _plain(final_score),
                    "score_grade": _score_grade(final_score),
                    "confidence": confidence,
                    "run_count": len(all_channel_runs),
                    "completed_runs": completed_count,
                    "failed_runs": failed_count,
                    "failure_rate": _plain(
                        _safe_div(Decimal(failed_count), Decimal(len(all_channel_runs)))
                        * HUNDRED
                    ),
                    "configuration_count": configuration_count,
                    "strategy_count": len({run.strategy_key for run in channel_completed}),
                    "signals": signal_count,
                    "filled_signals": filled_count,
                    "wins": wins,
                    "losses": max(closed_count - wins, 0),
                    "win_rate": _plain(
                        _safe_div(Decimal(wins), Decimal(closed_count)) * HUNDRED
                    ),
                    "fill_rate": _plain(
                        _safe_div(Decimal(filled_count), Decimal(signal_count)) * HUNDRED
                    ),
                    "total_pnl": _plain(total_pnl),
                    "return_pct": _plain(
                        _safe_div(total_pnl, total_initial) * HUNDRED
                    ),
                    "mean_run_pnl": _plain(
                        _mean([_decimal(row["total_pnl"]) for row in rows])
                    ),
                    "median_run_pnl": _plain(
                        _median([_decimal(row["total_pnl"]) for row in rows])
                    ),
                    "average_profit": _plain(_mean(winning)),
                    "average_loss": _plain(_mean(losing)),
                    "maximum_profit": _plain(max(winning, default=ZERO)),
                    "maximum_loss": _plain(min(losing, default=ZERO)),
                    "average_drawdown_pct": _plain(_mean(drawdowns) * HUNDRED),
                    "worst_drawdown_pct": _plain(max(drawdowns, default=ZERO) * HUNDRED),
                    "positive_run_rate": _plain(positive_run_rate),
                    "return_dispersion_pct": _plain(return_dispersion * HUNDRED),
                    "selection_penalty": _plain(selection_penalty),
                    "score_breakdown": {
                        "run_performance": _plain(performance),
                        "downside_control": _plain(downside_control),
                        "cross_scenario_robustness": _plain(robustness),
                        "sample_evidence": _plain(sample_evidence),
                        "execution_fill": _plain(execution_fill),
                    },
                    "coverage_from": earliest.isoformat(),
                    "coverage_to": latest.isoformat(),
                }
            )
        rankings.sort(
            key=lambda row: (
                _decimal(row["score"]),
                int(row.get("signals") or 0),
            ),
            reverse=True,
        )
        for index, row in enumerate(rankings, start=1):
            row["rank"] = index
        return rankings

    @staticmethod
    def _strategy_comparison(run_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in run_rows:
            grouped[str(row["strategy"])].append(row)
        result = []
        for strategy, rows in grouped.items():
            result.append(
                _group_summary(
                    dimension="strategy_key",
                    label="Strategy",
                    value=strategy,
                    rows=rows,
                    baseline_rows=run_rows,
                )
            )
        return sorted(
            result,
            key=lambda row: (_decimal(row["average_score"]), int(row["runs"])),
            reverse=True,
        )

    def _parameter_impact(
        self,
        runs: Sequence[IsolatedAnalyticsRun],
        run_rows: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        row_by_id = {str(row["run_id"]): row for row in run_rows}
        dimensions: list[dict[str, Any]] = []
        for key, label in self._PARAMETER_DIMENSIONS:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for run in runs:
                row = row_by_id.get(run.run_id)
                if row is None:
                    continue
                value = self._parameter_value(run, key)
                if value is None or value == "":
                    continue
                grouped[_display_parameter(value)].append(row)
            if not grouped:
                continue
            values = [
                _group_summary(
                    dimension=key,
                    label=label,
                    value=value,
                    rows=rows,
                    baseline_rows=run_rows,
                )
                for value, rows in grouped.items()
            ]
            values.sort(
                key=lambda row: (
                    _decimal(row["average_score"]),
                    int(row["runs"]),
                ),
                reverse=True,
            )
            dimensions.append(
                {
                    "key": key,
                    "label": label,
                    "values": values,
                    "variation_count": len(values),
                    "comparable": len(values) > 1,
                }
            )
        return dimensions

    @staticmethod
    def _parameter_value(run: IsolatedAnalyticsRun, key: str) -> Any:
        if key == "strategy_key":
            return run.strategy_key
        if key == "interval":
            return run.interval
        if key == "use_ai":
            return run.use_ai
        return run.request_payload.get(key)

    def _safe_parameters(self, run: IsolatedAnalyticsRun) -> dict[str, str]:
        return {
            key: _display_parameter(value)
            for key, _label in self._PARAMETER_DIMENSIONS
            if (value := self._parameter_value(run, key)) is not None and value != ""
        }

    def _configuration_fingerprint(self, run: IsolatedAnalyticsRun) -> str:
        values = {
            key: self._parameter_value(run, key)
            for key, _label in self._PARAMETER_DIMENSIONS
        }
        encoded = json.dumps(values, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:12]

    @staticmethod
    def _signal_rows(runs: Sequence[IsolatedAnalyticsRun]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for run in runs:
            capital = _decimal(run.request_payload.get("capital_per_signal"))
            for signal in run.signals:
                pnl = _decimal(signal.get("total_pnl"))
                initial = _decimal(signal.get("initial_balance"), default=capital)
                rows.append(
                    {
                        "run_id": run.run_id,
                        "source": run.source,
                        "report_path": run.report_path,
                        "channel": run.channel_resolved,
                        "strategy": run.strategy_key,
                        "signal_id": str(signal.get("signal_id") or "unknown"),
                        "symbol": str(signal.get("symbol") or "unknown"),
                        "side": str(signal.get("side") or "unknown"),
                        "status": str(signal.get("status") or "unknown"),
                        "entry_time": signal.get("entry_time"),
                        "exit_time": signal.get("exit_time"),
                        "total_pnl": _plain(pnl),
                        "return_pct": _plain(_safe_div(pnl, initial) * HUNDRED),
                    }
                )
        return rows

    @staticmethod
    def _histogram(values: Sequence[Decimal], *, bucket_count: int = 12) -> dict[str, Any]:
        if not values:
            return {"buckets": [], "samples": 0}
        minimum = min(values)
        maximum = max(values)
        if minimum == maximum:
            return {
                "buckets": [
                    {
                        "from": _plain(minimum),
                        "to": _plain(maximum),
                        "label": _plain(minimum),
                        "count": len(values),
                    }
                ],
                "samples": len(values),
            }
        width = (maximum - minimum) / Decimal(bucket_count)
        counts = [0 for _ in range(bucket_count)]
        for value in values:
            index = int((value - minimum) / width)
            counts[min(index, bucket_count - 1)] += 1
        buckets = []
        for index, count in enumerate(counts):
            start = minimum + width * Decimal(index)
            end = minimum + width * Decimal(index + 1)
            buckets.append(
                {
                    "from": _plain(start),
                    "to": _plain(end),
                    "label": f"{_plain(start)} to {_plain(end)}",
                    "count": count,
                }
            )
        return {"buckets": buckets, "samples": len(values)}

    @staticmethod
    def _bootstrap_channel(
        channel_row: dict[str, Any],
        completed_runs: Sequence[IsolatedAnalyticsRun],
    ) -> dict[str, Any]:
        channel = str(channel_row["channel"])
        signal_returns: list[Decimal] = []
        for run in completed_runs:
            if run.channel_resolved != channel:
                continue
            capital = _decimal(run.request_payload.get("capital_per_signal"))
            for signal in run.signals:
                initial = _decimal(signal.get("initial_balance"), default=capital)
                signal_returns.append(
                    _safe_div(_decimal(signal.get("total_pnl")), initial) * HUNDRED
                )
        if len(signal_returns) < 5:
            return {
                "channel": channel,
                "available": False,
                "samples": len(signal_returns),
                "reason": "At least 5 persisted signal outcomes are required.",
            }
        seed = int(hashlib.sha256(channel.encode("utf-8")).hexdigest()[:16], 16)
        generator = random.Random(seed)
        sample_size = len(signal_returns)
        means: list[Decimal] = []
        for _ in range(500):
            sample_total = sum(
                (signal_returns[generator.randrange(sample_size)] for _ in range(sample_size)),
                ZERO,
            )
            means.append(sample_total / Decimal(sample_size))
        means.sort()
        probability_positive = (
            _safe_div(
                Decimal(sum(1 for value in means if value > ZERO)),
                Decimal(len(means)),
            )
            * HUNDRED
        )
        return {
            "channel": channel,
            "available": True,
            "samples": sample_size,
            "resamples": len(means),
            "observed_mean_return_pct": _plain(_mean(signal_returns)),
            "p05_mean_return_pct": _plain(_percentile(means, 5)),
            "p50_mean_return_pct": _plain(_percentile(means, 50)),
            "p95_mean_return_pct": _plain(_percentile(means, 95)),
            "probability_positive_pct": _plain(probability_positive),
        }

    @staticmethod
    def _sort_run_rows(
        rows: list[dict[str, Any]],
        filters: IsolatedAnalyticsFilters,
    ) -> list[dict[str, Any]]:
        numeric_keys = {
            "score": "score",
            "total_pnl": "total_pnl",
            "return_pct": "return_pct",
            "max_drawdown_pct": "max_drawdown_pct",
            "signals": "signals",
        }
        reverse = filters.sort_order == "desc"
        if filters.sort_by == "created_at":
            return sorted(rows, key=lambda row: str(row["created_at"]), reverse=reverse)
        key = numeric_keys[filters.sort_by]
        return sorted(rows, key=lambda row: _decimal(row[key]), reverse=reverse)


def _group_summary(
    *,
    dimension: str,
    label: str,
    value: str,
    rows: Sequence[dict[str, Any]],
    baseline_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    returns = [_decimal(row["return_pct"]) for row in rows]
    pnl_values = [_decimal(row["total_pnl"]) for row in rows]
    scores = [_decimal(row["score"]) for row in rows]
    drawdowns = [_decimal(row["max_drawdown_pct"]) for row in rows]
    baseline_returns = [_decimal(row["return_pct"]) for row in baseline_rows]
    baseline_scores = [_decimal(row["score"]) for row in baseline_rows]
    positive_runs = sum(1 for result in returns if result > ZERO)
    run_count = len(rows)
    signal_count = sum(int(row["signals"]) for row in rows)
    evidence = min(
        _safe_div(Decimal(run_count), Decimal("5")) * Decimal("60")
        + _safe_div(Decimal(signal_count), Decimal("100")) * Decimal("40"),
        HUNDRED,
    )
    return {
        "dimension": dimension,
        "label": label,
        "value": value,
        "runs": run_count,
        "signals": signal_count,
        "average_pnl": _plain(_mean(pnl_values)),
        "average_return_pct": _plain(_mean(returns)),
        "median_return_pct": _plain(_median(returns)),
        "average_score": _plain(_mean(scores)),
        "average_drawdown_pct": _plain(_mean(drawdowns)),
        "positive_run_rate": _plain(
            _safe_div(Decimal(positive_runs), Decimal(run_count)) * HUNDRED
        ),
        "delta_return_pct": _plain(_mean(returns) - _mean(baseline_returns)),
        "delta_score": _plain(_mean(scores) - _mean(baseline_scores)),
        "evidence_score": _plain(evidence),
        "confidence": _confidence_label(evidence),
    }


def _decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _plain(value: Decimal | None) -> str:
    if value is None:
        return "0"
    normalized = value.quantize(Decimal("0.0001"))
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        return ZERO
    return numerator / denominator


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    return sum(values, ZERO) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _stddev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    average = _mean(values)
    variance = sum(((value - average) ** 2 for value in values), ZERO) / Decimal(
        len(values)
    )
    return variance.sqrt()


def _downside_deviation(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    downside_squares = [min(value, ZERO) ** 2 for value in values]
    return (_mean(downside_squares)).sqrt()


def _expected_shortfall(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    tail_size = max(1, (len(ordered) + 9) // 10)
    return _mean(ordered[:tail_size])


def _percentile(values: Sequence[Decimal], percentile: int) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile // 100
    return ordered[index]


def _clamp(value: Decimal, minimum: Decimal = ZERO, maximum: Decimal = HUNDRED) -> Decimal:
    return max(minimum, min(value, maximum))


def _signed_score(value: Decimal, *, target: Decimal) -> Decimal:
    if target <= ZERO:
        return Decimal("50")
    return _clamp(Decimal("50") + value / target * Decimal("50"))


def _score_grade(score: Decimal) -> str:
    if score >= Decimal("80"):
        return "excellent"
    if score >= Decimal("65"):
        return "strong"
    if score >= Decimal("50"):
        return "watch"
    if score >= Decimal("35"):
        return "weak"
    return "high-risk"


def _confidence_label(score: Decimal) -> str:
    if score >= Decimal("75"):
        return "high"
    if score >= Decimal("45"):
        return "medium"
    return "low"


def _display_parameter(value: Any) -> str:
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, Decimal):
        return _plain(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return ", ".join(str(item) for item in value)
    return str(value)
