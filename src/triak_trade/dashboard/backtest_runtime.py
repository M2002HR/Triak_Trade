"""Live dashboard backtest runtime state and orchestration."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from triak_trade.backtesting.real_runner import (
    RealBacktestMessageTrace,
    RealBacktestProgressEvent,
    RealBacktestRunner,
    RealBacktestRunRequest,
)
from triak_trade.config.settings import Settings


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
    signals: list[dict[str, Any]] = Field(default_factory=list)
    report_path: str | None = None
    markdown_report_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    messages: list[RealBacktestMessageTrace] = Field(default_factory=list)
    events: list[DashboardBacktestEvent] = Field(default_factory=list)


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
        payload = run.model_dump(mode="json")
        temporary = path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)

    def read(self, run_id: str) -> DashboardBacktestRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return DashboardBacktestRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 20) -> list[DashboardBacktestRun]:
        runs: list[DashboardBacktestRun] = []
        paths = sorted(
            self.root.glob("*.json"),
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

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"


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
            use_ai=request.use_ai,
            send_log_channel=request.send_log_channel,
            log_per_message=request.log_per_message,
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        self.store.create(run)
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
            use_ai=previous.use_ai,
            send_telegram_summary=False,
            send_log_channel=previous.send_log_channel,
            log_per_message=previous.log_per_message,
        )
        return self.start_run(request, channel_input=previous.channel_input)

    def get_run(self, run_id: str) -> DashboardBacktestRun | None:
        return self.store.read(run_id)

    def list_runs(self, limit: int = 20) -> list[DashboardBacktestRun]:
        return self.store.list_runs(limit=limit)

    def _execute_run(self, run_id: str, request: RealBacktestRunRequest) -> None:
        runner = self.runner_factory()
        run = self.store.read(run_id)
        if run is None:
            return
        if self._is_cancel_requested(run_id):
            self._mark_cancelled(run_id)
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.current_phase = "starting"
        run.current_phase_label = _phase_label("starting")
        run.current_phase_summary = "Backtest worker started."
        self.store.write(run)
        try:
            result = runner.run_sync(
                request,
                progress_callback=lambda event: self._handle_progress(run_id, event),
            )
        except DashboardBacktestCancelledError:
            self._mark_cancelled(run_id)
            return
        except Exception as exc:
            self._clear_cancel_request(run_id)
            failed = self.store.read(run_id)
            if failed is None:
                return
            failed.status = "failed"
            failed.finished_at = datetime.now(timezone.utc)
            failed.current_phase = "failed"
            failed.current_phase_label = _phase_label("failed")
            failed.current_phase_summary = f"Backtest worker crashed: {type(exc).__name__}"
            failed.errors.append(f"Background runner crashed: {type(exc).__name__}")
            failed.events.append(
                DashboardBacktestEvent(
                    at=datetime.now(timezone.utc),
                    phase="failed",
                    status="failed",
                    summary=failed.current_phase_summary,
                )
            )
            self.store.write(failed)
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
        for key in ("live_open_positions", "live_closed_trades", "live_wins", "live_losses"):
            if key in event.live_metrics:
                setattr(run, key, int(event.live_metrics[key]))
        for key in ("live_realized_pnl", "live_unrealized_pnl", "live_total_pnl"):
            if key in event.live_metrics:
                setattr(run, key, event.live_metrics[key])
        if event.live_signals:
            run.signals = event.live_signals
        run.events.append(
            DashboardBacktestEvent(
                at=event.timestamp,
                phase=event.phase,
                status=event.status,
                summary=event.summary,
                current_message_id=event.current_message_id,
            )
        )
        run.events = run.events[-250:]
        if event.trace is not None:
            self._merge_trace(run, event.trace)
        self.store.write(run)
        self._notify(run)

    def _merge_trace(self, run: DashboardBacktestRun, trace: RealBacktestMessageTrace) -> None:
        for index, existing in enumerate(run.messages):
            if existing.message_id == trace.message_id:
                run.messages[index] = trace
                break
        else:
            run.messages.append(trace)
        run.messages.sort(key=lambda item: item.message_date, reverse=True)

    def _notify(self, run: DashboardBacktestRun) -> None:
        if self.notifier is None:
            return
        self.notifier({"type": "backtest_run", "run": run.model_dump(mode="json")})

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
        self._notify(run)

    def _recover_incomplete_runs(self) -> None:
        now = datetime.now(timezone.utc)
        for run in self.store.list_runs(limit=200):
            if run.status not in {"queued", "running"}:
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
