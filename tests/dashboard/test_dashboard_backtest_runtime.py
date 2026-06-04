from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from triak_trade.backtesting.real_runner import (
    RealBacktestMessageStage,
    RealBacktestMessageTrace,
    RealBacktestProgressEvent,
    RealBacktestResult,
    RealBacktestRunRequest,
)
from triak_trade.config.settings import Settings
from triak_trade.dashboard.backtest_runtime import (
    DashboardBacktestCoordinator,
    DashboardBacktestRun,
    DashboardBacktestStore,
    normalize_channel_reference,
    parse_telegram_message_link,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "reports"),
    )


class FakeRunner:
    def readiness(self):
        class Readiness:
            def model_dump(self, mode: str = "json") -> dict[str, object]:
                return {
                    "ready": True,
                    "issues": [],
                    "real_backtest_enabled": True,
                    "telegram_credentials_present": True,
                    "telegram_session_configured": True,
                    "toobit_public_market_ready": True,
                    "ai_gateway_enabled": False,
                    "regex_fallback_enabled": True,
                    "report_dir": "runtime/reports/backtests",
                    "log_channel_enabled": True,
                }

        return Readiness()

    def run_sync(
        self,
        request: RealBacktestRunRequest,
        progress_callback=None,
    ) -> RealBacktestResult:
        now = datetime(2026, 6, 4, tzinfo=timezone.utc)
        trace = RealBacktestMessageTrace(
            message_id=77,
            channel_id=request.channel,
            channel_username="Tofan_Trade",
            message_link="https://t.me/Tofan_Trade/77",
            message_date=now,
            full_text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
            preview_text="BTCUSDT LONG Entry: 68000 - 68200",
            classification="new_signal",
            parsed_action="open",
            symbol="BTCUSDT",
            side="long",
            confidence="0.90",
            signal_id="sig_77",
            final_status="awaiting_market_data",
            result_summary="Signal is structurally valid for backtesting.",
            current_stage="validated",
            last_updated_at=now,
            debug_notes=["classification=new_signal"],
            stages=[
                RealBacktestMessageStage(
                    key="received",
                    label="Message Received",
                    status="completed",
                    detail="Message pulled from Telegram history.",
                    started_at=now,
                    finished_at=now,
                ),
                RealBacktestMessageStage(
                    key="classified",
                    label="Classification",
                    status="completed",
                    detail="classification=new_signal",
                    started_at=now,
                    finished_at=now,
                ),
                RealBacktestMessageStage(
                    key="validated",
                    label="Signal Validation",
                    status="active",
                    detail="Checking signal structure.",
                    started_at=now,
                ),
            ],
        )
        if progress_callback is not None:
            progress_callback(
                RealBacktestProgressEvent(
                    event_type="run",
                    timestamp=now,
                    phase="fetch_history",
                    status="completed",
                    summary="Fetched 1 Telegram messages.",
                    counts={"total_messages": 1},
                )
            )
            progress_callback(
                RealBacktestProgressEvent(
                    event_type="message",
                    timestamp=now,
                    phase="classify_messages",
                    status="running",
                    summary="Reviewing message 77.",
                    current_message_id=77,
                    counts={
                        "total_messages": 1,
                        "classified_messages": 1,
                        "parsed_signals": 1,
                        "valid_signals": 1,
                    },
                    live_metrics={
                        "live_open_positions": "1",
                        "live_closed_trades": "0",
                        "live_wins": "0",
                        "live_losses": "0",
                        "live_realized_pnl": "0",
                        "live_unrealized_pnl": "2.5",
                        "live_total_pnl": "2.5",
                        "live_realized_balance": "100",
                        "live_current_balance": "102.5",
                    },
                    live_signals=[
                        {
                            "signal_id": "sig_77",
                            "symbol": "BTCUSDT",
                            "side": "long",
                            "status": "open",
                            "status_group": "active",
                            "entry_time": now.isoformat(),
                            "entry_time_tehran": "2026-06-04T03:30:00+03:30",
                            "exit_time": None,
                            "exit_time_tehran": None,
                            "entry_price": "68010",
                            "stop_loss": "67400",
                            "take_profits": ["69000", "70000"],
                            "open_quantity": "1",
                            "mark_price": "68100",
                            "realized_pnl": "0",
                            "unrealized_pnl": "2.5",
                            "total_pnl": "2.5",
                            "targets_hit": 0,
                            "lifecycle": ["created"],
                        }
                    ],
                    trace=trace,
                )
            )

        return RealBacktestResult(
            success=True,
            channel=request.channel,
            from_date=request.from_date or now,
            to_date=request.to_date or now,
            interval=request.interval,
            real_telegram_used=True,
            real_market_data_used=True,
            ai_used=False,
            regex_fallback_used=True,
            total_messages=1,
            classified_messages=1,
            parsed_signals=1,
            valid_signals=1,
            invalid_signals=0,
            ignored_messages=0,
            ambiguous_messages=0,
            symbols_found=["BTCUSDT"],
            candles_fetched=10,
            trades_simulated=1,
            trades_filled=1,
            wins=1,
            losses=0,
            win_rate=Decimal("1"),
            total_pnl=Decimal("25"),
            profit_factor=Decimal("2"),
            max_drawdown=Decimal("1"),
            conservative_pnl=Decimal("20"),
            optimistic_pnl=Decimal("30"),
            channel_score=Decimal("75"),
            warnings=[],
            errors=[],
            generated_at=now,
            report_path="runtime/reports/backtests/report.json",
            markdown_report_path="runtime/reports/backtests/report.md",
        )


