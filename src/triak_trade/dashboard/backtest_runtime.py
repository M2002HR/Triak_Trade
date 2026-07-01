"""Live dashboard backtest runtime state and orchestration."""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from triak_trade.backtesting.real_runner import (
    RealBacktestMessageTrace,
    RealBacktestProgressEvent,
    RealBacktestRunner,
    RealBacktestRunRequest,
)
from triak_trade.backtesting.strategies.registry import build_strategy_from_key
from triak_trade.config.settings import Settings
from triak_trade.dashboard.log_sink import append_dashboard_log

_log = logging.getLogger(__name__)


class DashboardBacktestEvent(BaseModel):
    at: datetime
    phase: str
    status: str
    summary: str
    current_message_id: int | None = None


class DashboardBacktestRun(BaseModel):
    run_id: str
    channel_input: str
    channel_resolved: str
    start_message_link: str | None = None
    start_message_id: int | None = None
    from_date: datetime
    to_date: datetime
    interval: str
    max_messages: int
    initial_balance: Decimal = Decimal("100")
    risk_per_trade_pct: Decimal = Decimal("3")
    strategy_key: str = "default_risk_managed"
    use_ai: bool
    send_log_channel: bool
    log_per_message: bool
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_phase: str = "queued"
    current_phase_label: str = "Queued"
    current_phase_summary: str = "Waiting to start."
    current_message_id: int | None = None
    total_messages: int = 0
    classified_messages: int = 0
    parsed_signals: int = 0
    valid_signals: int = 0
    invalid_signals: int = 0
    ignored_messages: int = 0
    ambiguous_messages: int = 0
    trades_simulated: int = 0
    trades_filled: int = 0
    live_open_positions: int = 0
    live_closed_trades: int = 0
    live_wins: int = 0
    live_losses: int = 0
    live_realized_pnl: str = "0"
    live_unrealized_pnl: str = "0"
    live_total_pnl: str = "0"
    live_realized_balance: str = "0"
    live_current_balance: str = "0"
    signals: list[dict[str, Any]] = Field(default_factory=list)
    report_path: str | None = None
    markdown_report_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    messages: list[RealBacktestMessageTrace] = Field(default_factory=list)
    events: list[DashboardBacktestEvent] = Field(default_factory=list)


class DashboardBacktestRunSummary(BaseModel):
    run_id: str
    channel_input: str
    channel_resolved: str
    start_message_link: str | None = None
    start_message_id: int | None = None
    from_date: datetime
    to_date: datetime
    interval: str
    max_messages: int
    initial_balance: Decimal = Decimal("100")
    risk_per_trade_pct: Decimal = Decimal("3")
    strategy_key: str = "default_risk_managed"
    use_ai: bool
    send_log_channel: bool
    log_per_message: bool
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_phase: str = "queued"
    current_phase_label: str = "Queued"
    current_phase_summary: str = "Waiting to start."
    current_message_id: int | None = None
    total_messages: int = 0
    classified_messages: int = 0
    parsed_signals: int = 0
    valid_signals: int = 0
    invalid_signals: int = 0
    ignored_messages: int = 0
    ambiguous_messages: int = 0
    trades_simulated: int = 0
    trades_filled: int = 0
    live_open_positions: int = 0
    live_closed_trades: int = 0
    live_wins: int = 0
    live_losses: int = 0
    live_realized_pnl: str = "0"
    live_unrealized_pnl: str = "0"
    live_total_pnl: str = "0"
    live_realized_balance: str = "0"
    live_current_balance: str = "0"
    report_path: str | None = None
    markdown_report_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_run(cls, run: DashboardBacktestRun) -> DashboardBacktestRunSummary:
        payload = run.model_dump(
            mode="python",
            exclude={"messages", "events", "signals"},
        )
        return cls.model_validate(payload)


class DashboardBacktestCancelledError(RuntimeError):
    """Raised inside the progress callback to stop a running backtest safely."""


