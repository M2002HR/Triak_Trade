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
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.local_client import LocalASGIClient


class FakeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.strategy = None

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
                    live_signals=[
                        {
                            "signal_id": "sig_501",
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


def build_client(tmp_path: Path, monkeypatch) -> LocalASGIClient:
    monkeypatch.setattr("triak_trade.dashboard.services.RealBacktestRunner", FakeRunner)
    settings = Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard.db'}",
        TEST_DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard_test.db'}",
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        LIVE_TRADING_RUNTIME_DIR=str(tmp_path / "live_trading"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "reports"),
    )
    return LocalASGIClient(create_dashboard_app(settings))


def _headers() -> dict[str, str]:
    return {"X-Triak-Admin-Token": "test-token"}


def test_backtest_page_renders_live_workspace(tmp_path: Path, monkeypatch) -> None:
    response = build_client(tmp_path, monkeypatch).get("/backtests", headers=_headers())
    assert response.status_code == 200
    assert "Live Telegram Backtest Monitor" in response.text
    assert "Start Backtest" in response.text
    assert "Start From Message Link" in response.text
    assert "Per-Message Trace" in response.text
    assert "Open Run Feed" in response.text
    assert "Open History" in response.text
    assert "Active & Inactive" in response.text
    assert 'id="signal-state-preview"' in response.text
    assert 'data-open-panel-modal="signals"' in response.text
    assert 'id="run-action-bar"' in response.text
    assert 'data-message-filter="signals"' in response.text
    assert 'id="message-modal" class="modal-shell" hidden' in response.text
    assert 'id="panel-modal" class="modal-shell" hidden' in response.text


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
            "strategy_key": "tp_trailing_risk_managed",
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
    assert loaded["strategy_key"] == "tp_trailing_risk_managed"
    assert loaded["messages"][0]["message_id"] == 501
    assert loaded["messages"][0]["classification"] == "new_signal"
    assert loaded["signals"][0]["signal_id"] == "sig_501"
    assert loaded["signals"][0]["status_group"] == "active"
    assert loaded["report_path"] == "runtime/reports/backtests/report.json"


def test_backtest_start_api_defaults_log_per_message_to_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        },
    )
    assert start.status_code == 202
    body = start.json()
    assert body["run"]["log_per_message"] is True


def test_backtest_start_api_accepts_start_message_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = build_client(tmp_path, monkeypatch)
    start = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={
            "channel": "@Tofan_Trade",
            "from_date": "2026-06-03T00:00:00Z",
            "to_date": "2026-06-04T00:00:00Z",
            "start_message_link": "https://t.me/Tofan_Trade/5880",
            "interval": "1m",
            "max_messages": 1000,
            "use_ai": False,
            "send_log_channel": True,
            "log_per_message": True,
        },
    )
    assert start.status_code == 202
    body = start.json()
    assert body["run"]["start_message_id"] == 5880
    assert body["run"]["start_message_link"] == "https://t.me/Tofan_Trade/5880"


def test_backtest_start_api_derives_channel_from_start_message_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = build_client(tmp_path, monkeypatch)
    start = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={
            "channel": "",
            "from_date": "2026-06-03T00:00:00Z",
            "to_date": "2026-06-04T00:00:00Z",
            "start_message_link": "https://t.me/Tofan_Trade/5880",
            "interval": "1m",
            "max_messages": 1000,
            "use_ai": False,
            "send_log_channel": True,
            "log_per_message": True,
        },
    )
    assert start.status_code == 202
    body = start.json()
    assert body["run"]["channel_resolved"] == "https://t.me/Tofan_Trade"
    assert body["run"]["start_message_id"] == 5880