class CancellableRunner(FakeRunner):
    entered = threading.Event()
    release = threading.Event()

    def run_sync(
        self,
        request: RealBacktestRunRequest,
        progress_callback=None,
    ) -> RealBacktestResult:
        now = datetime(2026, 6, 4, tzinfo=timezone.utc)
        if progress_callback is not None:
            progress_callback(
                RealBacktestProgressEvent(
                    event_type="run",
                    timestamp=now,
                    phase="fetch_history",
                    status="running",
                    summary="Fetching Telegram history.",
                    counts={"total_messages": 0},
                )
            )
        self.entered.set()
        self.release.wait(timeout=2)
        if progress_callback is not None:
            progress_callback(
                RealBacktestProgressEvent(
                    event_type="run",
                    timestamp=now,
                    phase="classify_messages",
                    status="running",
                    summary="This checkpoint should observe cancellation.",
                )
            )
        return super().run_sync(request, progress_callback)


def test_normalize_channel_reference_accepts_usernames() -> None:
    assert normalize_channel_reference("Tofan_Trade") == "https://t.me/Tofan_Trade"
    assert normalize_channel_reference("@Tofan_Trade") == "https://t.me/Tofan_Trade"


def test_parse_telegram_message_link_extracts_channel_and_message_id() -> None:
    channel, message_id = parse_telegram_message_link("https://t.me/Tofan_Trade/5880")
    assert channel == "https://t.me/Tofan_Trade"
    assert message_id == 5880


def test_parse_telegram_message_link_rejects_non_message_urls() -> None:
    try:
        parse_telegram_message_link("https://t.me/Tofan_Trade")
    except ValueError as exc:
        assert "public Telegram message link" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for invalid message link")


def test_dashboard_backtest_store_round_trip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = DashboardBacktestStore(settings)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        store=store,
        runner_factory=FakeRunner,
    )
    run = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="@Tofan_Trade",
    )
    assert store.read(run.run_id) is not None