class DashboardBacktestStore:
    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.DASHBOARD_RUNTIME_DIR) / "backtests"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, run: DashboardBacktestRun) -> DashboardBacktestRun:
        self.write(run)
        return run

    def write(self, run: DashboardBacktestRun) -> None:
        path = self._path(run.run_id)
        summary_path = self._summary_path(run.run_id)
        payload = run.model_dump(mode="json")
        summary_payload = DashboardBacktestRunSummary.from_run(run).model_dump(mode="json")
        temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
        summary_temporary = summary_path.with_name(
            f"{summary_path.stem}.{uuid.uuid4().hex}.tmp"
        )
        with self._lock:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            summary_temporary.write_text(
                json.dumps(summary_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
            summary_temporary.replace(summary_path)

    def read(self, run_id: str) -> DashboardBacktestRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return DashboardBacktestRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 20) -> list[DashboardBacktestRun]:
        runs: list[DashboardBacktestRun] = []
        paths = sorted(
            self._run_paths(),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in paths:
            runs.append(
                DashboardBacktestRun.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            )
            if len(runs) >= limit:
                break
        return runs

    def list_run_summaries(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DashboardBacktestRunSummary]:
        runs: list[DashboardBacktestRunSummary] = []
        paths = sorted(
            self._run_paths(),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in paths[offset:]:
            run_id = path.stem
            summary_path = self._summary_path(run_id)
            if summary_path.exists():
                runs.append(
                    DashboardBacktestRunSummary.model_validate_json(
                        summary_path.read_text(encoding="utf-8")
                    )
                )
            else:
                run = DashboardBacktestRun.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                summary = DashboardBacktestRunSummary.from_run(run)
                runs.append(summary)
                self.write(run)
            if len(runs) >= limit:
                break
        return runs

    def count_runs(self) -> int:
        return len(self._run_paths())

    def _run_paths(self) -> list[Path]:
        return [
            path
            for path in self.root.glob("*.json")
            if not path.name.endswith(".summary.json")
        ]

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def _summary_path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.summary.json"


def normalize_channel_reference(channel_input: str) -> str:
    normalized = channel_input.strip()
    if normalized.startswith("https://t.me/") or normalized.startswith("http://t.me/"):
        return normalized
    if normalized.startswith("@"):
        return f"https://t.me/{normalized[1:]}"
    return f"https://t.me/{normalized}"


_TELEGRAM_MESSAGE_LINK_PATTERN = re.compile(
    r"^https?://(?:t\.me|telegram\.me)/(?P<channel>[A-Za-z0-9_]{5,})/(?P<message_id>\d+)(?:[/?#].*)?$"
)


def parse_telegram_message_link(message_link: str) -> tuple[str, int]:
    normalized = message_link.strip()
    match = _TELEGRAM_MESSAGE_LINK_PATTERN.match(normalized)
    if not match:
        raise ValueError("start_message_link must be a public Telegram message link")
    channel_reference = normalize_channel_reference(match.group("channel"))
    message_id = int(match.group("message_id"))
    if message_id <= 0:
        raise ValueError("start_message_link must include a positive message id")
    return channel_reference, message_id


class DashboardBacktestCoordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: DashboardBacktestStore | None = None,
        runner_factory: Callable[[], RealBacktestRunner] | None = None,
        notifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or DashboardBacktestStore(settings)
        self.runner_factory = runner_factory or (lambda: RealBacktestRunner(settings=settings))
        self.notifier = notifier
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_requested: set[str] = set()
        self._lock = threading.Lock()
        self._recover_incomplete_runs()

    def readiness(self) -> dict[str, Any]:
        return self.runner_factory().readiness().model_dump(mode="json")

    def start_run(
        self,
        request: RealBacktestRunRequest,
        *,
        channel_input: str,
        strategy_key: str = "default_risk_managed",
    ) -> DashboardBacktestRun:
        from_date, to_date = request.resolve_range()
        run = DashboardBacktestRun(
            run_id=f"backtest_{uuid.uuid4().hex[:12]}",
            channel_input=channel_input,
            channel_resolved=request.channel,
            start_message_link=request.start_message_link,
            start_message_id=request.start_message_id,
            from_date=from_date,
            to_date=to_date,
            interval=request.interval,
            max_messages=request.max_messages,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            strategy_key=strategy_key,
            use_ai=request.use_ai,
            send_log_channel=request.send_log_channel,
            log_per_message=request.log_per_message,
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        self.store.create(run)
        self._append_run_log(
            "dashboard.backtest.queued",
            run,
            level="INFO",
            extra={
                "channel_input": channel_input,
                "strategy_key": strategy_key,
            },
        )
        thread = threading.Thread(
            target=self._execute_run,
            args=(run.run_id, request),
            name=f"dashboard-backtest-{run.run_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[run.run_id] = thread
        thread.start()
        self._notify(run)
        return run

    def stop_run(self, run_id: str) -> tuple[DashboardBacktestRun | None, bool, str]:
        run = self.store.read(run_id)
        if run is None:
            return None, False, "run_not_found"
        if run.status == "cancelled":
            return run, False, "run_already_cancelled"
        if run.status not in {"queued", "running", "cancelling"}:
            return run, False, f"run_not_stoppable_status_{run.status}"

        now = datetime.now(timezone.utc)
        with self._lock:
            self._cancel_requested.add(run_id)
        run.status = "cancelling"
        run.current_phase = "cancelling"
        run.current_phase_label = _phase_label("cancelling")
        run.current_phase_summary = (
            "Stop requested. The worker will stop at the next safe checkpoint."
        )
        run.events.append(
            DashboardBacktestEvent(
                at=now,
                phase="cancelling",
                status="running",
                summary=run.current_phase_summary,
                current_message_id=run.current_message_id,
            )
        )
        self.store.write(run)
        self._append_run_log(
            "dashboard.backtest.cancellation_requested",
            run,
            level="WARNING",
        )
        self._notify(run)
        return run, True, "stop_requested"

    def rerun_run(self, run_id: str) -> DashboardBacktestRun | None:
        previous = self.store.read(run_id)
        if previous is None:
            return None
        request = RealBacktestRunRequest(
            channel=previous.channel_resolved,
            from_date=previous.from_date,
            to_date=previous.to_date,
            hours=None,
            start_message_link=previous.start_message_link,
            start_message_id=previous.start_message_id,
            interval=previous.interval,
            max_messages=previous.max_messages,
            initial_balance=previous.initial_balance,
            risk_per_trade_pct=previous.risk_per_trade_pct,
            use_ai=previous.use_ai,
            send_telegram_summary=False,
            send_log_channel=previous.send_log_channel,
            log_per_message=previous.log_per_message,
        )
        return self.start_run(
            request,
            channel_input=previous.channel_input,
            strategy_key=previous.strategy_key,
        )

    def get_run(self, run_id: str) -> DashboardBacktestRun | None:
        return self.store.read(run_id)

    def list_runs(self, limit: int = 20) -> list[DashboardBacktestRun]:
        return self.store.list_runs(limit=limit)

    def list_run_summaries(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DashboardBacktestRunSummary]:
        return self.store.list_run_summaries(limit=limit, offset=offset)

    def count_runs(self) -> int:
        return self.store.count_runs()

    def _execute_run(self, run_id: str, request: RealBacktestRunRequest) -> None:
        run = self.store.read(run_id)
        if run is None:
            return
        runner = self.runner_factory()
        runner.strategy = build_strategy_from_key(run.strategy_key)
        if self._is_cancel_requested(run_id):
            self._mark_cancelled(run_id)
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.current_phase = "starting"
        run.current_phase_label = _phase_label("starting")
        run.current_phase_summary = "Backtest worker started."
        self.store.write(run)
        self._append_run_log("dashboard.backtest.started", run, level="INFO")
        try:
            result = runner.run_sync(
                request,
                progress_callback=lambda event: self._handle_progress(run_id, event),
            )
        except DashboardBacktestCancelledError:
            self._mark_cancelled(run_id)
            return
        except Exception as exc:
            tb = traceback.format_exc()
            _log.error(
                "Backtest worker crashed for run %s: %s\n%s",
                run_id,
                exc,
                tb,
            )
            self._clear_cancel_request(run_id)
            failed = self.store.read(run_id)
            if failed is None:
                return
            failed.status = "failed"
            failed.finished_at = datetime.now(timezone.utc)
            failed.current_phase = "failed"
            failed.current_phase_label = _phase_label("failed")
            exc_summary = f"{type(exc).__name__}: {exc}"
            failed.current_phase_summary = f"Backtest worker crashed: {type(exc).__name__}"
            failed.errors.append(f"Background runner crashed: {exc_summary}")
            # Store the full traceback so it is visible in the dashboard and
            # in log output — without this only the exception type was retained
            # which made diagnosing failures impossible.
            for line in tb.splitlines():
                if line.strip():
                    failed.errors.append(line)
            failed.events.append(
                DashboardBacktestEvent(
                    at=datetime.now(timezone.utc),
                    phase="failed",
                    status="failed",
                    summary=failed.current_phase_summary,
                )
            )
            self.store.write(failed)
            self._append_run_log(
                "dashboard.backtest.worker_crashed",
                failed,
                level="ERROR",
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback_tail": tb.splitlines()[-8:],
                },
            )
            self._notify(failed)
            return

        completed = self.store.read(run_id)
        if completed is None:
            return
        self._clear_cancel_request(run_id)
        completed.status = "completed" if result.success else "failed"
        completed.finished_at = datetime.now(timezone.utc)
        completed.current_phase = "complete" if result.success else "failed"
        completed.current_phase_label = _phase_label(completed.current_phase)
        completed.current_phase_summary = (
            "Backtest finished successfully."
            if result.success
            else "Backtest finished with a precise failure report."
        )
        completed.report_path = result.report_path
        completed.markdown_report_path = result.markdown_report_path
        completed.errors = list(result.errors)
        completed.warnings = list(result.warnings)
        completed.trades_simulated = result.trades_simulated
        completed.trades_filled = result.trades_filled
        completed.live_open_positions = 0
        completed.live_closed_trades = result.trades_filled
        completed.live_wins = result.wins
        completed.live_losses = result.losses
        completed.live_realized_pnl = str(result.total_pnl)
        completed.live_unrealized_pnl = "0"
        completed.live_total_pnl = str(result.total_pnl)
        completed.events.append(
            DashboardBacktestEvent(
                at=datetime.now(timezone.utc),
                phase=completed.current_phase,
                status=completed.status,
                summary=completed.current_phase_summary,
            )
        )
        self.store.write(completed)
        self._append_run_log(
            "dashboard.backtest.completed" if result.success else "dashboard.backtest.failed",
            completed,
            level="INFO" if result.success else "ERROR",
            extra={
                "success": result.success,
                "report_path": result.report_path,
                "markdown_report_path": result.markdown_report_path,
                "warnings": result.warnings[:5],
                "errors": result.errors[:5],
            },
        )
        self._notify(completed)

    def _handle_progress(self, run_id: str, event: RealBacktestProgressEvent) -> None:
        if self._is_cancel_requested(run_id):
            raise DashboardBacktestCancelledError()
        run = self.store.read(run_id)
        if run is None:
            return
        run.current_phase = event.phase
        run.current_phase_label = _phase_label(event.phase)
        run.current_phase_summary = event.summary
        run.current_message_id = event.current_message_id
        for key, value in event.counts.items():
            if hasattr(run, key):
                setattr(run, key, value)
        if event.live_metrics is not None:
            for key in ("live_open_positions", "live_closed_trades", "live_wins", "live_losses"):
                if key in event.live_metrics:
                    setattr(run, key, int(event.live_metrics[key]))
            for key in (
                "live_realized_pnl",
                "live_unrealized_pnl",
                "live_total_pnl",
                "live_realized_balance",
                "live_current_balance",
            ):
                if key in event.live_metrics:
                    setattr(run, key, event.live_metrics[key])
        if event.live_signals is not None:
            run.signals = self._merge_signal_history(run.signals, event.live_signals)
            self._refresh_signal_aggregate_metrics(run)
        run.events.append(
            DashboardBacktestEvent(
                at=event.timestamp,
                phase=event.phase,
                status=event.status,
                summary=event.summary,
                current_message_id=event.current_message_id,
            )
        )
        run.events = run.events[-120:]
        if event.trace is not None:
            self._merge_trace(run, event.trace)
        self.store.write(run)
        self._append_run_progress_log(run, event)
        self._notify(run)

    _MAX_STORED_TRACES = 500

    def _merge_trace(self, run: DashboardBacktestRun, trace: RealBacktestMessageTrace) -> None:
        for index, existing in enumerate(run.messages):
            if existing.message_id == trace.message_id:
                run.messages[index] = trace
                break
        else:
            run.messages.append(trace)
        run.messages.sort(key=lambda item: item.message_date, reverse=True)
        if len(run.messages) > self._MAX_STORED_TRACES:
            run.messages = run.messages[: self._MAX_STORED_TRACES]

    @staticmethod
    def _merge_signal_history(
        existing_signals: list[dict[str, Any]],
        incoming_signals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for signal in existing_signals:
            signal_id = str(signal.get("signal_id") or "")
            if signal_id:
                merged[signal_id] = dict(signal)
        for signal in incoming_signals:
            signal_id = str(signal.get("signal_id") or "")
            if not signal_id:
                continue
            base = merged.get(signal_id, {})
            base.update(signal)
            merged[signal_id] = base
        return sorted(
            merged.values(),
            key=lambda item: (
                item.get("status_group") != "active",
                str(item.get("entry_time") or ""),
                str(item.get("signal_id") or ""),
            ),
        )

    @staticmethod
    def _refresh_signal_aggregate_metrics(run: DashboardBacktestRun) -> None:
        realized_pnl = Decimal("0")
        unrealized_pnl = Decimal("0")
        wins = 0
        losses = 0
        filled = 0
        active = 0
        closed = 0

        for signal in run.signals:
            status = str(signal.get("status") or "")
            status_group = str(signal.get("status_group") or "")
            if status != "not_filled":
                filled += 1
            if status_group == "active":
                active += 1
            elif status != "not_filled":
                closed += 1

            signal_realized = Decimal(str(signal.get("realized_pnl") or "0"))
            signal_unrealized = Decimal(str(signal.get("unrealized_pnl") or "0"))
            signal_total = Decimal(str(signal.get("total_pnl") or "0"))
            realized_pnl += signal_realized
            unrealized_pnl += signal_unrealized

            if status_group != "active" and status != "not_filled":
                if signal_total > Decimal("0"):
                    wins += 1
                elif signal_total < Decimal("0"):
                    losses += 1

        total_pnl = realized_pnl + unrealized_pnl
        run.trades_simulated = len(run.signals)
        run.trades_filled = filled
        run.live_open_positions = active
        run.live_closed_trades = closed
        run.live_wins = wins
        run.live_losses = losses
        run.live_realized_pnl = str(realized_pnl)
        run.live_unrealized_pnl = str(unrealized_pnl)
        run.live_total_pnl = str(total_pnl)
        initial_balance = Decimal(run.initial_balance)
        run.live_realized_balance = str(initial_balance + realized_pnl)
        run.live_current_balance = str(initial_balance + total_pnl)

    def _notify(self, run: DashboardBacktestRun) -> None:
        if self.notifier is None:
            return
        # Exclude the per-message trace list from real-time WebSocket pushes.
        # Clients that need trace detail can request it via the dedicated
        # /api/backtests/<run_id>/messages endpoint. This prevents serializing
        # and pushing hundreds of full-text trace objects on every progress tick.
        payload = run.model_dump(mode="json", exclude={"messages"})
        self.notifier({"type": "backtest_run", "run": payload})

    def _is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancel_requested

    def _clear_cancel_request(self, run_id: str) -> None:
        with self._lock:
            self._cancel_requested.discard(run_id)

    def _mark_cancelled(self, run_id: str) -> None:
        now = datetime.now(timezone.utc)
        self._clear_cancel_request(run_id)
        run = self.store.read(run_id)
        if run is None:
            return
        run.status = "cancelled"
        run.finished_at = now
        run.current_phase = "cancelled"
        run.current_phase_label = _phase_label("cancelled")
        run.current_phase_summary = "Backtest run was stopped by the dashboard operator."
        run.events.append(
            DashboardBacktestEvent(
                at=now,
                phase="cancelled",
                status="cancelled",
                summary=run.current_phase_summary,
                current_message_id=run.current_message_id,
            )
        )
        self.store.write(run)
        self._append_run_log("dashboard.backtest.cancelled", run, level="WARNING")
        self._notify(run)

    def _recover_incomplete_runs(self) -> None:
        now = datetime.now(timezone.utc)
        for summary in self.store.list_run_summaries(limit=200):
            if summary.status not in {"queued", "running"}:
                continue
            run = self.store.read(summary.run_id)
            if run is None:
                continue
            run.status = "failed"
            run.finished_at = now
            run.current_phase = "failed"
            run.current_phase_label = _phase_label("failed")
            run.current_phase_summary = (
                "Backtest worker was interrupted before completion. Start a new run."
            )
            run.errors.append("Background backtest worker was interrupted.")
            run.events.append(
                DashboardBacktestEvent(
                    at=now,
                    phase="failed",
                    status="failed",
                    summary=run.current_phase_summary,
                    current_message_id=run.current_message_id,
                )
            )
            self.store.write(run)
            self._append_run_log(
                "dashboard.backtest.recovered_incomplete_run",
                run,
                level="ERROR",
            )

    def _append_run_progress_log(
        self,
        run: DashboardBacktestRun,
        event: RealBacktestProgressEvent,
    ) -> None:
        extra: dict[str, Any] = {
            "event_type": event.event_type,
            "event_status": event.status,
            "counts": event.counts,
        }
        if event.trace is not None:
            extra["trace"] = {
                "message_id": event.trace.message_id,
                "classification": event.trace.classification,
                "parsed_action": event.trace.parsed_action,
                "symbol": event.trace.symbol,
                "final_status": event.trace.final_status,
                "preview_text": event.trace.preview_text,
            }
        self._append_run_log(
            "dashboard.backtest.progress",
            run,
            level="INFO",
            extra=extra,
            summary_override=event.summary,
            phase_override=event.phase,
            current_message_id=event.current_message_id,
        )

    def _append_run_log(
        self,
        event: str,
        run: DashboardBacktestRun,
        *,
        level: str,
        extra: dict[str, Any] | None = None,
        summary_override: str | None = None,
        phase_override: str | None = None,
        current_message_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "channel": run.channel_resolved,
            "status": run.status,
            "phase": phase_override or run.current_phase,
            "summary": summary_override or run.current_phase_summary,
            "current_message_id": (
                current_message_id
                if current_message_id is not None
                else run.current_message_id
            ),
            "total_messages": run.total_messages,
            "classified_messages": run.classified_messages,
            "valid_signals": run.valid_signals,
            "trades_simulated": run.trades_simulated,
            "trades_filled": run.trades_filled,
        }
        if extra:
            payload.update(extra)
        append_dashboard_log(
            self.settings,
            event,
            payload,
            level=level,
            module="backtest",
        )


def _phase_label(phase: str) -> str:
    mapping = {
        "queued": "Queued",
        "starting": "Starting",
        "fetch_history": "Fetching Telegram History",
        "classify_messages": "Classifying Messages",
        "fetch_market_data": "Fetching Market Data",
        "simulate": "Simulating Trades",
        "report": "Writing Report",
        "cancelling": "Stopping",
        "cancelled": "Stopped",
        "complete": "Completed",
        "failed": "Failed",
    }
    return mapping.get(phase, phase.replace("_", " ").title())
