from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from triak_trade.backtesting.real_runner import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.backtesting.report_store import BacktestReportStore
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle, RawTelegramMessage
from triak_trade.observability.errors import TelegramLogChannelError
from triak_trade.telegram.client import FakeTelegramClient


class FakeMarketDataProvider:
    def __init__(self, candles_by_symbol: dict[str, list[Candle]] | None = None) -> None:
        self.candles_by_symbol = candles_by_symbol or {}
        self.requests: list[str] = []

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        self.requests.append(symbol)
        return list(self.candles_by_symbol.get(symbol, []))

    async def get_latest_price(self, symbol: str) -> Decimal:
        return Decimal("0")


class FakeLogClient:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_text(self, text: str, *, real: bool = False) -> object:
        self.texts.append(text)
        return {"sent": True, "real": real}


class FailingLogClient:
    def __init__(self) -> None:
        self.calls = 0

    async def send_text(self, text: str, *, real: bool = False) -> object:
        self.calls += 1
        raise TelegramLogChannelError("simulated log channel failure")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "REAL_BACKTEST_ENABLED": True,
        "RUN_BACKTEST_INTEGRATION_TESTS": 1,
        "RUN_TELEGRAM_INTEGRATION_TESTS": 1,
        "RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS": 1,
        "TELEGRAM_API_ID": 123,
        "TELEGRAM_API_HASH": "fake-hash",
        "REAL_BACKTEST_REPORT_DIR": str(tmp_path),
        "BACKTEST_DEFAULT_FILL_POLICY": "conservative",
        "REAL_BACKTEST_SEND_TO_LOG_CHANNEL": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _message(now: datetime, text: str) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=1,
        text=text,
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )


def _candles(now: datetime) -> list[Candle]:
    candles: list[Candle] = []
    for index, open_price, high_price, low_price, close_price in (
        (0, "68010", "69050", "67950", "68900"),
        (1, "68900", "70100", "68800", "70050"),
    ):
        open_time = now + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=Decimal(open_price),
                high=Decimal(high_price),
                low=Decimal(low_price),
                close=Decimal(close_price),
                volume=Decimal("10"),
                source=CandleSource.TOOBIT,
            )
        )
    return candles


def test_real_backtest_runner_blocks_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, REAL_BACKTEST_ENABLED=False)
    runner = RealBacktestRunner(
        settings=settings,
        telegram_client=FakeTelegramClient(),
        market_data_provider=FakeMarketDataProvider(),
    )
    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            hours=24,
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
        )
    )
    assert result.success is False
    assert result.report_path is not None
    assert "REAL_BACKTEST_ENABLED=true is required" in "\n".join(result.errors)


def test_real_backtest_runner_uses_regex_fallback_and_fetches_candles(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)})
    settings = _settings(tmp_path, AI_GATEWAY_ENABLED=False)
    runner = RealBacktestRunner(
        settings=settings,
        telegram_client=telegram,
        market_data_provider=provider,
    )

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=True,
            send_telegram_summary=False,
            send_log_channel=False,
        )
    )

    assert result.success is False
    assert result.real_telegram_used is False
    assert result.real_market_data_used is False
    assert result.ai_used is False
    assert result.regex_fallback_used is False
    assert "AI gateway is required for this backtest run but is not enabled." in result.errors
    assert provider.requests == []
    assert result.report_path is not None


def test_real_backtest_runner_skips_unknown_symbol_safely(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    telegram = FakeTelegramClient(
        history_by_channel={
            "https://t.me/Tofan_Trade": [
                _message(
                    now,
                    "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 2x",
                )
            ]
        }
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
    )

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
        )
    )

    assert result.success is False
    assert "No candle data available for detected symbols" in "\n".join(result.errors)
    assert any("BTCUSDT" in item for item in result.skipped_reasons)


def test_real_backtest_runner_finalizes_trace_when_market_data_missing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    telegram = FakeTelegramClient(
        history_by_channel={
            "https://t.me/Tofan_Trade": [
                _message(
                    now,
                    "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 2x",
                )
            ]
        }
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
    )
    progress_events = []

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        ),
        progress_callback=progress_events.append,
    )

    assert result.success is False
    message_events = [event for event in progress_events if event.trace is not None]
    assert message_events
    final_trace = message_events[-1].trace
    assert final_trace is not None
    assert final_trace.final_status == "market_data_unavailable"
    assert final_trace.current_stage == "finalized"
    assert "No candle data returned" in (final_trace.result_summary or "")


def test_real_backtest_runner_emits_message_progress(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path, AI_GATEWAY_ENABLED=False),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    progress_events = []

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        ),
        progress_callback=progress_events.append,
    )

    assert result.success is True
    assert any(event.event_type == "run" for event in progress_events)
    assert any(event.event_type == "message" for event in progress_events)
    message_event = next(event for event in progress_events if event.trace is not None)
    assert message_event.trace is not None
    assert message_event.trace.message_link == "https://t.me/Tofan_Trade/1"
    assert message_event.trace.classification in {"new_signal", None}


def test_real_backtest_runner_sends_per_message_log_trace(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)})
    log_client = FakeLogClient()
    runner = RealBacktestRunner(
        settings=_settings(
            tmp_path,
            AI_GATEWAY_ENABLED=False,
            REAL_BACKTEST_SEND_TO_LOG_CHANNEL=True,
        ),
        telegram_client=telegram,
        market_data_provider=provider,
        log_client=log_client,
    )

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        )
    )

    assert result.success is True
    assert any("Backtest Message Trace" in text for text in log_client.texts)
    assert any("Message Link" in text for text in log_client.texts)


def test_real_backtest_runner_survives_log_channel_failures(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)})
    log_client = FailingLogClient()
    runner = RealBacktestRunner(
        settings=_settings(
            tmp_path,
            AI_GATEWAY_ENABLED=False,
            REAL_BACKTEST_SEND_TO_LOG_CHANNEL=True,
        ),
        telegram_client=telegram,
        market_data_provider=provider,
        log_client=log_client,
    )

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=True,
            log_per_message=True,
        )
    )

    assert result.success is True
    assert log_client.calls > 0
    assert any("Telegram log channel send failed" in warning for warning in result.warnings)
    assert not any("simulated log channel failure" == error for error in result.errors)


def test_report_store_writes_json_and_markdown_and_latest(tmp_path: Path) -> None:
    store = BacktestReportStore(str(tmp_path))
    stored = store.write({"channel": "https://t.me/Tofan_Trade", "generated_at": "x"})
    assert Path(stored.json_path).exists()
    assert Path(stored.markdown_path).exists()
    assert store.latest() is not None
