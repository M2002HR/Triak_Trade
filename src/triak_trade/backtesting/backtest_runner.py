"""Signal-first backtest runner."""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import tempfile
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from triak_trade.agents.classifier import MessageClassifier, RegexMessageClassifier
from triak_trade.agents.context import ChannelContext, merge_parsed_signals
from triak_trade.backtesting.correlation import resolve_related_signal_id
from triak_trade.backtesting.directives import (
    apply_text_directive_action,
    build_ignored_signal,
    detect_close_all_instruction,
    detect_move_stop_to_entry,
    detect_tp_list_update,
    extract_close_fraction,
    normalize_related_signal_action,
)
from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.real_runner import (
    RealBacktestMessageTrace,
    RealBacktestProgressEvent,
    RealBacktestResult,
    RealBacktestRunner,
    RealBacktestRunRequest,
)
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.backtesting.strategies.registry import (
    describe_strategy_by_key,
    load_strategy,
)
from triak_trade.backtesting.symbol_mapper import normalize_market_symbol
from triak_trade.core.formatting import decimal_to_plain_string, format_decimal
from triak_trade.core.time import TEHRAN_TZ
from triak_trade.domain.enums import BacktestFillPolicy, SignalAction, SignalStatus
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import Candle, ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.market_data.intervals import interval_to_seconds

log = logging.getLogger(__name__)

_BACKTEST_WORKER_ASSIGNED_CPU_ID: int | None = None

_FOLLOW_UP_ACTIONS = {
    SignalAction.CLOSE,
    SignalAction.CANCEL,
    SignalAction.UPDATE_SL,
    SignalAction.UPDATE_TP,
    SignalAction.UPDATE_LEVERAGE,
    SignalAction.UPDATE_ENTRY,
}

_BACKTEST_MAX_CHART_CANDLES = 1200


def backtest_available_cpu_ids() -> tuple[int, ...]:
    if hasattr(os, "sched_getaffinity"):
        try:
            cpu_ids = sorted(int(item) for item in os.sched_getaffinity(0))
        except OSError:
            cpu_ids = []
        if cpu_ids:
            return tuple(cpu_ids)
    cpu_count = os.cpu_count() or 1
    return tuple(range(max(1, cpu_count)))


def default_backtest_parallel_workers() -> int:
    return max(1, len(backtest_available_cpu_ids()))


def _build_backtest_worker_cpu_assignments(worker_count: int) -> list[int]:
    cpu_ids = backtest_available_cpu_ids()
    if not cpu_ids:
        return []
    safe_worker_count = max(1, min(worker_count, len(cpu_ids)))
    return list(cpu_ids[:safe_worker_count])


def backtest_worker_multiprocessing_context() -> Any:
    try:
        return get_context("spawn")
    except ValueError:
        return get_context()


def _artifact_path_for_symbol(root: Path, symbol: str) -> Path:
    safe_symbol = "".join(ch if ch.isalnum() else "_" for ch in symbol).strip("_") or "symbol"
    return root / f"{safe_symbol}.candles.pkl"


def _write_candle_artifact(
    *,
    artifact_root: Path,
    symbol: str,
    candles: list[Candle],
    artifact_key: str | None = None,
) -> BacktestCandleArtifact:
    artifact_root.mkdir(parents=True, exist_ok=True)
    path = _artifact_path_for_symbol(artifact_root, artifact_key or symbol)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            # Write each candle immediately. Building a second list of model
            # dumps nearly doubles the parent worker's peak memory during a
            # market-data fetch.
            pickle.dump(len(candles), handle, protocol=pickle.HIGHEST_PROTOCOL)
            for candle in candles:
                pickle.dump(
                    candle.model_dump(mode="python"),
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return BacktestCandleArtifact(
        symbol=symbol,
        path=str(path),
        candle_count=len(candles),
    )


def _load_candle_artifact(artifact: BacktestCandleArtifact) -> list[Candle]:
    with Path(artifact.path).open("rb") as handle:
        candle_count = pickle.load(handle)
        if not isinstance(candle_count, int) or candle_count < 0:
            raise ValueError("Invalid backtest candle artifact header")
        return [Candle.model_validate(pickle.load(handle)) for _ in range(candle_count)]


def _initialize_backtest_worker(cpu_id_queue: Any) -> None:
    global _BACKTEST_WORKER_ASSIGNED_CPU_ID
    try:
        cpu_id = cpu_id_queue.get_nowait()
    except Exception:
        cpu_id = None
    if cpu_id is None:
        _BACKTEST_WORKER_ASSIGNED_CPU_ID = None
        return
    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, {int(cpu_id)})
        except OSError:
            _BACKTEST_WORKER_ASSIGNED_CPU_ID = None
            return
    _BACKTEST_WORKER_ASSIGNED_CPU_ID = int(cpu_id)


def _probe_backtest_worker_cpu() -> dict[str, Any]:
    affinity: list[int] = []
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity = sorted(int(item) for item in os.sched_getaffinity(0))
        except OSError:
            affinity = []
    return {
        "pid": os.getpid(),
        "assigned_cpu_id": _BACKTEST_WORKER_ASSIGNED_CPU_ID,
        "affinity": affinity,
    }


class BacktestSignalRecord(BaseModel):
    signal_id: str
    channel_id: str
    symbol: str
    side: str
    source_message_id: int
    source_message_link: str | None = None
    source_message_date: datetime
    message_ids: list[int] = Field(default_factory=list)
    lifecycle_messages: list[dict[str, Any]] = Field(default_factory=list)
    events: list[BacktestEvent] = Field(default_factory=list)


class BacktestCandleArtifact(BaseModel):
    symbol: str
    path: str
    candle_count: int