def test_backtest_start_api_rejects_cross_channel_start_message_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={
            "channel": "@Tofan_Trade",
            "from_date": "2026-06-03T00:00:00Z",
            "to_date": "2026-06-04T00:00:00Z",
            "start_message_link": "https://t.me/Another_Channel/5880",
            "interval": "1m",
            "max_messages": 1000,
            "use_ai": False,
            "send_log_channel": True,
            "log_per_message": True,
        },
    )
    assert response.status_code == 400
    assert "must belong to the selected channel" in response.json()["detail"]


def test_backtest_start_api_rejects_missing_dates(tmp_path: Path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={"channel": "@Tofan_Trade"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "from_date and to_date are required"


def test_backtest_channel_api_saves_and_lists_channels(tmp_path: Path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    saved = client.post(
        "/api/backtests/channels",
        headers=_headers(),
        json={"channel": "@Crypto_Etehad"},
    )
    assert saved.status_code == 201
    body = saved.json()
    assert body["saved"] is True
    assert any(
        item["channel_resolved"] == "https://t.me/Crypto_Etehad"
        for item in body["channels"]
    )

    listed = client.get("/api/backtests/channels", headers=_headers())
    assert listed.status_code == 200
    listed_body = listed.json()
    assert any(
        item["channel_resolved"] == "https://t.me/Crypto_Etehad"
        for item in listed_body["channels"]
    )


def test_backtest_channel_api_removes_channels(tmp_path: Path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    client.post(
        "/api/backtests/channels",
        headers=_headers(),
        json={"channel": "@Crypto_Etehad"},
    )

    removed = client.request(
        "DELETE",
        "/api/backtests/channels",
        headers=_headers(),
        json={"channel": "https://t.me/Crypto_Etehad"},
    )
    assert removed.status_code == 200
    body = removed.json()
    assert body["deleted"] is True
    assert not any(
        item["channel_resolved"] == "https://t.me/Crypto_Etehad"
        for item in body["channels"]
    )


def test_backtest_channel_api_supports_query_token_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = build_client(tmp_path, monkeypatch)

    saved = client.post(
        "/api/backtests/channels?token=test-token",
        json={"channel": "@Crypto_Etehad"},
    )
    assert saved.status_code == 201
    assert saved.json()["saved"] is True

    listed = client.get("/api/backtests/channels?token=test-token")
    assert listed.status_code == 200
    assert any(
        item["channel_resolved"] == "https://t.me/Crypto_Etehad"
        for item in listed.json()["channels"]
    )


def test_backtest_rerun_api_starts_new_run_from_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = build_client(tmp_path, monkeypatch)
    start = client.post(
        "/api/backtests/start",
        headers=_headers(),
        json={
            "channel": "@Tofan_Trade",
            "from_date": "2026-06-03T00:00:00Z",
            "to_date": "2026-06-04T00:00:00Z",
            "start_message_link": "https://t.me/Tofan_Trade/5880",
            "interval": "1m",
            "max_messages": 1000,
            "use_ai": False,
            "send_log_channel": True,
            "log_per_message": True,
        },
    )
    assert start.status_code == 202
    original_run_id = start.json()["run"]["run_id"]

    rerun = client.post(
        f"/api/backtests/runs/{original_run_id}/rerun",
        headers=_headers(),
    )

    assert rerun.status_code == 202
    body = rerun.json()
    assert body["started"] is True
    assert body["rerun_of"] == original_run_id
    assert body["run"]["run_id"] != original_run_id
    assert body["run"]["start_message_id"] == 5880
    assert body["run"]["start_message_link"] == "https://t.me/Tofan_Trade/5880"


def test_backtest_stop_api_rejects_completed_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    run_id = start.json()["run"]["run_id"]
    for _ in range(50):
        loaded = client.get(f"/api/backtests/runs/{run_id}", headers=_headers()).json()
        if loaded["status"] == "completed":
            break
        time.sleep(0.02)

    stop = client.post(f"/api/backtests/runs/{run_id}/stop", headers=_headers())

    assert stop.status_code == 409
    assert stop.json()["stopped"] is False
    assert "run_not_stoppable_status_completed" == stop.json()["reason"]
