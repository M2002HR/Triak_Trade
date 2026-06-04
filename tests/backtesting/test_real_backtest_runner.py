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
        self.request_ranges: list[tuple[str, str, datetime, datetime]] = []

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        self.requests.append(symbol)
        self.request_ranges.append((symbol, interval, start_time, end_time))
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


class SkippingLogClient:
    def __init__(self) -> None:
        self.calls = 0

    async def send_text(self, text: str, *, real: bool = False) -> object:
        self.calls += 1
        return {
            "sent": False,
            "skipped": True,
            "reason": "guard disabled",
            "message_id": None,
        }


class TrackingTelegramClient(FakeTelegramClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ensure_calls: list[int] = []

    async def ensure_media_payload(self, message: RawTelegramMessage) -> RawTelegramMessage:
        self.ensure_calls.append(message.message_id)
        payload = dict(message.raw_payload)
        payload["image_data_urls"] = [
            {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,ZmFrZQ=="}
        ]
        payload["media_downloaded"] = True
        return message.model_copy(update={"raw_payload": payload})


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
        "TELEGRAM_LOG_CHANNEL_RETRY_DELAY_SECONDS": 0,
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


def _sapien_candles(now: datetime) -> list[Candle]:
    candles: list[Candle] = []
    for index, open_price, high_price, low_price, close_price in (
        (0, "0.09710", "0.09790", "0.09680", "0.09770"),
        (1, "0.09770", "0.09820", "0.09740", "0.09810"),
    ):
        open_time = now + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="SAPIENUSDT",
                interval="1m",
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=Decimal(open_price),
                high=Decimal(high_price),
                low=Decimal(low_price),
                close=Decimal(close_price),
                volume=Decimal("12"),
                source=CandleSource.TOOBIT,
            )
        )
    return candles


def _home_candles(now: datetime) -> list[Candle]:
    candles: list[Candle] = []
    for index, open_price, high_price, low_price, close_price in (
        (0, "1.00", "1.05", "0.99", "1.03"),
        (1, "1.03", "1.08", "1.01", "1.06"),
    ):
        open_time = now + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="HOMEUSDT",
                interval="1m",
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=Decimal(open_price),
                high=Decimal(high_price),
                low=Decimal(low_price),
                close=Decimal(close_price),
                volume=Decimal("25"),
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


def test_real_backtest_trace_formatter_escapes_html_sensitive_values(tmp_path: Path) -> None:
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=FakeTelegramClient(),
        market_data_provider=FakeMarketDataProvider(),
    )
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    trace = runner._make_trace(
        RawTelegramMessage(
            channel_id="https://t.me/Tofan_Trade",
            channel_username="Tofan_Trade",
            message_id=220,
            text="Signal <broken> & noisy [link](https://example.com)",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        )
    )
    trace.classification = "new_signal"
    trace.parsed_action = "open"
    trace.symbol = "BTCUSDT"
    trace.confidence = "0.91"
    trace.final_status = "invalid_signal"
    trace.result_summary = "Missing <SL> & TP"
    trace.debug_notes = ["reason=<bad> & uncertain"]
    runner._set_trace_stage(
        trace,
        "finalized",
        status="completed",
        detail="Final <decision> & detail",
    )

    rendered = runner._format_trace_for_telegram(trace)

    assert "<b>Backtest Message Trace</b>" in rendered
    assert "&lt;broken&gt;" in rendered
    assert "&lt;SL&gt;" in rendered
    assert "reason=&lt;bad&gt; &amp; uncertain" in rendered
    assert "Final &lt;decision&gt; &amp; detail" in rendered


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