class BacktestRunRequest(RealBacktestRunRequest):
    capital_per_signal: Decimal = Decimal("100")
    fill_policy: BacktestFillPolicy = BacktestFillPolicy.CONSERVATIVE
    leverage_source: Literal["signal_or_default", "fixed"] = "signal_or_default"
    fixed_leverage: int | None = None
    max_effective_leverage: Decimal = Decimal("50")
    default_signal_leverage: Decimal = Decimal("50")
    min_allocation_pct: Decimal = Decimal("2")
    max_allocation_pct: Decimal = Decimal("20")
    default_stop_pct: Decimal = Decimal("5")
    synthetic_stop_max_loss_pct_of_balance: Decimal = Decimal("5")
    max_stop_loss_pct_of_balance: Decimal = Decimal("5")
    fee_rate_pct: Decimal = Decimal("0.04")
    consolidation_seconds: int = 180
    close_open_positions_at_end: bool = False
    lifecycle_refresh_interval: str = "30m"
    max_parallel_signals: int = Field(default_factory=lambda: default_backtest_parallel_workers())
    include_not_filled_signals: bool = True

    @field_validator("capital_per_signal", "max_effective_leverage", "default_signal_leverage")
    @classmethod
    def _positive_decimal(cls, value: Decimal) -> Decimal:
        if value <= Decimal("0"):
            raise ValueError("value must be positive")
        return value

    @field_validator("min_allocation_pct", "max_allocation_pct", "default_stop_pct")
    @classmethod
    def _non_negative_decimal(cls, value: Decimal) -> Decimal:
        if value < Decimal("0"):
            raise ValueError("value must be non-negative")
        return value

    @field_validator(
        "synthetic_stop_max_loss_pct_of_balance",
        "max_stop_loss_pct_of_balance",
        "fee_rate_pct",
    )
    @classmethod
    def _allow_zero_decimal(cls, value: Decimal) -> Decimal:
        if value < Decimal("0"):
            raise ValueError("value must be non-negative")
        return value

    @field_validator("max_parallel_signals")
    @classmethod
    def _positive_parallelism(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_parallel_signals must be positive")
        return value

    @field_validator("consolidation_seconds")
    @classmethod
    def _non_negative_consolidation_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("consolidation_seconds must be non-negative")
        return value

    @field_validator("lifecycle_refresh_interval")
    @classmethod
    def _validate_lifecycle_interval(cls, value: str) -> str:
        interval_to_seconds(value)
        return value

    @model_validator(mode="after")
    def _validate_backtest_values(self) -> BacktestRunRequest:
        if self.max_allocation_pct < self.min_allocation_pct:
            raise ValueError("max_allocation_pct must be >= min_allocation_pct")
        if self.leverage_source == "fixed" and (
            self.fixed_leverage is None or self.fixed_leverage <= 0
        ):
            raise ValueError("fixed_leverage must be positive when leverage_source=fixed")
        return self


class BacktestResult(RealBacktestResult):
    run_type: Literal["backtest"] = "backtest"
    signals: list[dict[str, Any]] = Field(default_factory=list)
    aggregate: dict[str, Any] = Field(default_factory=dict)
    report_payload: dict[str, Any] | None = None


class BacktestCheckpoint(BaseModel):
    """Durable, non-secret state needed to restart a backtest pipeline phase."""

    resume_phase: Literal["classify_messages", "fetch_market_data", "simulate", "report"]
    messages: list[RawTelegramMessage] = Field(default_factory=list)
    real_telegram_used: bool = False
    events: list[BacktestEvent] = Field(default_factory=list)
    traces: list[RealBacktestMessageTrace] = Field(default_factory=list)
    signal_trace_map: dict[str, int] = Field(default_factory=dict)
    symbol_trace_map: dict[str, list[int]] = Field(default_factory=dict)
    records: list[BacktestSignalRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    ai_used: bool = False
    regex_fallback_used: bool = False


class BacktestRunner(RealBacktestRunner):
    checkpoint_callback: Callable[[BacktestCheckpoint], None] | None = None
    resume_checkpoint: BacktestCheckpoint | None = None

    async def run(
        self,
        request: RealBacktestRunRequest,
        *,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None = None,
    ) -> BacktestResult:
        backtest_request = (
            request
            if isinstance(request, BacktestRunRequest)
            else BacktestRunRequest.model_validate(request.model_dump(mode="python"))
        )
        self._log_sending_disabled_for_run = False
        self._run_started_at = datetime.now(timezone.utc)
        self._reset_phase_tracking()
        readiness = self.readiness()
        from_date, to_date = backtest_request.resolve_range()
        warnings: list[str] = []
        checkpoint = self.resume_checkpoint

        self._emit_run_progress(
            progress_callback,
            phase="starting",
            status="running",
            summary="Backtest created and waiting for readiness checks.",
        )
        if not readiness.ready:
            return self._write_backtest_failure(
                request=backtest_request,
                from_date=from_date,
                to_date=to_date,
                errors=readiness.issues,
            )

        selection = self._select_classifier(backtest_request.use_ai)
        if backtest_request.use_ai and not selection.ai_configured:
            return self._write_backtest_failure(
                request=backtest_request,
                from_date=from_date,
                to_date=to_date,
                errors=[
                    "AI gateway is required for this backtest run "
                    "but is not enabled."
                ],
            )

        self._emit_run_progress(
            progress_callback,
            phase="fetch_history",
            status="running",
            summary=(
                "Restoring saved backtest checkpoint."
                if checkpoint
                else "Fetching Telegram message history."
            ),
        )
        fetch_result: Any
        if checkpoint is not None:
            messages = checkpoint.messages
            fetch_result = SimpleNamespace(used_real_telegram=checkpoint.real_telegram_used)
        else:
            try:
                messages, fetch_result = await self.telegram_source.fetch(
                    channel=backtest_request.channel,
                    start=from_date,
                    end=to_date,
                    limit=min(
                        backtest_request.max_messages,
                        self.settings.REAL_BACKTEST_MAX_MESSAGES,
                    ),
                    start_message_id=backtest_request.start_message_id,
                )
            except Exception as exc:
                return self._write_backtest_failure(
                    request=backtest_request,
                    from_date=from_date,
                    to_date=to_date,
                    errors=[f"Telegram history fetch failed: {type(exc).__name__}"],
                )

        counts: dict[str, int] = checkpoint.counts.copy() if checkpoint is not None else {
            "history_steps_total": 1,
            "history_steps_completed": 1,
            "total_messages": len(messages),
            "classified_messages": 0,
            "parsed_signals": 0,
            "valid_signals": 0,
            "invalid_signals": 0,
            "ignored_messages": 0,
            "ambiguous_messages": 0,
            "ai_failed_messages": 0,
            "market_data_targets_total": 0,
            "market_data_targets_completed": 0,
            "simulation_targets_total": 0,
            "simulation_targets_completed": 0,
            "report_steps_total": 1,
            "report_steps_completed": 0,
            "trades_simulated": 0,
            "trades_filled": 0,
        }
        self._emit_run_progress(
            progress_callback,
            phase="fetch_history",
            status="completed",
            summary=f"Fetched {len(messages)} Telegram messages.",
            counts=counts,
        )

        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="running",
            summary="Building backtest signal records from channel history.",
            counts=counts,
        )
        (
            events,
            traces_by_message_id,
            signal_trace_map,
            symbol_trace_map,
            records_by_signal_id,
            counts,
        ) = await self._build_backtest_records(
            request=backtest_request,
            classifier=selection.classifier,
            messages=messages,
            progress_callback=progress_callback,
            counts=counts,
            warnings=warnings,
        )
        open_events = [event for event in events if event.action is SignalAction.OPEN]
        valid_open_events = [
            event
            for event in open_events
            if self.validator.validate_for_backtest_open(event.parsed_signal)[0]
        ]
        ai_used = any("classifier=ai" in event.debug_notes for event in events)
        regex_fallback_used = (
            any("ai-fallback=regex" in note for event in events for note in event.debug_notes)
            or isinstance(selection.classifier, RegexMessageClassifier)
        )
        if selection.warning:
            self._append_warning(warnings, selection.warning)
        self._publish_checkpoint(
            BacktestCheckpoint(
                resume_phase="fetch_market_data",
                messages=messages,
                real_telegram_used=fetch_result.used_real_telegram,
                events=events,
                traces=list(traces_by_message_id.values()),
                signal_trace_map=signal_trace_map,
                symbol_trace_map=symbol_trace_map,
                records=list(records_by_signal_id.values()),
                counts=counts,
                warnings=warnings,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
            )
        )

        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="completed",
            summary=(
                f"Classification complete: {counts['classified_messages']} processed, "
                f"{counts['valid_signals']} signal records ready."
            ),
            counts=counts,
        )

        if not messages:
            return self._write_backtest_failure(
                request=backtest_request,
                from_date=from_date,
                to_date=to_date,
                real_telegram_used=fetch_result.used_real_telegram,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
                errors=["No Telegram messages fetched for the requested range"],
                counts=counts,
                warnings=warnings,
            )

        if not records_by_signal_id:
            return self._write_backtest_failure(
                request=backtest_request,
                from_date=from_date,
                to_date=to_date,
                real_telegram_used=fetch_result.used_real_telegram,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
                errors=["No structurally valid signals were detected"],
                counts=counts,
                warnings=warnings,
            )

        with tempfile.TemporaryDirectory(prefix="triak_backtest_candles_") as artifact_dir:
            artifact_root = Path(artifact_dir)
            self._emit_run_progress(
                progress_callback,
                phase="fetch_market_data",
                status="running",
                summary=f"Fetching shared candle sets for {len(symbol_trace_map)} symbols.",
                counts=counts,
            )
            prefetched_candles_by_symbol: dict[str, list[Candle]] = {}
            prefetched_candle_ranges_by_symbol: dict[str, Any] = {}
            candle_artifacts_by_symbol: dict[str, list[BacktestCandleArtifact]] = {}
            skipped_reasons: list[str] = []
            real_market_data_used = False
            total_candles_fetched = 0
            counts["market_data_targets_total"] = len(symbol_trace_map)
            counts["market_data_targets_completed"] = 0
            candidate_map = self._build_symbol_candidates_by_primary(valid_open_events)
            fetch_concurrency = max(
                1,
                min(
                    self.settings.BACKTEST_MARKET_DATA_MAX_CONCURRENCY,
                    len(symbol_trace_map) or 1,
                ),
            )
            semaphore = asyncio.Semaphore(fetch_concurrency)

            async def fetch_symbol_market_data(
                symbol: str,
                message_ids: list[int],
            ) -> tuple[
                str,
                list[BacktestCandleArtifact] | None,
                str | None,
                list[str],
                str | None,
            ]:
                if not message_ids:
                    return symbol, None, None, [], None
                candidate_symbols = candidate_map.get(symbol, [symbol])
                range_start, range_end = self._market_data_range_for_symbol(
                    request=backtest_request,
                    message_ids=message_ids,
                    traces_by_message_id=traces_by_message_id,
                )
                requested_candles = self._estimated_candle_count(
                    start=range_start,
                    end=range_end,
                    interval=backtest_request.interval,
                )
                candle_budget = self.settings.BACKTEST_MAX_CANDLES_PER_SYMBOL
                segments = self._market_data_segments(
                    start=range_start,
                    end=range_end,
                    interval=backtest_request.interval,
                    max_candles=candle_budget,
                )
                started_at = datetime.now(timezone.utc)
                self._log_event(
                    logging.INFO,
                    "backtesting.backtest_market_data_fetch_started",
                    symbol=symbol,
                    interval=backtest_request.interval,
                    requested_candles=requested_candles,
                    segment_count=len(segments),
                    candles_per_segment=candle_budget,
                    timeout_seconds=self.settings.BACKTEST_MARKET_DATA_TIMEOUT_SECONDS,
                    range_start=range_start.isoformat(),
                    range_end=range_end.isoformat(),
                )
                artifacts: list[BacktestCandleArtifact] = []
                for segment_index, (segment_start, segment_end) in enumerate(segments, start=1):
                    try:
                        async with semaphore:
                            (
                                fetched,
                                selected_symbol,
                                _from_cache,
                                last_error_type,
                                no_data_candidates,
                            ) = await asyncio.wait_for(
                                self._ensure_prefetched_market_data(
                                    request=backtest_request,
                                    market_symbol=symbol,
                                    candidate_symbols=candidate_symbols,
                                    range_start=segment_start,
                                    range_end=segment_end,
                                    prefetched_candles_by_symbol=prefetched_candles_by_symbol,
                                    prefetched_candle_ranges_by_symbol=prefetched_candle_ranges_by_symbol,
                                ),
                                timeout=self.settings.BACKTEST_MARKET_DATA_TIMEOUT_SECONDS,
                            )
                    except TimeoutError:
                        self._log_event(
                            logging.WARNING,
                            "backtesting.backtest_market_data_fetch_timed_out",
                            symbol=symbol,
                            interval=backtest_request.interval,
                            requested_candles=requested_candles,
                            segment_index=segment_index,
                            segment_count=len(segments),
                            timeout_seconds=self.settings.BACKTEST_MARKET_DATA_TIMEOUT_SECONDS,
                        )
                        return symbol, None, "MarketDataFetchDeadlineExceeded", [], None
                    except Exception as exc:
                        self._log_event(
                            logging.WARNING,
                            "backtesting.backtest_market_data_fetch_failed",
                            symbol=symbol,
                            interval=backtest_request.interval,
                            error_type=type(exc).__name__,
                            requested_candles=requested_candles,
                            segment_index=segment_index,
                            segment_count=len(segments),
                        )
                        return symbol, None, type(exc).__name__, [], None
                    if not fetched:
                        return (
                            symbol,
                            None,
                            last_error_type,
                            no_data_candidates,
                            (
                                f"{symbol}: no complete candle replay; segment {segment_index} "
                                f"of {len(segments)} returned no data."
                            ),
                        )
                    artifact = _write_candle_artifact(
                        artifact_root=artifact_root,
                        symbol=selected_symbol,
                        artifact_key=f"{symbol}_{segment_index:04d}",
                        candles=fetched,
                    )
                    artifacts.append(artifact)
                    prefetched_candles_by_symbol.pop(selected_symbol, None)
                    prefetched_candle_ranges_by_symbol.pop(selected_symbol, None)
                self._log_event(
                    logging.INFO,
                    "backtesting.backtest_market_data_fetch_finished",
                    symbol=symbol,
                    interval=backtest_request.interval,
                    candle_count=sum(artifact.candle_count for artifact in artifacts),
                    segment_count=len(artifacts),
                    elapsed_ms=max(
                        0,
                        int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
                    ),
                )
                return symbol, artifacts, None, [], None

            fetch_tasks = [
                asyncio.create_task(fetch_symbol_market_data(symbol, message_ids))
                for symbol, message_ids in symbol_trace_map.items()
            ]
            for task in asyncio.as_completed(fetch_tasks):
                (
                    symbol,
                    artifacts,
                    last_error_type,
                    no_data_candidates,
                    skip_reason,
                ) = await task
                candidate_symbols = candidate_map.get(symbol, [symbol])
                if artifacts:
                    real_market_data_used = True
                    candle_artifacts_by_symbol[symbol] = artifacts
                    total_candles_fetched += sum(artifact.candle_count for artifact in artifacts)
                else:
                    attempted = ", ".join(no_data_candidates or candidate_symbols)
                    if skip_reason is not None:
                        skipped_reasons.append(skip_reason)
                    elif last_error_type is not None:
                        skipped_reasons.append(
                            f"{symbol}: candle fetch failed ({last_error_type})"
                        )
                    else:
                        skipped_reasons.append(
                            f"{symbol}: no candle data returned (tried: {attempted})"
                        )
                counts["market_data_targets_completed"] += 1
                self._emit_run_progress(
                    progress_callback,
                    phase="fetch_market_data",
                    status="running",
                    summary=(
                        f"Processed market data for "
                        f"{counts['market_data_targets_completed']} of "
                        f"{counts['market_data_targets_total']} symbols."
                    ),
                    counts=counts,
                )

            self._emit_run_progress(
                progress_callback,
                phase="fetch_market_data",
                status="completed" if candle_artifacts_by_symbol else "failed",
                summary=f"Fetched candle sets for {len(candle_artifacts_by_symbol)} symbols.",
                counts=counts,
            )
            if not candle_artifacts_by_symbol:
                return self._write_backtest_failure(
                    request=backtest_request,
                    from_date=from_date,
                    to_date=to_date,
                    real_telegram_used=fetch_result.used_real_telegram,
                    real_market_data_used=real_market_data_used,
                    ai_used=ai_used,
                    regex_fallback_used=regex_fallback_used,
                    errors=["No candle data available for backtest signal simulation"],
                    counts=counts,
                    warnings=warnings,
                    skipped_reasons=skipped_reasons,
                )

            self._emit_run_progress(
                progress_callback,
                phase="simulate",
                status="running",
                summary=(
                    f"Simulating {len(records_by_signal_id)} signals "
                    f"with up to {backtest_request.max_parallel_signals} workers."
                ),
                counts=counts,
            )
            counts["simulation_targets_total"] = sum(
                1
                for record in records_by_signal_id.values()
                if record.symbol in candle_artifacts_by_symbol
            )
            counts["simulation_targets_completed"] = 0
            signal_results = self._simulate_records_from_artifacts(
                request=backtest_request,
                records=list(records_by_signal_id.values()),
                candle_artifacts_by_symbol=candle_artifacts_by_symbol,
                progress_callback=progress_callback,
                counts=counts,
            )
        if not backtest_request.include_not_filled_signals:
            signal_results = [
                item for item in signal_results if str(item.get("status") or "") != "not_filled"
            ]

        for signal_id, message_id in signal_trace_map.items():
            trace = traces_by_message_id.get(message_id)
            signal_result = next(
                (item for item in signal_results if item.get("signal_id") == signal_id),
                None,
            )
            if trace is None or signal_result is None:
                continue
            trace.final_status = str(signal_result.get("status") or "no_trade")
            trace.result_summary = (
                f"Backtest result={signal_result.get('status')} "
                f"pnl={signal_result.get('total_pnl')}"
            )
            self._set_trace_stage(
                trace,
                "simulated",
                status="completed",
                detail=trace.result_summary,
            )
            self._set_trace_stage(
                trace,
                "finalized",
                status="completed",
                detail=trace.result_summary,
            )
            self._emit_message_progress(
                progress_callback,
                phase="simulate",
                summary=f"Backtest simulation finalized for message {message_id}.",
                counts=counts,
                trace=trace,
            )

        aggregate = self._build_backtest_aggregate(
            signals=signal_results,
            capital_per_signal=backtest_request.capital_per_signal,
        )
        result = BacktestResult(
            success=True,
            channel=backtest_request.channel,
            from_date=from_date,
            to_date=to_date,
            interval=backtest_request.interval,
            real_telegram_used=fetch_result.used_real_telegram,
            real_market_data_used=real_market_data_used,
            ai_used=ai_used,
            regex_fallback_used=regex_fallback_used,
            total_messages=len(messages),
            classified_messages=counts["classified_messages"],
            parsed_signals=len(open_events),
            valid_signals=counts["valid_signals"],
            invalid_signals=counts["invalid_signals"],
            ignored_messages=counts["ignored_messages"],
            ambiguous_messages=counts["ambiguous_messages"],
            ai_failed_messages=counts.get("ai_failed_messages", 0),
            symbols_found=sorted(
                {
                    str(item.get("symbol") or "")
                    for item in signal_results
                    if item.get("symbol")
                }
            ),
            candles_fetched=total_candles_fetched,
            trades_simulated=len(signal_results),
            trades_filled=int(aggregate["filled_signals"]),
            wins=int(aggregate["wins"]),
            losses=int(aggregate["losses"]),
            win_rate=Decimal(str(aggregate["win_rate"])),
            total_pnl=Decimal(str(aggregate["total_pnl"])),
            profit_factor=(
                Decimal(str(aggregate["profit_factor"]))
                if aggregate.get("profit_factor") is not None
                else None
            ),
            max_drawdown=Decimal(str(aggregate["max_drawdown"])),
            conservative_pnl=Decimal(str(aggregate["total_pnl"])),
            optimistic_pnl=Decimal(str(aggregate["total_pnl"])),
            channel_score=Decimal("0"),
            skipped_reasons=skipped_reasons,
            warnings=warnings,
            generated_at=datetime.now(timezone.utc),
            runtime_duration_ms=self._elapsed_runtime_ms(),
            phase_durations_ms=self._phase_durations_snapshot(),
            signals=signal_results,
            aggregate=aggregate,
        )
        result.report_payload = self._build_backtest_payload(
            result,
            request=backtest_request,
        )
        stored = self.report_store.write(result.report_payload)
        result.report_path = stored.json_path
        result.markdown_report_path = stored.markdown_path

        final_counts = {
            **counts,
            "trades_simulated": result.trades_simulated,
            "trades_filled": result.trades_filled,
        }
        self._emit_run_progress(
            progress_callback,
            phase="simulate",
            status="completed",
            summary=(
                f"Backtest simulation complete: {result.trades_simulated} signals, "
                f"{result.trades_filled} filled."
            ),
            counts=final_counts,
        )
        self._emit_run_progress(
            progress_callback,
            phase="report",
            status="running",
            summary="Writing backtest report artifacts.",
            counts={**final_counts, "report_steps_completed": 0},
        )
        self._emit_run_progress(
            progress_callback,
            phase="report",
            status="completed",
            summary=f"Report written to {result.report_path}.",
            counts={**final_counts, "report_steps_completed": 1},
            live_metrics={
                "live_open_positions": str(aggregate["open_signals"]),
                "live_closed_trades": str(aggregate["closed_signals"]),
                "live_wins": str(result.wins),
                "live_losses": str(result.losses),
                "live_realized_pnl": str(result.total_pnl),
                "live_unrealized_pnl": "0",
                "live_total_pnl": str(result.total_pnl),
                "live_realized_balance": str(aggregate["total_final_balance"]),
                "live_current_balance": str(aggregate["total_final_balance"]),
            },
            live_signals=signal_results,
        )
        result.phase_durations_ms = self._phase_durations_snapshot()
        result.report_payload = self._build_backtest_payload(
            result,
            request=backtest_request,
        )
        self.report_store.write(
            result.report_payload
            | {
                "report_path": result.report_path,
                "markdown_report_path": result.markdown_report_path,
            }
        )
        self._log_event(
            logging.INFO,
            "backtesting.backtest_run_completed",
            channel=request.channel,
            signal_count=result.trades_simulated,
            filled_count=result.trades_filled,
            total_pnl=str(result.total_pnl),
            report_path=result.report_path,
        )
        return result

    def _publish_checkpoint(self, checkpoint: BacktestCheckpoint) -> None:
        callback = getattr(self, "checkpoint_callback", None)
        if callback is not None:
            callback(checkpoint)

    @staticmethod
    def _estimated_candle_count(*, start: datetime, end: datetime, interval: str) -> int:
        duration_seconds = max(0, int((end - start).total_seconds()))
        interval_seconds = interval_to_seconds(interval)
        return max(1, (duration_seconds + interval_seconds - 1) // interval_seconds)

    @staticmethod
    def _market_data_segments(
        *,
        start: datetime,
        end: datetime,
        interval: str,
        max_candles: int,
    ) -> list[tuple[datetime, datetime]]:
        if end <= start:
            return [(start, end)]
        segment_duration = timedelta(seconds=max_candles * interval_to_seconds(interval))
        segments: list[tuple[datetime, datetime]] = []
        segment_start = start
        while segment_start < end:
            segment_end = min(end, segment_start + segment_duration)
            segments.append((segment_start, segment_end))
            segment_start = segment_end
        return segments

    async def _build_backtest_records(
        self,
        *,
        request: BacktestRunRequest,
        classifier: MessageClassifier,
        messages: list[RawTelegramMessage],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        warnings: list[str],
    ) -> tuple[
        list[BacktestEvent],
        dict[int, RealBacktestMessageTrace],
        dict[str, int],
        dict[str, list[int]],
        dict[str, BacktestSignalRecord],
        dict[str, int],
    ]:
        checkpoint = self.resume_checkpoint
        if checkpoint is not None and checkpoint.resume_phase == "fetch_market_data":
            return (
                checkpoint.events,
                {trace.message_id: trace for trace in checkpoint.traces},
                checkpoint.signal_trace_map,
                checkpoint.symbol_trace_map,
                {record.signal_id: record for record in checkpoint.records},
                checkpoint.counts.copy(),
            )
        context = ChannelContext(
            channel_id=request.channel,
            max_message_limit=max(
                request.max_messages,
                self.settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
            ),
            max_update_window_hours=max(1, self.settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS),
        )
        events: list[BacktestEvent] = []
        traces_by_message_id: dict[int, RealBacktestMessageTrace] = {}
        signal_trace_map: dict[str, int] = {}
        symbol_trace_map: dict[str, list[int]] = {}
        event_index_by_signal_id: dict[str, int] = {}
        parsed_by_message_id: dict[int, ParsedSignal] = {}
        raw_by_message_id: dict[int, RawTelegramMessage] = {}
        tracked_signal_ids: set[str] = set()
        closed_signal_ids: set[str] = set()
        records_by_signal_id: dict[str, BacktestSignalRecord] = {}
        sorted_messages = sorted(messages, key=lambda item: item.date)
        context.seed_message_catalog(sorted_messages)

        for message in sorted_messages:
            trace = self._make_trace(message)
            traces_by_message_id[message.message_id] = trace
            self._set_trace_stage(
                trace,
                "received",
                status="completed",
                detail="Message pulled from Telegram history.",
            )
            self._set_trace_stage(
                trace,
                "preprocess",
                status="active",
                detail="Preparing message payload for classification.",
            )
            message = await self._prepare_message_for_classification(
                message=message,
                context=context,
                trace=trace,
                progress_callback=progress_callback,
                counts=counts,
                warnings=warnings,
            )
            self._set_trace_stage(
                trace,
                "preprocess",
                status="completed",
                detail="Message payload prepared for classification.",
            )
            self._set_trace_stage(
                trace,
                "classified",
                status="active",
                detail="Classifier is analyzing this message.",
            )
            self._emit_message_progress(
                progress_callback,
                phase="classify_messages",
                summary=f"Reviewing message {message.message_id}.",
                counts=counts,
                trace=trace,
            )
            context.add_recent_message(message)

            if not self._message_has_processible_text(message):
                passive_signal = build_ignored_signal(
                    message,
                    invalid_reason="message has no text or caption",
                )
                trace.classification = "ignored"
                trace.parsed_action = passive_signal.action.value
                trace.confidence = "0"
                trace.final_status = "ignored"
                trace.result_summary = "Message has no text or caption; skipped from parsing."
                trace.debug_notes = ["classification_skipped=empty_message"]
                self._set_trace_stage(
                    trace,
                    "classified",
                    status="completed",
                    detail="Skipped because this Telegram message has no text or caption.",
                )
                self._mark_non_signal_trace(
                    trace,
                    "Message has no text or caption; used only as a record checkpoint.",
                )
                counts["ignored_messages"] += 1
                counts["classified_messages"] += 1
                self._emit_message_progress(
                    progress_callback,
                    phase="classify_messages",
                    summary=f"Message {message.message_id} skipped due to empty text.",
                    counts=counts,
                    trace=trace,
                )
                continue

            try:
                classified = classifier.classify(message, context)
            except Exception as exc:
                counts["ai_failed_messages"] += 1
                ai_failed_signal = build_ignored_signal(
                    message,
                    invalid_reason=f"ai_classification_error={type(exc).__name__}",
                )
                trace.classification = "ai_failed"
                trace.parsed_action = ai_failed_signal.action.value
                trace.confidence = "0"
                trace.final_status = "ai_failed"
                trace.result_summary = (
                    f"AI classification failed ({type(exc).__name__}); "
                    "message recorded as ai_failed and excluded from trading."
                )
                trace.debug_notes = ["classifier=ai", f"ai-error={type(exc).__name__}"]
                self._set_trace_stage(
                    trace,
                    "classified",
                    status="failed",
                    detail=f"AI classification raised {type(exc).__name__}.",
                )
                self._mark_non_signal_trace(trace, "AI classification failed; excluded.")
                counts["classified_messages"] += 1
                continue

            parsed = classified.parsed_signal
            parsed_by_message_id[message.message_id] = parsed
            raw_by_message_id[message.message_id] = message
            signal_id: str | None = None
            resolved_related_id: str | None = None
            parsed_for_event = parsed
            close_all = detect_close_all_instruction(message.text)
            if close_all and parsed_for_event.action is not SignalAction.CLOSE:
                parsed_for_event = parsed_for_event.model_copy(
                    update={"action": SignalAction.CLOSE}
                )

            reply_owner = context.find_signal_by_message_reply(message.reply_to_msg_id)
            if (
                classified.is_potential_new_signal
                and reply_owner is not None
                and self._message_matches_signal_identity(parsed_for_event, reply_owner)
                and self._is_backtest_signal_eligible_for_follow_up(
                    signal=reply_owner,
                    method="reply_to",
                    tracked_signal_ids=tracked_signal_ids,
                    closed_signal_ids=closed_signal_ids,
                )
            ):
                classified.is_potential_new_signal = False
                classified.is_related_to_existing_signal = True
                if not classified.related_signal_id:
                    classified.related_signal_id = reply_owner.signal_id
                classified.debug_notes.append(
                    f"rerouted_open_to_followup; reply_owner={reply_owner.signal_id}"
                )

            if classified.is_potential_new_signal:
                symbol_reuse_owner = self._find_reusable_backtest_signal_for_symbol(
                    context=context,
                    parsed=parsed_for_event,
                    message=message,
                    tracked_signal_ids=tracked_signal_ids,
                    closed_signal_ids=closed_signal_ids,
                )
                if symbol_reuse_owner is not None:
                    classified.is_potential_new_signal = False
                    classified.is_related_to_existing_signal = True
                    classified.related_signal_id = symbol_reuse_owner.signal_id
                    classified.debug_notes.append(
                        f"rerouted_open_to_followup; symbol_owner={symbol_reuse_owner.signal_id}"
                    )
                    parsed_for_event = parsed_for_event.model_copy(
                        update={"action": SignalAction.UPDATE_TP}
                    )
                    if parsed_for_event.stop_loss is None and not parsed_for_event.take_profits:
                        parsed_for_event = parsed_for_event.model_copy(
                            update={"action": SignalAction.UPDATE_ENTRY}
                        )

            if classified.is_potential_new_signal:
                signal_id = make_signal_id(message.channel_id, message.message_id)
                trace.signal_id = signal_id
                context.add_signal(
                    SignalState(
                        signal_id=signal_id,
                        channel_id=message.channel_id,
                        status=SignalStatus.PENDING_CONSOLIDATION,
                        created_from_message_id=message.message_id,
                        related_message_ids=[message.message_id],
                        current_signal=parsed,
                        version=1,
                        created_at=message.date,
                        updated_at=message.date,
                        expires_at=None,
                    ),
                    pending=True,
                )
            else:
                effective_action = normalize_related_signal_action(parsed, is_related=True)
                effective_action = apply_text_directive_action(effective_action, message.text)
                tp_list_values: list[Decimal] = []
                if effective_action in {SignalAction.UNKNOWN, SignalAction.IGNORE}:
                    tp_list_values = detect_tp_list_update(message.text)
                    if tp_list_values:
                        effective_action = SignalAction.UPDATE_TP
                if effective_action is not parsed.action:
                    update: dict[str, Any] = {"action": effective_action}
                    if tp_list_values:
                        update["take_profits"] = tp_list_values
                    parsed_for_event = parsed.model_copy(update=update)
                    classified.debug_notes.append(
                        f"normalized_follow_up_action={effective_action.value}"
                    )

                promoted_parent_id = self._maybe_promote_reply_parent_backtest(
                    message=message,
                    context=context,
                    parsed_by_message_id=parsed_by_message_id,
                    raw_by_message_id=raw_by_message_id,
                    events=events,
                    event_index_by_signal_id=event_index_by_signal_id,
                    tracked_signal_ids=tracked_signal_ids,
                    symbol_trace_map=symbol_trace_map,
                    signal_trace_map=signal_trace_map,
                    traces_by_message_id=traces_by_message_id,
                    records_by_signal_id=records_by_signal_id,
                    counts=counts,
                )
                if promoted_parent_id is not None:
                    classified.debug_notes.append(f"promoted_reply_parent={promoted_parent_id}")

                correlation = resolve_related_signal_id(
                    context=context,
                    parsed=parsed,
                    raw_related_id=classified.related_signal_id,
                    message=message,
                    action=effective_action,
                    allow_last_resort=self.settings.REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH,
                    signal_filter=lambda signal, method: (
                        self._is_backtest_signal_eligible_for_follow_up(
                            signal=signal,
                            method=method,
                            tracked_signal_ids=tracked_signal_ids,
                            closed_signal_ids=closed_signal_ids,
                        )
                    ),
                )
                if correlation.note:
                    classified.debug_notes.append(correlation.note)
                if correlation.signal_id is not None:
                    signal_id = correlation.signal_id
                    resolved_related_id = correlation.signal_id
                    trace.signal_id = signal_id
                    context.attach_message(signal_id, message)
                    context.merge_signal(signal_id, parsed, message.date)
                    classified.debug_notes.append(f"related_resolution={correlation.method}")
                elif effective_action in _FOLLOW_UP_ACTIONS:
                    classified.debug_notes.append(f"followup_unattached={effective_action.value}")
                    self._append_warning(
                        warnings,
                        f"Follow-up directive '{effective_action.value}' in message "
                        f"{message.message_id} could not be attached to any backtest signal.",
                    )

            trace.classification = self._classify_label(classified)
            trace.parsed_action = parsed_for_event.action.value
            trace.symbol = parsed.symbol
            trace.side = parsed.side.value
            trace.confidence = str(classified.confidence)
            trace.debug_notes = list(classified.debug_notes)
            self._set_trace_stage(
                trace,
                "classified",
                status="completed",
                detail=(
                    f"classification={trace.classification}, action={trace.parsed_action}, "
                    f"confidence={trace.confidence}"
                ),
            )

            if signal_id is not None:
                self._activate_backtest_signal(
                    context=context,
                    signal_id=signal_id,
                    traces_by_message_id=traces_by_message_id,
                    signal_trace_map=signal_trace_map,
                    event_index_by_signal_id=event_index_by_signal_id,
                    symbol_trace_map=symbol_trace_map,
                    tracked_signal_ids=tracked_signal_ids,
                    records_by_signal_id=records_by_signal_id,
                    counts=counts,
                )

            if classified.is_potential_new_signal and parsed_for_event.action is SignalAction.OPEN:
                counts["parsed_signals"] += 1
                signal_state = context.get_signal(signal_id or "")
                if signal_state is not None and signal_state.current_signal is not None:
                    parsed_for_event = signal_state.current_signal
                    trace.parsed_action = parsed_for_event.action.value
                    trace.symbol = parsed_for_event.symbol
                    trace.side = parsed_for_event.side.value
                if signal_id in tracked_signal_ids:
                    trace.final_status = "backtest_tracking"
                    trace.result_summary = (
                        "Signal validated and queued for simulation."
                    )
                    self._set_trace_stage(
                        trace,
                        "validated",
                        status="completed",
                        detail="Signal is structurally valid for simulation.",
                    )
                    self._set_trace_stage(
                        trace,
                        "market_data",
                        status="pending",
                        detail="Shared candle fetch will happen after record build completes.",
                    )
                    self._set_trace_stage(
                        trace,
                        "simulated",
                        status="pending",
                        detail="Waiting for simulation phase.",
                    )
                else:
                    counts["invalid_signals"] += 1
                    trace.final_status = "invalid_signal"
                    trace.result_summary = "Signal was not structurally valid."
                    self._set_trace_stage(
                        trace,
                        "validated",
                        status="failed",
                        detail=trace.result_summary,
                    )
                    self._set_trace_stage(
                        trace,
                        "market_data",
                        status="skipped",
                        detail="Skipped because signal validation failed.",
                    )
                    self._set_trace_stage(
                        trace,
                        "simulated",
                        status="skipped",
                        detail="Skipped because signal validation failed.",
                    )
                    self._set_trace_stage(
                        trace,
                        "finalized",
                        status="completed",
                        detail=trace.result_summary,
                    )
            elif parsed_for_event.action is SignalAction.IGNORE:
                counts["ignored_messages"] += 1
                trace.final_status = "ignored"
                trace.result_summary = "Message was ignored by the parser."
                self._mark_non_signal_trace(trace, "Ignored message; no signal record created.")
            elif parsed_for_event.action is SignalAction.UNKNOWN:
                counts["ambiguous_messages"] += 1
                trace.final_status = "ambiguous"
                trace.result_summary = "Message remained ambiguous after deterministic parsing."
                self._mark_non_signal_trace(trace, "Ambiguous message; no signal record created.")
            else:
                trace.final_status = "follow_up"
                trace.result_summary = f"Detected follow-up action: {parsed_for_event.action.value}"
                self._mark_non_signal_trace(trace, trace.result_summary)

            event = BacktestEvent(
                timestamp=message.date,
                action=parsed_for_event.action,
                signal_id=signal_id if classified.is_potential_new_signal else None,
                parsed_signal=parsed_for_event,
                related_signal_id=resolved_related_id,
                debug_notes=list(trace.debug_notes),
                source_message_id=message.message_id,
                source_text=message.text,
                close_fraction=extract_close_fraction(message.text),
                close_all=close_all,
                move_stop_to_entry=detect_move_stop_to_entry(message.text),
                leverage=parsed_for_event.leverage,
            )
            events.append(event)
            if signal_id is not None and classified.is_potential_new_signal:
                event_index_by_signal_id[signal_id] = len(events) - 1
                record = records_by_signal_id.get(signal_id)
                if record is not None:
                    record.events.append(event)
                    record.message_ids.append(message.message_id)
                    record.lifecycle_messages.append(
                        self._record_lifecycle_message(message, parsed_for_event, "open")
                    )
            elif resolved_related_id is not None:
                record = records_by_signal_id.get(resolved_related_id)
                if record is not None:
                    record.events.append(
                        event.model_copy(update={"signal_id": resolved_related_id})
                    )
                    record.message_ids.append(message.message_id)
                    record.lifecycle_messages.append(
                        self._record_lifecycle_message(
                            message,
                            parsed_for_event,
                            parsed_for_event.action.value,
                        )
                    )
                    if parsed_for_event.action in {SignalAction.CLOSE, SignalAction.CANCEL}:
                        closed_signal_ids.add(resolved_related_id)
            if close_all:
                for record in records_by_signal_id.values():
                    if record.source_message_date <= message.date:
                        record.events.append(
                            event.model_copy(update={"signal_id": record.signal_id})
                        )
                        record.lifecycle_messages.append(
                            self._record_lifecycle_message(message, parsed_for_event, "close_all")
                        )
                        closed_signal_ids.add(record.signal_id)

            counts["classified_messages"] += 1
            self._emit_message_progress(
                progress_callback,
                phase="classify_messages",
                summary=f"Message {message.message_id} classified.",
                counts=counts,
                trace=trace,
            )
        self._apply_live_consolidation(
            request=request,
            events=events,
            records_by_signal_id=records_by_signal_id,
            traces_by_message_id=traces_by_message_id,
            symbol_trace_map=symbol_trace_map,
            tracked_signal_ids=tracked_signal_ids,
            counts=counts,
        )
        return (
            events,
            traces_by_message_id,
            signal_trace_map,
            symbol_trace_map,
            records_by_signal_id,
            counts,
        )

    @staticmethod
    def _record_lifecycle_message(
        message: RawTelegramMessage,
        parsed_signal: ParsedSignal,
        kind: str,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "message_id": message.message_id,
            "timestamp": message.date.isoformat(),
            "timestamp_tehran": message.date.astimezone(TEHRAN_TZ).isoformat(),
            "action": parsed_signal.action.value,
            "symbol": parsed_signal.symbol,
            "preview": (message.text or "")[:200],
        }

    def _activate_backtest_signal(
        self,
        *,
        context: ChannelContext,
        signal_id: str,
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        signal_trace_map: dict[str, int],
        event_index_by_signal_id: dict[str, int],
        symbol_trace_map: dict[str, list[int]],
        tracked_signal_ids: set[str],
        records_by_signal_id: dict[str, BacktestSignalRecord],
        counts: dict[str, int],
    ) -> None:
        signal_state = context.get_signal(signal_id)
        if signal_state is None or signal_state.current_signal is None:
            return
        merged_signal = signal_state.current_signal
        if merged_signal.action is not SignalAction.OPEN:
            merged_signal = merged_signal.model_copy(update={"action": SignalAction.OPEN})
        merged_signal, geometry_error = self.validator.normalize_for_execution(
            merged_signal
        )
        signal_state.current_signal = merged_signal
        base_message_id = signal_state.created_from_message_id
        base_trace = traces_by_message_id.get(base_message_id)
        if base_trace is None:
            return
        base_trace.signal_id = signal_id
        base_trace.symbol = merged_signal.symbol
        base_trace.side = merged_signal.side.value
        base_trace.confidence = str(merged_signal.confidence)
        valid_for_backtest, _errors = self.validator.validate_for_backtest_open(merged_signal)
        market_symbol = (
            normalize_market_symbol(merged_signal.symbol)
            if merged_signal.symbol
            else None
        )
        if geometry_error is not None:
            base_trace.debug_notes.append(f"execution_geometry_rejected={geometry_error}")
        if geometry_error is not None or not valid_for_backtest or market_symbol is None:
            return
        if signal_id not in tracked_signal_ids:
            counts["valid_signals"] += 1
        tracked_signal_ids.add(signal_id)
        signal_trace_map[signal_id] = base_message_id
        symbol_trace_map.setdefault(market_symbol, [])
        if base_message_id not in symbol_trace_map[market_symbol]:
            symbol_trace_map[market_symbol].append(base_message_id)
        records_by_signal_id.setdefault(
            signal_id,
            BacktestSignalRecord(
                signal_id=signal_id,
                channel_id=signal_state.channel_id,
                symbol=market_symbol,
                side=merged_signal.side.value,
                source_message_id=base_message_id,
                source_message_link=base_trace.message_link,
                source_message_date=base_trace.message_date,
            ),
        ).symbol = market_symbol

    def _apply_live_consolidation(
        self,
        *,
        request: BacktestRunRequest,
        events: list[BacktestEvent],
        records_by_signal_id: dict[str, BacktestSignalRecord],
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        symbol_trace_map: dict[str, list[int]],
        tracked_signal_ids: set[str],
        counts: dict[str, int],
    ) -> None:
        """Delay and merge pending signals exactly as the live consolidation phase does."""

        mergeable_actions = {
            SignalAction.OPEN,
            SignalAction.UPDATE_SL,
            SignalAction.UPDATE_TP,
            SignalAction.UPDATE_LEVERAGE,
            SignalAction.UPDATE_ENTRY,
        }
        invalid_signal_ids: set[str] = set()
        global_open_replacements: dict[tuple[str, int], BacktestEvent] = {}
        consolidated_followups: set[tuple[str, int]] = set()
        for signal_id, record in records_by_signal_id.items():
            open_index = next(
                (
                    index
                    for index, event in enumerate(record.events)
                    if event.action is SignalAction.OPEN
                    and (event.signal_id == signal_id or event.related_signal_id is None)
                ),
                None,
            )
            if open_index is None:
                continue
            open_event = record.events[open_index]
            deadline = open_event.timestamp + timedelta(
                seconds=request.consolidation_seconds
            )
            merged_signal = open_event.parsed_signal
            merged_message_ids: list[int] = []
            for followup in sorted(record.events, key=lambda item: item.timestamp):
                if followup is open_event or followup.timestamp > deadline:
                    continue
                if followup.action not in mergeable_actions:
                    continue
                merged_signal = merge_parsed_signals(
                    merged_signal,
                    followup.parsed_signal,
                ).model_copy(update={"action": SignalAction.OPEN})
                if followup.source_message_id is not None:
                    merged_message_ids.append(followup.source_message_id)
                    consolidated_followups.add(
                        (signal_id, followup.source_message_id)
                    )

            normalized, geometry_error = self.validator.normalize_for_execution(
                merged_signal.model_copy(update={"action": SignalAction.OPEN})
            )
            structurally_valid, validation_errors = (
                self.validator.validate_for_backtest_open(normalized)
            )
            base_trace = traces_by_message_id.get(record.source_message_id)
            if geometry_error is not None or not structurally_valid:
                invalid_signal_ids.add(signal_id)
                if base_trace is not None:
                    detail = geometry_error or ", ".join(validation_errors)
                    base_trace.final_status = "invalid_signal"
                    base_trace.result_summary = (
                        f"Signal rejected at live-style consolidation: {detail}"
                    )
                    base_trace.debug_notes.append(
                        f"consolidation_rejected={detail}"
                    )
                continue

            notes = [
                *open_event.debug_notes,
                f"live_consolidation_seconds={request.consolidation_seconds}",
            ]
            if merged_message_ids:
                notes.append(
                    "consolidated_update_message_ids="
                    + ",".join(str(item) for item in merged_message_ids)
                )
            consolidated_open = open_event.model_copy(
                update={
                    "timestamp": deadline,
                    "parsed_signal": normalized,
                    "leverage": normalized.leverage,
                    "debug_notes": notes,
                }
            )
            record.events[open_index] = consolidated_open
            record.events[:] = [
                event
                for event in record.events
                if (
                    event is consolidated_open
                    or (
                        signal_id,
                        event.source_message_id or -1,
                    )
                    not in consolidated_followups
                )
            ]
            record.symbol = normalize_market_symbol(normalized.symbol) or record.symbol
            record.side = normalized.side.value
            global_open_replacements[(signal_id, record.source_message_id)] = (
                consolidated_open
            )
            if base_trace is not None:
                base_trace.symbol = normalized.symbol
                base_trace.side = normalized.side.value
                base_trace.debug_notes.extend(
                    note for note in notes if note not in base_trace.debug_notes
                )

        if invalid_signal_ids:
            for signal_id in invalid_signal_ids:
                removed_record = (
                    records_by_signal_id.pop(signal_id)
                    if signal_id in records_by_signal_id
                    else None
                )
                tracked_signal_ids.discard(signal_id)
                if removed_record is not None:
                    message_ids = symbol_trace_map.get(removed_record.symbol, [])
                    symbol_trace_map[removed_record.symbol] = [
                        item
                        for item in message_ids
                        if item != removed_record.source_message_id
                    ]
                    if not symbol_trace_map[removed_record.symbol]:
                        symbol_trace_map.pop(removed_record.symbol, None)
            counts["valid_signals"] = max(
                0,
                counts["valid_signals"] - len(invalid_signal_ids),
            )
            counts["invalid_signals"] += len(invalid_signal_ids)

        updated_events: list[BacktestEvent] = []
        for event in events:
            if (
                event.signal_id in invalid_signal_ids
                or event.related_signal_id in invalid_signal_ids
            ):
                continue
            owner_signal_id = event.related_signal_id or event.signal_id or ""
            if (
                owner_signal_id,
                event.source_message_id or -1,
            ) in consolidated_followups:
                continue
            key = (
                event.signal_id or "",
                event.source_message_id or -1,
            )
            updated_events.append(global_open_replacements.get(key, event))
        events[:] = updated_events

    @staticmethod
    def _is_backtest_signal_eligible_for_follow_up(
        *,
        signal: SignalState,
        method: str,
        tracked_signal_ids: set[str],
        closed_signal_ids: set[str],
    ) -> bool:
        if signal.signal_id in closed_signal_ids:
            return False
        if signal.signal_id in tracked_signal_ids:
            return True
        return method in {"reply_to", "reply_chain"} and signal.current_signal is not None

    @staticmethod
    def _find_reusable_backtest_signal_for_symbol(
        *,
        context: ChannelContext,
        parsed: ParsedSignal,
        message: RawTelegramMessage,
        tracked_signal_ids: set[str],
        closed_signal_ids: set[str],
    ) -> SignalState | None:
        if parsed.action is not SignalAction.OPEN or parsed.symbol is None:
            return None
        candidates = [
            signal
            for signal in context.find_signals_by_symbol(parsed.symbol)
            if signal.signal_id in tracked_signal_ids
            and signal.signal_id not in closed_signal_ids
            and signal.current_signal is not None
            and signal.created_from_message_id != message.message_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda signal: signal.updated_at)

    def _maybe_promote_reply_parent_backtest(
        self,
        *,
        message: RawTelegramMessage,
        context: ChannelContext,
        parsed_by_message_id: dict[int, ParsedSignal],
        raw_by_message_id: dict[int, RawTelegramMessage],
        events: list[BacktestEvent],
        event_index_by_signal_id: dict[str, int],
        tracked_signal_ids: set[str],
        symbol_trace_map: dict[str, list[int]],
        signal_trace_map: dict[str, int],
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        records_by_signal_id: dict[str, BacktestSignalRecord],
        counts: dict[str, int],
    ) -> str | None:
        parent_id = message.reply_to_msg_id
        if parent_id is None:
            return None
        parent_parsed = parsed_by_message_id.get(parent_id)
        parent_raw = raw_by_message_id.get(parent_id)
        if parent_parsed is None or parent_raw is None:
            return None
        if parent_parsed.action is not SignalAction.OPEN or not parent_parsed.symbol:
            return None
        if parent_id in context.signal_by_message_id:
            return None
        signal_id = make_signal_id(parent_raw.channel_id, parent_id)
        if context.get_signal(signal_id) is not None:
            return None
        valid_for_backtest, _errors = self.validator.validate_for_backtest_open(parent_parsed)
        market_symbol = normalize_market_symbol(parent_parsed.symbol)
        if not valid_for_backtest or market_symbol is None:
            return None
        base_trace = traces_by_message_id.get(parent_id)
        if base_trace is None:
            return None
        context.add_signal(
            SignalState(
                signal_id=signal_id,
                channel_id=parent_raw.channel_id,
                status=SignalStatus.PENDING_CONSOLIDATION,
                created_from_message_id=parent_id,
                related_message_ids=[parent_id],
                current_signal=parent_parsed,
                version=1,
                created_at=parent_raw.date,
                updated_at=parent_raw.date,
                expires_at=None,
            ),
            pending=True,
        )
        event = BacktestEvent(
            timestamp=parent_raw.date,
            action=parent_parsed.action,
            signal_id=signal_id,
            parsed_signal=parent_parsed,
            related_signal_id=None,
            debug_notes=["promoted_from_reply"],
            source_message_id=parent_id,
            source_text=parent_raw.text,
            close_fraction=extract_close_fraction(parent_raw.text),
            move_stop_to_entry=detect_move_stop_to_entry(parent_raw.text),
            leverage=parent_parsed.leverage,
        )
        events.append(event)
        event_index_by_signal_id[signal_id] = len(events) - 1
        signal_trace_map[signal_id] = parent_id
        tracked_signal_ids.add(signal_id)
        symbol_trace_map.setdefault(market_symbol, []).append(parent_id)
        records_by_signal_id[signal_id] = BacktestSignalRecord(
            signal_id=signal_id,
            channel_id=parent_raw.channel_id,
            symbol=market_symbol,
            side=parent_parsed.side.value,
            source_message_id=parent_id,
            source_message_link=base_trace.message_link,
            source_message_date=base_trace.message_date,
            message_ids=[parent_id],
            lifecycle_messages=[
                self._record_lifecycle_message(parent_raw, parent_parsed, "promoted_open")
            ],
            events=[event],
        )
        counts["parsed_signals"] += 1
        counts["valid_signals"] += 1
        return signal_id

    def _simulate_records_from_artifacts(
        self,
        *,
        request: BacktestRunRequest,
        records: list[BacktestSignalRecord],
        candle_artifacts_by_symbol: dict[str, list[BacktestCandleArtifact]],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        if not records:
            return []
        records_by_symbol: dict[str, list[BacktestSignalRecord]] = defaultdict(list)
        for record in records:
            if record.symbol in candle_artifacts_by_symbol:
                records_by_symbol[record.symbol].append(record)
        jobs = [
            {
                "symbol": symbol,
                "records": [record.model_dump(mode="python") for record in symbol_records],
                "artifacts": [
                    artifact.model_dump(mode="python")
                    for artifact in candle_artifacts_by_symbol[symbol]
                ],
                "request": request.model_dump(mode="python"),
                "strategy_key": self.strategy_key or "default_risk_managed",
            }
            for symbol, symbol_records in records_by_symbol.items()
        ]
        total_records = sum(len(job["records"]) for job in jobs)
        counts["simulation_targets_total"] = total_records
        counts["simulation_targets_completed"] = 0
        available_parallelism = default_backtest_parallel_workers()
        worker_count = max(
            1,
            min(
                request.max_parallel_signals,
                len(jobs),
                available_parallelism,
            ),
        )
        if worker_count <= 1 or len(jobs) <= 1:
            results: list[dict[str, Any]] = []
            completed = 0
            for job in jobs:
                batch_results = _simulate_backtest_symbol_artifact_job(job)
                results.extend(batch_results)
                completed += len(batch_results)
                counts["trades_simulated"] = completed
                counts["simulation_targets_completed"] = completed
                counts["trades_filled"] = sum(
                    1 for item in results if str(item.get("status") or "") != "not_filled"
                )
                self._emit_run_progress(
                    progress_callback,
                    phase="simulate",
                    status="running",
                    summary=f"Simulated {completed} of {total_records} signals.",
                    counts=counts,
                )
            return _sort_signal_results(results)

        results_by_symbol: dict[int, list[dict[str, Any]]] = {}
        cpu_assignments = _build_backtest_worker_cpu_assignments(worker_count)
        mp_context = backtest_worker_multiprocessing_context()
        cpu_id_queue = mp_context.Queue()
        for cpu_id in cpu_assignments:
            cpu_id_queue.put(cpu_id)
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=mp_context,
            initializer=_initialize_backtest_worker,
            initargs=(cpu_id_queue,),
        ) as executor:
            future_map = {
                executor.submit(_simulate_backtest_symbol_artifact_job, job): index
                for index, job in enumerate(jobs)
            }
            completed = 0
            for future in as_completed(future_map):
                index = future_map[future]
                batch_results = future.result()
                results_by_symbol[index] = batch_results
                completed += len(batch_results)
                partial_results = [
                    item
                    for batch in results_by_symbol.values()
                    for item in batch
                ]
                counts["trades_simulated"] = completed
                counts["simulation_targets_completed"] = completed
                counts["trades_filled"] = sum(
                    1
                    for item in partial_results
                    if str(item.get("status") or "") != "not_filled"
                )
                self._emit_run_progress(
                    progress_callback,
                    phase="simulate",
                    status="running",
                    summary=f"Simulated {completed} of {total_records} signals.",
                    counts=counts,
                )
        ordered_results = [
            item
            for index in sorted(results_by_symbol)
            for item in results_by_symbol[index]
        ]
        return _sort_signal_results(ordered_results)

    def _build_backtest_payload(
        self,
        result: BacktestResult,
        *,
        request: BacktestRunRequest,
    ) -> dict[str, Any]:
        payload = result.model_dump(mode="json")
        payload["strategy_key"] = self.strategy_key or "default_risk_managed"
        if self.strategy_key is not None:
            payload["strategy"] = describe_strategy_by_key(self.strategy_key)
        payload["request"] = request.model_dump(mode="json")
        payload["score_reason"] = "backtest independent signal aggregation"
        return payload

    def _write_backtest_failure(
        self,
        *,
        request: BacktestRunRequest,
        from_date: datetime,
        to_date: datetime,
        errors: list[str],
        counts: dict[str, int] | None = None,
        warnings: list[str] | None = None,
        skipped_reasons: list[str] | None = None,
        real_telegram_used: bool = False,
        real_market_data_used: bool = False,
        ai_used: bool = False,
        regex_fallback_used: bool = False,
    ) -> BacktestResult:
        counts = counts or {}
        result = BacktestResult(
            success=False,
            channel=request.channel,
            from_date=from_date,
            to_date=to_date,
            interval=request.interval,
            real_telegram_used=real_telegram_used,
            real_market_data_used=real_market_data_used,
            ai_used=ai_used,
            regex_fallback_used=regex_fallback_used,
            total_messages=int(counts.get("total_messages", 0)),
            classified_messages=int(counts.get("classified_messages", 0)),
            parsed_signals=int(counts.get("parsed_signals", 0)),
            valid_signals=int(counts.get("valid_signals", 0)),
            invalid_signals=int(counts.get("invalid_signals", 0)),
            ignored_messages=int(counts.get("ignored_messages", 0)),
            ambiguous_messages=int(counts.get("ambiguous_messages", 0)),
            ai_failed_messages=int(counts.get("ai_failed_messages", 0)),
            symbols_found=[],
            candles_fetched=0,
            trades_simulated=int(counts.get("trades_simulated", 0)),
            trades_filled=int(counts.get("trades_filled", 0)),
            wins=0,
            losses=0,
            win_rate=Decimal("0"),
            total_pnl=Decimal("0"),
            profit_factor=None,
            max_drawdown=Decimal("0"),
            conservative_pnl=Decimal("0"),
            optimistic_pnl=Decimal("0"),
            channel_score=Decimal("0"),
            skipped_reasons=skipped_reasons or [],
            errors=errors,
            warnings=warnings or [],
            generated_at=datetime.now(timezone.utc),
            runtime_duration_ms=self._elapsed_runtime_ms(),
            phase_durations_ms=self._phase_durations_snapshot(),
            signals=[],
            aggregate=self._build_backtest_aggregate(
                signals=[],
                capital_per_signal=request.capital_per_signal,
            ),
        )
        result.report_payload = self._build_backtest_payload(result, request=request)
        stored = self.report_store.write(result.report_payload)
        result.report_path = stored.json_path
        result.markdown_report_path = stored.markdown_path
        return result

    @staticmethod
    def _build_backtest_aggregate(
        *,
        signals: list[dict[str, Any]],
        capital_per_signal: Decimal,
    ) -> dict[str, Any]:
        filled = [item for item in signals if str(item.get("status") or "") != "not_filled"]
        closed = [
            item
            for item in filled
            if str(item.get("status_group") or "") != "active"
        ]
        wins = sum(
            1
            for item in closed
            if Decimal(str(item.get("total_pnl") or "0")) > Decimal("0")
        )
        losses = sum(
            1
            for item in closed
            if Decimal(str(item.get("total_pnl") or "0")) < Decimal("0")
        )
        open_signals = sum(1 for item in signals if str(item.get("status_group") or "") == "active")
        not_filled = sum(1 for item in signals if str(item.get("status") or "") == "not_filled")
        pnls = [Decimal(str(item.get("total_pnl") or "0")) for item in signals]
        pnl_pcts = [Decimal(str(item.get("total_pnl_pct") or "0")) for item in signals]
        gross_profit = sum((value for value in pnls if value > 0), Decimal("0"))
        gross_loss = sum((value for value in pnls if value < 0), Decimal("0"))
        total_pnl = sum(pnls, Decimal("0"))
        final_balance = (capital_per_signal * Decimal(len(signals))) + total_pnl
        equity_curve = _backtest_equity_curve(
            signals=signals,
            capital_per_signal=capital_per_signal,
        )
        period_pnl = _backtest_period_pnl(signals)
        return {
            "total_signals": len(signals),
            "filled_signals": len(filled),
            "closed_signals": len(closed),
            "open_signals": open_signals,
            "not_filled_signals": not_filled,
            "wins": wins,
            "losses": losses,
            "status_counts": _status_counts(signals),
            "total_pnl": decimal_to_plain_string(total_pnl) or "0",
            "avg_pnl": decimal_to_plain_string(_avg_decimal(pnls)) or "0",
            "median_pnl": decimal_to_plain_string(_median_decimal(pnls)) or "0",
            "avg_pnl_pct": decimal_to_plain_string(_avg_decimal(pnl_pcts)) or "0",
            "median_pnl_pct": decimal_to_plain_string(_median_decimal(pnl_pcts)) or "0",
            "gross_profit": decimal_to_plain_string(gross_profit) or "0",
            "gross_loss": decimal_to_plain_string(gross_loss) or "0",
            "profit_factor": (
                decimal_to_plain_string(gross_profit / abs(gross_loss))
                if gross_loss != Decimal("0")
                else None
            ),
            "win_rate": decimal_to_plain_string(
                (Decimal(wins) / Decimal(len(closed))) if closed else Decimal("0")
            ) or "0",
            "max_drawdown": decimal_to_plain_string(_equity_max_drawdown(equity_curve)) or "0",
            "total_initial_balance": decimal_to_plain_string(
                capital_per_signal * Decimal(len(signals))
            )
            or "0",
            "total_final_balance": decimal_to_plain_string(final_balance) or "0",
            "period_pnl": period_pnl,
            "symbol_summary": _backtest_symbol_summary(signals),
            "equity_curve": equity_curve,
            "best_signals": _best_or_worst_signals(signals, reverse=True),
            "worst_signals": _best_or_worst_signals(signals, reverse=False),
        }


def _simulate_backtest_symbol_artifact_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    records = [
        BacktestSignalRecord.model_validate(item)
        for item in job["records"]
    ]
    artifacts = [
        BacktestCandleArtifact.model_validate(item)
        for item in job["artifacts"]
    ]
    request = BacktestRunRequest.model_validate(job["request"])
    strategy_key = str(job["strategy_key"])
    simulator = BacktestSimulator()
    strategy = load_strategy(strategy_key)
    results: list[dict[str, Any]] = []
    for record in records:
        events = list(record.events)
        if request.leverage_source == "fixed":
            events = [
                event.model_copy(update={"leverage": request.fixed_leverage})
                for event in record.events
            ]
        state = None
        trades: list[Any] = []
        final_balance = request.capital_per_signal
        snapshots: list[Any] = []
        for artifact_index, artifact in enumerate(artifacts):
            candles = _load_candle_artifact(artifact)
            trades, final_balance, snapshots, state = simulator.simulate_live_preview_incremental(
                events=events,
                candles=candles,
                initial_balance=request.capital_per_signal,
                risk_per_trade_pct=request.risk_per_trade_pct,
                fill_policy=request.fill_policy,
                active_signal_hours=None,
                max_effective_leverage=request.max_effective_leverage,
                min_allocation_pct=request.min_allocation_pct,
                max_allocation_pct=request.max_allocation_pct,
                default_stop_pct=request.default_stop_pct,
                synthetic_stop_max_loss_pct_of_balance=request.synthetic_stop_max_loss_pct_of_balance,
                max_stop_loss_pct_of_balance=request.max_stop_loss_pct_of_balance,
                strategy=strategy,
                fee_rate_pct=request.fee_rate_pct,
                default_signal_leverage=request.default_signal_leverage,
                previous_state=state,
                close_open_positions_at_end=(
                    request.close_open_positions_at_end and artifact_index == len(artifacts) - 1
                ),
            )
        signal_state = None
        if snapshots:
            signal_state = snapshots[-1].signal_states.get(record.signal_id)
        trade = next((item for item in trades if item.signal_id == record.signal_id), None)
        results.append(
            _serialize_backtest_signal_result(
                record=record,
                trade=trade,
                signal_state=signal_state,
                snapshots=snapshots,
                initial_balance=request.capital_per_signal,
                final_balance=final_balance,
            )
        )
    return results


def _serialize_backtest_signal_result(
    *,
    record: BacktestSignalRecord,
    trade: Any | None,
    signal_state: Any | None,
    snapshots: list[Any],
    initial_balance: Decimal,
    final_balance: Decimal,
) -> dict[str, Any]:
    if signal_state is not None:
        lifecycle = RealBacktestRunner._signal_lifecycle_events(signal_state)
        lifecycle.extend(record.lifecycle_messages)
        total_pnl = signal_state.realized_pnl + signal_state.unrealized_pnl
        chart_payload = _build_backtest_chart_payload(signal_state)
        return {
            "signal_id": signal_state.signal_id,
            "symbol": signal_state.symbol,
            "side": signal_state.side.value,
            "status": signal_state.status,
            "status_group": "active" if signal_state.status == "open" else "inactive",
            "entry_time": signal_state.entry_time.isoformat(),
            "entry_time_tehran": signal_state.entry_time.astimezone(TEHRAN_TZ).isoformat(),
            "exit_time": signal_state.exit_time.isoformat() if signal_state.exit_time else None,
            "exit_time_tehran": (
                signal_state.exit_time.astimezone(TEHRAN_TZ).isoformat()
                if signal_state.exit_time
                else None
            ),
            "entry_price": (
                format_decimal(signal_state.entry_price)
                if signal_state.entry_price is not None
                else None
            ),
            "exit_price": (
                format_decimal(signal_state.exit_price)
                if signal_state.exit_price is not None
                else None
            ),
            "stop_loss": (
                format_decimal(signal_state.stop_loss)
                if signal_state.stop_loss is not None
                else None
            ),
            "take_profits": [format_decimal(item) or "0" for item in signal_state.take_profits],
            "open_quantity": format_decimal(signal_state.open_quantity),
            "original_quantity": format_decimal(signal_state.original_quantity),
            "realized_pnl": format_decimal(signal_state.realized_pnl),
            "unrealized_pnl": format_decimal(signal_state.unrealized_pnl),
            "total_pnl": format_decimal(total_pnl),
            "total_pnl_pct": format_decimal(signal_state.total_pnl_pct),
            "margin_pnl_pct": format_decimal(signal_state.margin_pnl_pct),
            "declared_leverage": (
                format_decimal(signal_state.declared_leverage)
                if signal_state.declared_leverage is not None
                else None
            ),
            "effective_leverage": format_decimal(signal_state.effective_leverage),
            "leverage": (
                format_decimal(signal_state.declared_leverage)
                if signal_state.declared_leverage is not None
                else format_decimal(signal_state.effective_leverage)
            ),
            "margin": format_decimal(signal_state.margin),
            "balance_basis": format_decimal(signal_state.balance_basis),
            "initial_balance": format_decimal(initial_balance),
            "final_balance": format_decimal(final_balance),
            "targets_hit": signal_state.targets_hit,
            "message_link": record.source_message_link,
            "source_message_id": record.source_message_id,
            "message_ids": list(dict.fromkeys(record.message_ids)),
            "related_message_count": len(set(record.message_ids)),
            "lifecycle": lifecycle,
            "chart": chart_payload,
            "notes": list(signal_state.notes),
            "checkpoints": [
                {
                    "timestamp": snapshot.timestamp.isoformat(),
                    "timestamp_tehran": snapshot.timestamp.astimezone(TEHRAN_TZ).isoformat(),
                    "kind": snapshot.checkpoint_kind,
                    "current_balance": format_decimal(snapshot.current_balance),
                    "total_pnl": format_decimal(snapshot.total_pnl),
                }
                for snapshot in snapshots
            ],
        }
    trade_status = trade.status if trade is not None else "not_filled"
    trade_pnl = trade.pnl if trade is not None else Decimal("0")
    trade_pnl_pct = trade.pnl_pct if trade is not None else Decimal("0")
    return {
        "signal_id": record.signal_id,
        "symbol": record.symbol,
        "side": record.side,
        "status": trade_status,
        "status_group": "inactive",
        "entry_time": record.source_message_date.isoformat(),
        "entry_time_tehran": record.source_message_date.astimezone(TEHRAN_TZ).isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade and trade.exit_time else None,
        "exit_time_tehran": (
            trade.exit_time.astimezone(TEHRAN_TZ).isoformat()
            if trade and trade.exit_time
            else None
        ),
        "entry_price": (
            format_decimal(trade.entry_price)
            if trade and trade.entry_price is not None
            else None
        ),
        "exit_price": (
            format_decimal(trade.exit_price)
            if trade and trade.exit_price is not None
            else None
        ),
        "stop_loss": None,
        "take_profits": [],
        "open_quantity": format_decimal(trade.quantity) if trade is not None else "0",
        "original_quantity": format_decimal(trade.quantity) if trade is not None else "0",
        "realized_pnl": format_decimal(trade_pnl),
        "unrealized_pnl": "0",
        "total_pnl": format_decimal(trade_pnl),
        "total_pnl_pct": format_decimal(trade_pnl_pct),
        "margin_pnl_pct": format_decimal(trade_pnl_pct),
        "declared_leverage": None,
        "effective_leverage": None,
        "leverage": None,
        "margin": "0",
        "balance_basis": format_decimal(initial_balance),
        "initial_balance": format_decimal(initial_balance),
        "final_balance": format_decimal(final_balance),
        "targets_hit": 0,
        "message_link": record.source_message_link,
        "source_message_id": record.source_message_id,
        "message_ids": list(dict.fromkeys(record.message_ids)),
        "related_message_count": len(set(record.message_ids)),
        "lifecycle": list(record.lifecycle_messages),
        "chart": {"timezone": "Asia/Tehran", "interval": None, "candles": []},
        "notes": list(trade.notes) if trade is not None else [],
        "checkpoints": [],
    }


def _build_backtest_chart_payload(signal_state: Any) -> dict[str, Any]:
    history = [
        point
        for point in (signal_state.price_history or [])
        if hasattr(point, "candle_open_time") and hasattr(point, "candle_close_time")
    ]
    chart_candles = _compact_backtest_chart_candles(history)
    return {
        "timezone": "Asia/Tehran",
        "interval": _backtest_chart_interval(chart_candles),
        "candles": chart_candles,
        "visible_points": len(chart_candles),
        "source_points": len(history),
        "sampled": len(chart_candles) < len(history),
        "stop_loss_history": RealBacktestRunner._level_history_payload(
            signal_state.stop_loss_history
        ),
        "take_profit_history": RealBacktestRunner._level_history_payload(
            signal_state.take_profit_history
        ),
    }


def _compact_backtest_chart_candles(history: list[Any]) -> list[dict[str, Any]]:
    if not history:
        return []
    if len(history) <= _BACKTEST_MAX_CHART_CANDLES:
        return [_signal_price_point_to_chart_candle(point) for point in history]

    bucket_size = max(
        1,
        (len(history) + _BACKTEST_MAX_CHART_CANDLES - 1) // _BACKTEST_MAX_CHART_CANDLES,
    )
    candles: list[dict[str, Any]] = []
    for index in range(0, len(history), bucket_size):
        bucket = history[index : index + bucket_size]
        first = bucket[0]
        last = bucket[-1]
        high = max(point.high for point in bucket)
        low = min(point.low for point in bucket)
        candles.append(
            {
                "timestamp": first.candle_open_time.isoformat(),
                "timestamp_ms": str(int(first.candle_open_time.timestamp() * 1000)),
                "timestamp_tehran": first.candle_open_time.astimezone(TEHRAN_TZ).isoformat(),
                "close_timestamp": last.candle_close_time.isoformat(),
                "close_timestamp_ms": str(int(last.candle_close_time.timestamp() * 1000)),
                "close_timestamp_tehran": last.candle_close_time.astimezone(TEHRAN_TZ).isoformat(),
                "open": decimal_to_plain_string(first.open),
                "high": decimal_to_plain_string(high),
                "low": decimal_to_plain_string(low),
                "close": decimal_to_plain_string(last.close),
                "mark_price": decimal_to_plain_string(last.mark_price),
                "stop_loss": (
                    decimal_to_plain_string(last.stop_loss)
                    if last.stop_loss is not None
                    else None
                ),
                "take_profits": [
                    decimal_to_plain_string(item) or "0" for item in last.take_profits
                ],
            }
        )
    return candles


def _signal_price_point_to_chart_candle(point: Any) -> dict[str, Any]:
    return {
        "timestamp": point.candle_open_time.isoformat(),
        "timestamp_ms": str(int(point.candle_open_time.timestamp() * 1000)),
        "timestamp_tehran": point.candle_open_time.astimezone(TEHRAN_TZ).isoformat(),
        "close_timestamp": point.candle_close_time.isoformat(),
        "close_timestamp_ms": str(int(point.candle_close_time.timestamp() * 1000)),
        "close_timestamp_tehran": point.candle_close_time.astimezone(TEHRAN_TZ).isoformat(),
        "open": decimal_to_plain_string(point.open),
        "high": decimal_to_plain_string(point.high),
        "low": decimal_to_plain_string(point.low),
        "close": decimal_to_plain_string(point.close),
        "mark_price": decimal_to_plain_string(point.mark_price),
        "stop_loss": (
            decimal_to_plain_string(point.stop_loss)
            if point.stop_loss is not None
            else None
        ),
        "take_profits": [
            decimal_to_plain_string(item) or "0" for item in point.take_profits
        ],
    }


def _backtest_chart_interval(candles: list[dict[str, Any]]) -> str:
    if len(candles) < 2:
        return "n/a"
    first = datetime.fromisoformat(str(candles[0]["timestamp"]))
    second = datetime.fromisoformat(str(candles[1]["timestamp"]))
    minutes = int((second - first).total_seconds() // 60)
    return f"{minutes}m" if minutes > 0 else "n/a"


def _backtest_period_pnl(signals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    closed = [item for item in signals if item.get("exit_time")]
    return {
        "daily": _bucket_signal_pnl(closed, "%Y-%m-%d"),
        "weekly": _bucket_signal_pnl(closed, "%G-W%V"),
        "monthly": _bucket_signal_pnl(closed, "%Y-%m"),
    }


def _bucket_signal_pnl(signals: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for item in signals:
        exit_time = datetime.fromisoformat(str(item["exit_time"]))
        key = exit_time.astimezone(timezone.utc).strftime(pattern)
        bucket = buckets.setdefault(
            key,
            {"period": key, "signals": 0, "wins": 0, "losses": 0, "pnl": Decimal("0")},
        )
        pnl = Decimal(str(item.get("total_pnl") or "0"))
        bucket["signals"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    return [
        {
            "period": item["period"],
            "signals": item["signals"],
            "wins": item["wins"],
            "losses": item["losses"],
            "pnl": decimal_to_plain_string(item["pnl"]) or "0",
        }
        for item in sorted(buckets.values(), key=lambda row: row["period"])
    ]


def _backtest_symbol_summary(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for signal in signals:
        symbol = str(signal.get("symbol") or "unknown")
        item = summary.setdefault(
            symbol,
            {
                "symbol": symbol,
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "not_filled": 0,
                "pnl": Decimal("0"),
            },
        )
        pnl = Decimal(str(signal.get("total_pnl") or "0"))
        item["signals"] += 1
        item["pnl"] += pnl
        if str(signal.get("status") or "") == "not_filled":
            item["not_filled"] += 1
        elif pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1
    return [
        {
            "symbol": item["symbol"],
            "signals": item["signals"],
            "wins": item["wins"],
            "losses": item["losses"],
            "not_filled": item["not_filled"],
            "pnl": decimal_to_plain_string(item["pnl"]) or "0",
        }
        for item in sorted(
            summary.values(),
            key=lambda row: (row["pnl"], row["signals"]),
            reverse=True,
        )
    ]


def _backtest_equity_curve(
    *,
    signals: list[dict[str, Any]],
    capital_per_signal: Decimal,
) -> list[dict[str, Any]]:
    equity = capital_per_signal * Decimal(len(signals))
    points: list[dict[str, Any]] = []
    ordered = sorted(
        signals,
        key=lambda item: str(item.get("exit_time") or item.get("entry_time") or ""),
    )
    for index, signal in enumerate(ordered, start=1):
        equity += Decimal(str(signal.get("total_pnl") or "0"))
        points.append(
            {
                "index": index,
                "signal_id": signal.get("signal_id"),
                "symbol": signal.get("symbol"),
                "status": signal.get("status"),
                "timestamp": signal.get("exit_time") or signal.get("entry_time"),
                "equity": decimal_to_plain_string(equity) or "0",
            }
        )
    return points


def _equity_max_drawdown(points: list[dict[str, Any]]) -> Decimal:
    peak: Decimal | None = None
    max_drawdown = Decimal("0")
    for point in points:
        equity = Decimal(str(point.get("equity") or "0"))
        if peak is None or equity > peak:
            peak = equity
        if peak is None or peak <= Decimal("0"):
            continue
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _status_counts(signals: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in signals:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items(), key=lambda row: (-row[1], row[0])))


def _best_or_worst_signals(
    signals: list[dict[str, Any]],
    *,
    reverse: bool,
) -> list[dict[str, Any]]:
    ranked = sorted(
        signals,
        key=lambda item: Decimal(str(item.get("total_pnl") or "0")),
        reverse=reverse,
    )[:5]
    return [
        {
            "signal_id": item.get("signal_id"),
            "symbol": item.get("symbol"),
            "status": item.get("status"),
            "total_pnl": item.get("total_pnl"),
            "total_pnl_pct": item.get("total_pnl_pct"),
        }
        for item in ranked
    ]


def _avg_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return Decimal(str(median(values)))


def _sort_signal_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (
            item.get("status_group") != "active",
            str(item.get("entry_time") or ""),
            str(item.get("signal_id") or ""),
        ),
    )
