from __future__ import annotations

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
    DashboardBacktestStore,
    normalize_channel_reference,
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


def test_normalize_channel_reference_accepts_usernames() -> None:
    assert normalize_channel_reference("Tofan_Trade") == "https://t.me/Tofan_Trade"
    assert normalize_channel_reference("@Tofan_Trade") == "https://t.me/Tofan_Trade"


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
    assert loaded.report_path == "runtime/reports/backtests/report.json"