def test_dashboard_backtest_coordinator_persists_live_progress(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        runner_factory=FakeRunner,
    )
    run = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="@Tofan_Trade",
    )

    for _ in range(50):
        loaded = coordinator.get_run(run.run_id)
        if loaded is not None and loaded.status in {"completed", "failed"}:
            break
        time.sleep(0.02)

    loaded = coordinator.get_run(run.run_id)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.total_messages == 1
    assert loaded.valid_signals == 1
    assert loaded.current_phase in {"complete", "classify_messages", "report"}
    assert len(loaded.messages) == 1
    assert loaded.messages[0].message_id == 77
    assert loaded.messages[0].classification == "new_signal"
    assert loaded.live_total_pnl == "25"
    assert loaded.live_realized_balance == "100"
    assert loaded.live_current_balance == "102.5"
    assert loaded.signals
    assert loaded.signals[0]["signal_id"] == "sig_77"
    assert loaded.signals[0]["symbol"] == "BTCUSDT"
    assert loaded.signals[0]["status_group"] == "active"
    assert loaded.report_path == "runtime/reports/backtests/report.json"


def test_dashboard_backtest_coordinator_notifies_on_updates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    notifications: list[dict[str, object]] = []
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        runner_factory=FakeRunner,
        notifier=notifications.append,
    )
    run = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            risk_per_trade_pct=Decimal("3"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="@Tofan_Trade",
    )

    for _ in range(50):
        loaded = coordinator.get_run(run.run_id)
        if loaded is not None and loaded.status in {"completed", "failed"}:
            break
        time.sleep(0.02)

    assert any(item.get("type") == "backtest_run" for item in notifications)
    assert any(item["run"]["run_id"] == run.run_id for item in notifications)  # type: ignore[index]


def test_dashboard_backtest_coordinator_preserves_signals_on_run_events_without_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = DashboardBacktestStore(settings)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        store=store,
        runner_factory=FakeRunner,
    )
    run = store.create(
        DashboardBacktestRun(
            run_id="run_preserve_signals",
            channel_input="https://t.me/Tofan_Trade",
            channel_resolved="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 5, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            risk_per_trade_pct=Decimal("3"),
            use_ai=False,
            send_log_channel=False,
            log_per_message=False,
            status="running",
            created_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        )
    )
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    trace = RealBacktestMessageTrace(
        message_id=77,
        channel_id=run.channel_resolved,
        channel_username="Tofan_Trade",
        message_link="https://t.me/Tofan_Trade/77",
        message_date=now,
        full_text="BTCUSDT LONG",
        preview_text="BTCUSDT LONG",
        classification="new_signal",
        parsed_action="open",
        symbol="BTCUSDT",
        side="long",
        confidence="0.90",
        signal_id="sig_77",
        final_status="simulation_tracking",
        result_summary="Signal is being simulated.",
        current_stage="simulated",
        last_updated_at=now,
        stages=[
            RealBacktestMessageStage(
                key="received",
                label="Message Received",
                status="completed",
                detail="Message pulled from Telegram history.",
                started_at=now,
                finished_at=now,
            )
        ],
    )
    coordinator._handle_progress(
        run.run_id,
        RealBacktestProgressEvent(
            event_type="message",
            timestamp=now,
            phase="simulate",
            status="running",
            summary="Live simulation state updated for message 77.",
            current_message_id=77,
            counts={"total_messages": 1},
            live_metrics={"live_open_positions": "1"},
            live_signals=[
                {
                    "signal_id": "sig_77",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "status": "open",
                    "status_group": "active",
                    "entry_time": now.isoformat(),
                    "entry_time_tehran": "2026-06-04T03:30:00+03:30",
                    "exit_time": None,
                    "exit_time_tehran": None,
                    "entry_price": "68010",
                    "stop_loss": "67400",
                    "take_profits": ["69000", "70000"],
                    "open_quantity": "1",
                    "mark_price": "68100",
                    "realized_pnl": "0",
                    "unrealized_pnl": "2.5",
                    "total_pnl": "2.5",
                    "targets_hit": 0,
                    "lifecycle": ["created"],
                }
            ],
            trace=trace,
        ),
    )
    coordinator._handle_progress(
        run.run_id,
        RealBacktestProgressEvent(
            event_type="run",
            timestamp=now,
            phase="fetch_market_data",
            status="running",
            summary="Fetching market candles for 1 symbols.",
            counts={"total_messages": 1},
        ),
    )

    loaded = store.read(run.run_id)
    assert loaded is not None
    assert loaded.signals
    assert loaded.signals[0]["signal_id"] == "sig_77"