def test_real_backtest_runner_starts_simulation_tracking_immediately_for_valid_signal(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)}),
    )
    progress_events = []

    runner.run_sync(
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

    classified = [
        event for event in progress_events
        if event.trace is not None and event.phase == "classify_messages"
    ]
    assert classified
    latest_classify_trace = classified[-1].trace
    assert latest_classify_trace is not None
    assert latest_classify_trace.final_status == "simulation_tracking"
    assert latest_classify_trace.current_stage == "simulated"
    market_stage = next(
        stage for stage in latest_classify_trace.stages if stage.key == "market_data"
    )
    simulated_stage = next(
        stage for stage in latest_classify_trace.stages if stage.key == "simulated"
    )
    assert market_stage.status == "completed"
    assert simulated_stage.status == "active"
    assert "Simulation tracking started" in (simulated_stage.detail or "")


def test_real_backtest_runner_anchors_market_data_to_start_message_time(
    tmp_path: Path,
) -> None:
    signal_time = datetime(2026, 6, 1, 16, 43, 56, tzinfo=timezone.utc)
    request_start = signal_time + timedelta(days=2)
    request_end = request_start + timedelta(hours=24)
    message = _message(
        signal_time,
        "BTCUSDT LONG MARKET SL: 67400 TP: 69000 / 70000 Leverage: 2x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(signal_time)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    progress_events = []

    runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=request_start,
            to_date=request_end,
            start_message_link="https://t.me/Tofan_Trade/1",
            start_message_id=1,
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        ),
        progress_callback=progress_events.append,
    )

    assert provider.request_ranges
    _symbol, _interval, market_start, market_end = provider.request_ranges[0]
    assert market_start == signal_time
    assert market_start < request_start
    assert market_end >= request_end
    traces = [event.trace for event in progress_events if event.trace is not None]
    assert traces
    debug_notes = "\n".join(traces[-1].debug_notes)
    assert "market_data_start_utc=2026-06-01T16:43:56+00:00" in debug_notes
    assert "market_data_start_tehran=2026-06-01T20:13:56+03:30" in debug_notes


def test_real_backtest_runner_records_stage_durations_in_trace(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=FakeTelegramClient(history_by_channel={}),
        market_data_provider=FakeMarketDataProvider(),
    )
    trace = runner._make_trace(_message(now, "BTCUSDT LONG MARKET SL: 67400 TP: 69000"))
    runner._set_trace_stage(
        trace,
        "received",
        status="completed",
        detail="Message pulled from Telegram history.",
    )
    runner._set_trace_stage(
        trace,
        "preprocess",
        status="active",
        detail="Preparing message payload for classification.",
    )
    runner._set_trace_stage(
        trace,
        "preprocess",
        status="completed",
        detail="Message payload prepared for classification.",
    )

    preprocess = next(stage for stage in trace.stages if stage.key == "preprocess")
    assert preprocess.started_at is not None
    assert preprocess.finished_at is not None
    assert preprocess.duration_ms is not None
    assert trace.processing_duration_ms is not None
    formatted = runner._format_trace_for_telegram(trace)
    assert "Processing Duration" in formatted
    assert "duration=" in formatted


def test_real_backtest_runner_skips_empty_messages_without_classifying_them(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    telegram = FakeTelegramClient(
        history_by_channel={
            "https://t.me/Tofan_Trade": [
                _message(
                    now,
                    "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 2x",
                ),
                RawTelegramMessage(
                    channel_id="https://t.me/Tofan_Trade",
                    channel_username="Tofan_Trade",
                    message_id=2,
                    text=None,
                    date=now + timedelta(minutes=1),
                    edited_at=None,
                    reply_to_msg_id=None,
                ),
            ]
        }
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)}),
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
            log_per_message=False,
        )
    )

    assert result.success is True
    assert result.classified_messages == 1
    assert result.ignored_messages == 1


def test_real_backtest_runner_activates_signal_after_follow_up_stop_loss(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    first = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text="HOMEUSDT LONG Entry: 1.00 - 1.10 TP: 1.20 / 1.30",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    second = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=11,
        text="SL: 0.95",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=10,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [first, second]}
    )
    provider = FakeMarketDataProvider(candles_by_symbol={"HOMEUSDT": _home_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
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
    assert result.valid_signals >= 1
    live_signal_events = [event.live_signals for event in progress_events if event.live_signals]
    assert live_signal_events
    latest_signal = live_signal_events[-1][0]
    assert latest_signal["symbol"] == "HOMEUSDT"
    assert latest_signal["status_group"] == "active"
    traces = [event.trace for event in progress_events if event.trace is not None]
    originating = next(trace for trace in reversed(traces) if trace.message_id == 10)
    assert originating is not None
    assert "signal_tracking_activated_from_follow_up" in originating.debug_notes


def test_real_backtest_runner_hydrates_caption_media_on_demand_only(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    first = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=1,
        text="caption signal",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={"has_media": True, "caption_present": True},
    )
    second = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=2,
        text=None,
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={"has_media": True, "caption_present": False},
    )
    telegram = TrackingTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [first, second]}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path, AI_GATEWAY_ENABLED=False),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
    )

    runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=10,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        )
    )

    assert telegram.ensure_calls == [1]


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
            REAL_BACKTEST_SEND_TO_LOG_CHANNEL=False,
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
    assert any("classification=new_signal" in text for text in log_client.texts)
    assert len(log_client.texts) >= 2


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


