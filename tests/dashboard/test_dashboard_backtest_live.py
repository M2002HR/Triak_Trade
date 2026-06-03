from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from triak_trade.backtesting.real_runner import (
    RealBacktestMessageStage,
    RealBacktestMessageTrace,
    RealBacktestProgressEvent,
    RealBacktestResult,
    RealBacktestRunRequest,
)
from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app


class FakeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def readiness(self):
        class Readiness:
            def __init__(self) -> None:
                self.ready = True
                self.issues: list[str] = []

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
            message_id=501,
            channel_id=request.channel,
            channel_username="Tofan_Trade",
            message_link="https://t.me/Tofan_Trade/501",
            message_date=now,
            full_text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
            preview_text="BTCUSDT LONG Entry: 68000 - 68200",
            classification="new_signal",
            parsed_action="open",
            symbol="BTCUSDT",
            side="long",
            confidence="0.90",
            signal_id="sig_501",
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
                    summary="Reviewing message 501.",
                    current_message_id=501,
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
            candles_fetched=50,
            trades_simulated=1,
            trades_filled=1,
            wins=1,
            losses=0,
            win_rate=Decimal("1"),
            total_pnl=Decimal("15"),
            profit_factor=Decimal("1.5"),
            max_drawdown=Decimal("1"),
            conservative_pnl=Decimal("12"),
            optimistic_pnl=Decimal("18"),
            channel_score=Decimal("80"),
            warnings=[],
            errors=[],
            generated_at=now,
            report_path="runtime/reports/backtests/report.json",
            markdown_report_path="runtime/reports/backtests/report.md",
        )


def build_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr("triak_trade.dashboard.services.RealBacktestRunner", FakeRunner)
    settings = Settings(
        _env_file=None,
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "reports"),
    )
    return TestClient(create_dashboard_app(settings))


def _headers() -> dict[str, str]:
    return {"X-Triak-Admin-Token": "test-token"}


def test_backtest_page_renders_live_workspace(tmp_path: Path, monkeypatch) -> None:
    response = build_client(tmp_path, monkeypatch).get("/backtests", headers=_headers())
    assert response.status_code == 200
    assert "Live Telegram Backtest Monitor" in response.text
    assert "Start Backtest" in response.text
    assert "Per-Message Trace" in response.text


def test_backtest_start_api_runs_and_exposes_live_run(tmp_path: Path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    start = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={
            "channel": "@Tofan_Trade",
            "from_date": "2026-06-03T00:00:00Z",
            "to_date": "2026-06-04T00:00:00Z",
            "interval": "1m",
            "max_messages": 1000,
            "use_ai": False,
            "send_log_channel": True,
            "log_per_message": True,
        },
    )
    assert start.status_code == 202
    body = start.json()
    assert body["started"] is True
    run_id = body["run"]["run_id"]

    loaded = None
    for _ in range(50):
        response = client.get(f"/api/backtests/runs/{run_id}", headers=_headers())
        assert response.status_code == 200
        loaded = response.json()
        if loaded["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)

    assert loaded is not None
    assert loaded["status"] == "completed"
    assert loaded["channel_resolved"] == "https://t.me/Tofan_Trade"
    assert loaded["messages"][0]["message_id"] == 501
    assert loaded["messages"][0]["classification"] == "new_signal"
    assert loaded["report_path"] == "runtime/reports/backtests/report.json"


def test_backtest_start_api_rejects_missing_dates(tmp_path: Path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={"channel": "@Tofan_Trade"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "from_date and to_date are required"