def test_dashboard_backtest_coordinator_recovers_incomplete_runs_on_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = DashboardBacktestStore(settings)
    orphan = DashboardBacktestRun(
        run_id="backtest_orphaned",
        channel_input="@Tofan_Trade",
        channel_resolved="https://t.me/Tofan_Trade",
        from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
        interval="1m",
        max_messages=100,
        use_ai=False,
        send_log_channel=True,
        log_per_message=True,
        status="running",
        created_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        current_phase="classify_messages",
        current_phase_label="Classifying Messages",
        current_phase_summary="Reviewing message 5855.",
    )
    store.write(orphan)

    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        store=store,
        runner_factory=FakeRunner,
    )
    recovered = coordinator.get_run("backtest_orphaned")

    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.current_phase == "failed"
    assert "interrupted" in recovered.current_phase_summary.lower()
    assert any("interrupted" in error.lower() for error in recovered.errors)


def test_dashboard_backtest_coordinator_persists_start_message_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        runner_factory=FakeRunner,
    )
    run = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            start_message_link="https://t.me/Tofan_Trade/5880",
            start_message_id=5880,
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="https://t.me/Tofan_Trade/5880",
    )

    for _ in range(50):
        loaded = coordinator.get_run(run.run_id)
        if loaded is not None and loaded.status in {"completed", "failed"}:
            break
        time.sleep(0.02)

    loaded = coordinator.get_run(run.run_id)
    assert loaded is not None
    assert loaded.start_message_link == "https://t.me/Tofan_Trade/5880"
    assert loaded.start_message_id == 5880


def test_dashboard_backtest_coordinator_stops_running_run(tmp_path: Path) -> None:
    CancellableRunner.entered.clear()
    CancellableRunner.release.clear()
    settings = _settings(tmp_path)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        runner_factory=CancellableRunner,
    )
    run = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="@Tofan_Trade",
    )

    assert CancellableRunner.entered.wait(timeout=2)
    stopped_run, stopped, reason = coordinator.stop_run(run.run_id)
    assert stopped_run is not None
    assert stopped is True
    assert reason == "stop_requested"
    assert stopped_run.status == "cancelling"

    CancellableRunner.release.set()
    loaded = None
    for _ in range(50):
        loaded = coordinator.get_run(run.run_id)
        if loaded is not None and loaded.status == "cancelled":
            break
        time.sleep(0.02)

    assert loaded is not None
    assert loaded.status == "cancelled"
    assert loaded.current_phase == "cancelled"


def test_dashboard_backtest_coordinator_reruns_saved_parameters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    coordinator = DashboardBacktestCoordinator(
        settings=settings,
        runner_factory=FakeRunner,
    )
    original = coordinator.start_run(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            start_message_link="https://t.me/Tofan_Trade/5880",
            start_message_id=5880,
            interval="1m",
            max_messages=25,
            use_ai=True,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        ),
        channel_input="https://t.me/Tofan_Trade/5880",
    )
    rerun = coordinator.rerun_run(original.run_id)

    assert rerun is not None
    assert rerun.run_id != original.run_id
    assert rerun.channel_input == original.channel_input
    assert rerun.start_message_link == "https://t.me/Tofan_Trade/5880"
    assert rerun.start_message_id == 5880
    assert rerun.max_messages == 25
    assert rerun.use_ai is True