def test_real_backtest_runner_warns_when_log_channel_send_is_skipped(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)})
    log_client = SkippingLogClient()
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
    assert any("skipped:guard disabled" in warning for warning in result.warnings)


def test_real_backtest_runner_sends_failure_log_for_no_valid_signal_case(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    telegram = FakeTelegramClient(
        history_by_channel={
            "https://t.me/Tofan_Trade": [
                _message(now, "Random market commentary only"),
            ]
        }
    )
    log_client = FakeLogClient()
    runner = RealBacktestRunner(
        settings=_settings(
            tmp_path,
            AI_GATEWAY_ENABLED=False,
            REAL_BACKTEST_SEND_TO_LOG_CHANNEL=True,
        ),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
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

    assert result.success is False
    assert any("Real backtest finished without valid signals" in text for text in log_client.texts)


def test_real_backtest_runner_simulates_noisy_market_signal_immediately(tmp_path: Path) -> None:
    now = datetime(2026, 5, 28, 20, 24, 54, tzinfo=timezone.utc)
    message = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=5880,
        text=(
            "**سیگنال فیوچرز طوفان ترید ****🌪****\n\n"
            " SAPIEN/USD ****🌪****\n\n"
            "****🌪****LONG ****🌪****🌪****\n\n"
            "****🌪**** LEVERAGE: Cross 25x ****🌪****\n\n"
            "****⚙️**** Entry نقطه ورود ****⬇️****\n"
            "MARKET ****🕸****\n\n"
            "Targets : تارگت ها ****🔼****🔽****\n\n"  # noqa: RUF001
            "****1️⃣****   ****🌪****0.09775\n\n"
            "****2️⃣****   ****🌪****0.09800\n\n"
            "****3️⃣****   ****🌪****0.09850\n\n"
            "****4️⃣****  ****🌪**** 0.09950\n\n"
            "****➕**** ****⭐️**** ****🌪**** 0.10003\n\n"  # noqa: RUF001
            "****➕**** ****⭐️**** ****🌪**** 0.10200\n\n"  # noqa: RUF001
            "****⚙️**** STOPLOSS  حد ضرر ****⬇️****\n"
            "0.09480 ****⚠️****\n\n"
            "**\n"
            "🌪 [Trade on Toobit](https://t.me/Tofan_Trade/220) 🌪\n"
            "[مدیریت سرمایه رعایت شود ](https://t.me/Tofan_Trade/166)👑"
        ),
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"SAPIENUSDT": _sapien_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
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
    assert result.valid_signals == 1
    assert result.trades_simulated == 1
    assert "SAPIENUSDT" in provider.requests
    latest_trace = next(
        event.trace
        for event in reversed(progress_events)
        if event.trace is not None and event.trace.message_id == 5880
    )
    assert latest_trace is not None
    assert latest_trace.symbol == "SAPIENUSDT"
    assert latest_trace.current_stage == "finalized"
    assert latest_trace.final_status in {
        "tp_hit",
        "tp_hit_same_candle",
        "open_until_end",
        "partial_tp_open_until_end",
    }


def test_report_store_writes_json_and_markdown_and_latest(tmp_path: Path) -> None:
    store = BacktestReportStore(str(tmp_path))
    stored = store.write({"channel": "https://t.me/Tofan_Trade", "generated_at": "x"})
    assert Path(stored.json_path).exists()
    assert Path(stored.markdown_path).exists()
    assert store.latest() is not None
