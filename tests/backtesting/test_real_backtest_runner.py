from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from triak_trade.backtesting.real_runner import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.backtesting.report_store import BacktestReportStore
from triak_trade.backtesting.simulator import (
    PriceLevelSpan,
    SignalPricePoint,
    SimulationSignalState,
    SimulationSnapshot,
)
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import CandleSource, SignalAction, TradeSide
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

    async def ensure_media_payload(
        self,
        message: RawTelegramMessage,
        *,
        allow_captionless: bool = False,
    ) -> RawTelegramMessage:
        self.ensure_calls.append(message.message_id)
        payload = dict(message.raw_payload)
        payload["image_data_urls"] = [
            {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,ZmFrZQ=="}
        ]
        payload["media_downloaded"] = True
        if allow_captionless:
            payload["media_download_mode"] = "captionless_allowed"
        return message.model_copy(update={"raw_payload": payload})


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "REAL_BACKTEST_ENABLED": True,
        "RUN_BACKTEST_INTEGRATION_TESTS": 1,
        "RUN_TELEGRAM_INTEGRATION_TESTS": 1,
        "RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS": 1,
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


def _1000shib_candles(now: datetime) -> list[Candle]:
    candles: list[Candle] = []
    for index, open_price, high_price, low_price, close_price in (
        (0, "0.005979", "0.006060", "0.005950", "0.006020"),
        (1, "0.006020", "0.006140", "0.005990", "0.006110"),
    ):
        open_time = now + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="1000SHIB-SWAP-USDT",
                interval="1m",
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=Decimal(open_price),
                high=Decimal(high_price),
                low=Decimal(low_price),
                close=Decimal(close_price),
                volume=Decimal("50"),
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


def test_real_backtest_runner_updates_live_state_on_new_message_before_refresh_interval(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    open_message = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=1,
        text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    close_message = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=2,
        text="Close BTCUSDT now",
        date=now + timedelta(minutes=10),
        edited_at=None,
        reply_to_msg_id=1,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [open_message, close_message]}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path, BACKTEST_LIFECYCLE_REFRESH_INTERVAL="30m"),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": _candles(now)}),
    )
    progress_events = []

    result = runner.run_sync(
        RealBacktestRunRequest(
            channel="https://t.me/Tofan_Trade",
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=20),
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
    simulate_message_events = [
        event
        for event in progress_events
        if event.event_type == "message" and event.phase == "simulate"
    ]
    assert len(
        [
            event
            for event in simulate_message_events
            if event.summary == "Live simulation state updated for message 1."
        ]
    ) >= 2
    assert any(
        event.live_metrics is not None
        and event.live_metrics.get("live_open_positions") == "0"
        for event in simulate_message_events
    )
    assert not any(
        event.event_type == "run"
        and event.phase == "simulate"
        and "Virtual lifecycle refresh checkpoint" in event.summary
        for event in progress_events
    )


def test_real_backtest_runner_exposes_market_signal_fill_from_first_available_candle(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, 30, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTCUSDT LONG MARKET SL: 67400 TP: 69000 / 70000 Leverage: 2x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    late_candles = _candles(now + timedelta(minutes=5))
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(candles_by_symbol={"BTCUSDT": late_candles}),
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
    assert result.trades_simulated == 1
    assert result.trades_filled == 1
    assert any(event.counts.get("trades_simulated") == 1 for event in progress_events)
    assert any(event.counts.get("trades_filled") == 1 for event in progress_events)
    live_signal_events = [event.live_signals for event in progress_events if event.live_signals]
    assert live_signal_events
    assert any(
        signal["signal_id"].startswith("sig_")
        and signal["entry_price"] == "68010"
        and signal["status"] == "partial_tp_complete"
        and signal["status_group"] == "inactive"
        for signals in live_signal_events
        for signal in signals
    )
    assert any(
        event.trace is not None and event.trace.final_status == "simulation_tracking"
        for event in progress_events
    )
    final_trace = next(
        event.trace
        for event in reversed(progress_events)
        if event.trace is not None and event.trace.message_id == 1
    )
    assert final_trace is not None
    assert final_trace.final_status == "partial_tp_complete"


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
    assert result.valid_signals == 1
    assert result.trades_simulated == 1
    assert result.trades_filled == 1
    live_signal_events = [event.live_signals for event in progress_events if event.live_signals]
    assert live_signal_events
    latest_signal = live_signal_events[-1][0]
    assert latest_signal["symbol"] == "HOMEUSDT"
    assert latest_signal["status"] == "open"
    assert latest_signal["status_group"] == "active"
    traces = [event.trace for event in progress_events if event.trace is not None]
    originating = next(trace for trace in reversed(traces) if trace.message_id == 10)
    assert originating is not None
    assert "signal_tracking_activated_from_follow_up" in originating.debug_notes


def test_real_backtest_runner_close_without_reply_attaches_by_symbol(
    tmp_path: Path,
) -> None:
    # A "close / save profit" follow-up with NO reply id must still attach to
    # the only open signal for that symbol and close the position.
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    first = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text="HOMEUSDT LONG Entry: 1.00 - 1.10 SL: 0.95 TP: 1.20 / 1.30",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    close_msg = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=11,
        text="سیو سود کنید",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=None,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [first, close_msg]}
    )
    provider = FakeMarketDataProvider(candles_by_symbol={"HOMEUSDT": _home_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    progress_events: list[object] = []

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
    # The close follow-up must have resolved to the HOME signal (by symbol),
    # not been dropped, and must not leave an "unattached" warning. Progress


def test_real_backtest_runner_market_symbol_is_normalized_on_event(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, 30, tzinfo=timezone.utc)
    message = _message(
        now,
        "BTC/USD LONG MARKET SL: 98 TP: 104 / 106 Leverage: 2x",
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(candles_by_symbol={"BTC-SWAP-USDT": _candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
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
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        )
    )

    assert result.success is True
    assert result.trades_simulated == 1
    assert result.trades_filled == 1


def test_real_backtest_runner_preserves_numeric_symbol_prefix_for_market_data_fetch(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, 30, tzinfo=timezone.utc)
    message = _message(
        now,
        (
            "1000SHIB/USDT BUY Entry zone: 0.005979 SL: 0.005680 "
            "TP1 0.006054 TP2 0.006128 TP3 0.006278 Leverage: 5x"
        ),
    )
    telegram = FakeTelegramClient(history_by_channel={"https://t.me/Tofan_Trade": [message]})
    provider = FakeMarketDataProvider(
        candles_by_symbol={"1000SHIB-SWAP-USDT": _1000shib_candles(now)}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
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
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            log_per_message=False,
        )
    )

    assert result.success is True
    assert provider.requests[0] == "1000SHIB-SWAP-USDT"
    assert "SHIB-SWAP-USDT" not in provider.requests


def test_real_backtest_runner_close_all_message_closes_every_open_signal(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    open_one = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text="BTCUSDT LONG Entry: 100 - 100 SL: 98 TP: 104 / 106",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    open_two = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=11,
        text="ETHUSDT LONG Entry: 50 - 50 SL: 49 TP: 53 / 55",
        date=now + timedelta(seconds=10),
        edited_at=None,
        reply_to_msg_id=None,
    )
    close_all = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=12,
        text="ببندید همه سیگنالارو",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=None,
    )
    btc_candles = [
        Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now,
            close_time=now + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
        Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now + timedelta(minutes=1),
            close_time=now + timedelta(minutes=2),
            open=Decimal("100.25"),
            high=Decimal("100.75"),
            low=Decimal("99.75"),
            close=Decimal("100.1"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
    ]
    eth_candles = [
        Candle(
            symbol="ETHUSDT",
            interval="1m",
            open_time=now,
            close_time=now + timedelta(minutes=1),
            open=Decimal("50"),
            high=Decimal("51"),
            low=Decimal("49.5"),
            close=Decimal("50.4"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
        Candle(
            symbol="ETHUSDT",
            interval="1m",
            open_time=now + timedelta(minutes=1),
            close_time=now + timedelta(minutes=2),
            open=Decimal("50.2"),
            high=Decimal("50.6"),
            low=Decimal("49.8"),
            close=Decimal("50.0"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
    ]
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [open_one, open_two, close_all]}
    )
    provider = FakeMarketDataProvider(
        candles_by_symbol={"BTCUSDT": btc_candles, "ETHUSDT": eth_candles}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    progress_events: list[object] = []

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
    assert result.trades_simulated == 2
    assert result.trades_filled == 2
    live_signal_events = [event.live_signals for event in progress_events if event.live_signals]
    assert live_signal_events
    latest = live_signal_events[-1]
    assert all(signal["status_group"] == "inactive" for signal in latest)
    # events emit deep-copied trace snapshots, so take the LAST one for msg 11
    # (after correlation has run), not the first (pre-resolution) snapshot.
    close_traces = [
        trace
        for event in progress_events
        if (trace := getattr(event, "trace", None)) is not None
        and trace.message_id == 12
    ]
    assert close_traces
    close_trace = close_traces[-1]
    assert close_trace.parsed_action == "close"
    assert close_trace.final_status == "follow_up"
    assert not any("followup_unattached" in note for note in close_trace.debug_notes)


def test_real_backtest_runner_exposes_leverage_in_live_signal(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    first = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text="HOMEUSDT LONG x10 Entry: 1.00 - 1.10 SL: 0.95 TP: 1.20 / 1.30",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [first]}
    )
    provider = FakeMarketDataProvider(candles_by_symbol={"HOMEUSDT": _home_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    progress_events: list[object] = []

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

    live_signal_events = [
        event.live_signals for event in progress_events if getattr(event, "live_signals", None)
    ]
    assert live_signal_events
    latest = live_signal_events[-1][0]
    assert "leverage" in latest
    assert "margin" in latest


def test_real_backtest_runner_keeps_closed_signal_levels_and_chart_metadata() -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    state = SimulationSignalState(
        signal_id="sig_10",
        symbol="HOMEUSDT",
        side=TradeSide.LONG,
        status="tp_hit",
        original_quantity=Decimal("10"),
        open_quantity=Decimal("0"),
        entry_price=Decimal("1.00"),
        stop_loss=Decimal("0.95"),
        take_profits=[Decimal("1.04"), Decimal("1.08")],
        notional_value=Decimal("10"),
        risk_amount=Decimal("0.5"),
        realized_pnl=Decimal("0.4"),
        unrealized_pnl=Decimal("0"),
        total_pnl_pct=Decimal("4"),
        mark_price=Decimal("1.04"),
        entry_time=now,
        exit_time=now + timedelta(minutes=1),
        exit_price=Decimal("1.04"),
        targets_hit=1,
        notes=["take_profit_hit=1.04"],
        declared_leverage=Decimal("10"),
        effective_leverage=Decimal("10"),
        margin=Decimal("1"),
        balance_basis=Decimal("10"),
        margin_pnl_pct=Decimal("40"),
        price_history=[
            SignalPricePoint(
                timestamp=now,
                candle_open_time=now,
                candle_close_time=now + timedelta(minutes=1),
                open=Decimal("1.00"),
                high=Decimal("1.05"),
                low=Decimal("0.99"),
                close=Decimal("1.04"),
                stop_loss=Decimal("0.95"),
                take_profits=[Decimal("1.04"), Decimal("1.08")],
                mark_price=Decimal("1.04"),
                source_message_id=10,
            ),
            SignalPricePoint(
                timestamp=now + timedelta(minutes=1),
                candle_open_time=now + timedelta(minutes=1),
                candle_close_time=now + timedelta(minutes=2),
                open=Decimal("1.04"),
                high=Decimal("1.06"),
                low=Decimal("1.03"),
                close=Decimal("1.05"),
                stop_loss=Decimal("0.95"),
                take_profits=[Decimal("1.04"), Decimal("1.08")],
                mark_price=Decimal("1.05"),
                source_message_id=10,
            ),
        ],
        stop_loss_history=[
            PriceLevelSpan(
                kind="stop_loss",
                label="SL",
                value=Decimal("0.95"),
                started_at=now,
            )
        ],
        take_profit_history=[
            PriceLevelSpan(
                kind="take_profit",
                label="TP1",
                value=Decimal("1.04"),
                started_at=now,
            )
        ],
    )
    snapshot = SimulationSnapshot(
        timestamp=now + timedelta(minutes=1),
        source_message_id=10,
        open_positions=0,
        closed_trades=1,
        wins=1,
        losses=0,
        realized_pnl=Decimal("0.4"),
        unrealized_pnl=Decimal("0"),
        total_pnl=Decimal("0.4"),
        realized_balance=Decimal("100.4"),
        current_balance=Decimal("100.4"),
        signal_states={"sig_10": state},
        checkpoint_kind="message",
    )
    latest = RealBacktestRunner._live_signals_from_snapshot(snapshot)[0]
    assert latest["status"] == "tp_hit"
    assert latest["leverage"] == "10"
    assert latest["declared_leverage"] == "10"
    assert latest["effective_leverage"] == "10"
    assert latest["stop_loss"] == "0.95"
    assert latest["take_profits"] == ["1.04", "1.08"]
    assert latest["take_profit_levels"] == ["1.04", "1.08"]
    assert latest["total_pnl_pct"] == "4"
    assert latest["margin_pnl_pct"] == "40"
    assert latest["balance_basis"] == "10"
    assert latest["chart"]["interval"] == "1m"
    assert latest["chart"]["candles"]
    assert "timestamp_ms" in latest["chart"]["candles"][0]
    assert "started_at_ms" in latest["chart"]["stop_loss_history"][0]


def test_real_backtest_runner_preserves_precise_small_price_chart_data() -> None:
    now = datetime(2026, 6, 6, tzinfo=timezone.utc)
    state = SimulationSignalState(
        signal_id="sig_small",
        symbol="SAPIENUSDT",
        side=TradeSide.LONG,
        status="open",
        original_quantity=Decimal("1000"),
        open_quantity=Decimal("1000"),
        entry_price=Decimal("0.0061234"),
        stop_loss=Decimal("0.0059012"),
        take_profits=[
            Decimal("0.0061784"),
            Decimal("0.0062454"),
            Decimal("0.0063174"),
        ],
        notional_value=Decimal("6.1234"),
        risk_amount=Decimal("0.2222"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0.0226"),
        total_pnl_pct=Decimal("0.3691"),
        mark_price=Decimal("0.0061459"),
        entry_time=now,
        exit_time=None,
        exit_price=None,
        targets_hit=0,
        notes=[],
        declared_leverage=Decimal("5"),
        effective_leverage=Decimal("5"),
        margin=Decimal("1.22468"),
        balance_basis=Decimal("10"),
        margin_pnl_pct=Decimal("1.8456"),
        price_history=[
            SignalPricePoint(
                timestamp=now,
                candle_open_time=now,
                candle_close_time=now + timedelta(minutes=1),
                open=Decimal("0.0061017"),
                high=Decimal("0.0061888"),
                low=Decimal("0.0060844"),
                close=Decimal("0.0061459"),
                stop_loss=Decimal("0.0059012"),
                take_profits=[
                    Decimal("0.0061784"),
                    Decimal("0.0062454"),
                    Decimal("0.0063174"),
                ],
                mark_price=Decimal("0.0061459"),
                source_message_id=42,
            )
        ],
        stop_loss_history=[
            PriceLevelSpan(
                kind="stop_loss",
                label="SL",
                value=Decimal("0.0059012"),
                started_at=now,
            )
        ],
        take_profit_history=[
            PriceLevelSpan(
                kind="take_profit",
                label="TP1",
                value=Decimal("0.0061784"),
                started_at=now,
            )
        ],
    )
    snapshot = SimulationSnapshot(
        timestamp=now + timedelta(minutes=1),
        source_message_id=42,
        open_positions=1,
        closed_trades=0,
        wins=0,
        losses=0,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0.0226"),
        total_pnl=Decimal("0.0226"),
        realized_balance=Decimal("100"),
        current_balance=Decimal("100.0226"),
        signal_states={"sig_small": state},
        checkpoint_kind="message",
    )

    latest = RealBacktestRunner._live_signals_from_snapshot(snapshot)[0]

    assert latest["entry_price"] == "0.006123"
    assert latest["entry_price_raw"] == "0.0061234"
    assert latest["stop_loss"] == "0.005901"
    assert latest["stop_loss_raw"] == "0.0059012"
    assert latest["take_profits"] == ["0.006178", "0.006245", "0.006317"]
    assert latest["take_profit_levels_raw"] == ["0.0061784", "0.0062454", "0.0063174"]
    assert latest["mark_price"] == "0.006146"
    assert latest["mark_price_raw"] == "0.0061459"
    assert latest["chart"]["candles"][0]["open"] == "0.0061017"
    assert latest["chart"]["candles"][0]["high"] == "0.0061888"
    assert latest["chart"]["stop_loss_history"][0]["value"] == "0.0059012"
    assert latest["chart"]["stop_loss_history"][0]["value_display"] == "0.005901"


def test_real_backtest_runner_skips_interval_progress_for_inactive_snapshots(
    tmp_path: Path,
) -> None:
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=FakeTelegramClient(),
        market_data_provider=FakeMarketDataProvider(),
    )
    events: list[object] = []

    runner._emit_interval_snapshots(
        snapshots=[
            SimulationSnapshot(
                timestamp=datetime(2026, 6, 2, 0, 30, tzinfo=timezone.utc),
                source_message_id=None,
                open_positions=0,
                closed_trades=1,
                wins=1,
                losses=0,
                realized_pnl=Decimal("5"),
                unrealized_pnl=Decimal("0"),
                total_pnl=Decimal("5"),
                realized_balance=Decimal("105"),
                current_balance=Decimal("105"),
                signal_states={},
                checkpoint_kind="interval",
            )
        ],
        latest_snapshot=SimulationSnapshot(
            timestamp=datetime(2026, 6, 2, 0, 30, tzinfo=timezone.utc),
            source_message_id=None,
            open_positions=0,
            closed_trades=1,
            wins=1,
            losses=0,
            realized_pnl=Decimal("5"),
            unrealized_pnl=Decimal("0"),
            total_pnl=Decimal("5"),
            realized_balance=Decimal("105"),
            current_balance=Decimal("105"),
            signal_states={},
            checkpoint_kind="interval",
        ),
        counts={"total_messages": 1},
        progress_callback=events.append,
        live_metrics={"live_open_positions": "0"},
        current_message_id=10,
    )

    assert events == []


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


def test_real_backtest_runner_attaches_prior_captionless_media_for_signal_followup(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    image_only = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text=None,
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={"has_media": True, "caption_present": False, "has_photo": True},
    )
    targets = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=11,
        text="تارگت 0.090 0.092 0.095",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=None,
    )
    telegram = TrackingTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [image_only, targets]}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path, AI_GATEWAY_ENABLED=False),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
    )

    result = runner.run_sync(
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

    assert result is not None
    assert telegram.ensure_calls == [10]


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
        "simulation_tracking",
        "open_until_end",
        "partial_tp_open_until_end",
    }


class _PromotionClassifier:
    """Classifies a reply parent as OPEN but NOT a new signal (msg 6285 case)."""

    def classify(self, message, context):  # type: ignore[no-untyped-def]
        from triak_trade.agents.classifier import ClassifiedMessage
        from triak_trade.domain.enums import (
            EntryType,
            MarketType,
            SignalAction,
            TradeSide,
        )
        from triak_trade.domain.models import ParsedSignal

        if message.message_id == 10:
            parsed = ParsedSignal(
                action=SignalAction.OPEN,
                market=MarketType.FUTURES,
                symbol="HOMEUSDT",
                side=TradeSide.LONG,
                entry_type=EntryType.LIMIT,
                entry_low=Decimal("1.00"),
                entry_high=Decimal("1.05"),
                stop_loss=Decimal("0.95"),
                take_profits=[Decimal("1.20")],
                leverage=10,
                confidence=Decimal("0.9"),
                invalid_reason=None,
                source_channel_id=message.channel_id,
                source_message_id=message.message_id,
                parser_version="ai-v1",
            )
            return ClassifiedMessage(
                raw_message=message,
                normalized_message=None,
                parsed_signal=parsed,
                is_potential_new_signal=False,
                is_related_to_existing_signal=False,
                related_signal_id=None,
                relation_reason="ai-classified-not-new",
                confidence=Decimal("0.9"),
                debug_notes=["classifier=ai"],
            )
        parsed = ParsedSignal(
            action=SignalAction.CLOSE,
            market=MarketType.FUTURES,
            symbol=None,
            side=TradeSide.UNKNOWN,
            entry_type=EntryType.UNKNOWN,
            entry_low=None,
            entry_high=None,
            stop_loss=None,
            take_profits=[],
            leverage=None,
            confidence=Decimal("0.8"),
            invalid_reason=None,
            source_channel_id=message.channel_id,
            source_message_id=message.message_id,
            parser_version="ai-v1",
        )
        return ClassifiedMessage(
            raw_message=message,
            normalized_message=None,
            parsed_signal=parsed,
            is_potential_new_signal=False,
            is_related_to_existing_signal=True,
            related_signal_id=None,
            relation_reason="reply",
            confidence=Decimal("0.8"),
            debug_notes=["classifier=ai"],
        )


def test_real_backtest_runner_promotes_unrecognized_reply_parent(tmp_path: Path) -> None:
    import asyncio

    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    parent = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=10,
        text="HOMEUSDT LONG signal",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    reply = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=11,
        text="سیو سود کنید",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=10,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [parent, reply]}
    )
    provider = FakeMarketDataProvider(candles_by_symbol={"HOMEUSDT": _home_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    request = RealBacktestRunRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=now - timedelta(minutes=1),
        to_date=now + timedelta(minutes=10),
        interval="1m",
        max_messages=100,
        use_ai=True,
        send_telegram_summary=False,
        send_log_channel=False,
        log_per_message=False,
    )
    counts = {
        "total_messages": 2,
        "caption_media_candidates": 0,
        "classified_messages": 0,
        "parsed_signals": 0,
        "valid_signals": 0,
        "invalid_signals": 0,
        "ignored_messages": 0,
        "ambiguous_messages": 0,
        "ai_failed_messages": 0,
        "trades_simulated": 0,
        "trades_filled": 0,
    }

    (
        events,
        traces_by_message_id,
        signal_trace_map,
        _symbol_trace_map,
        counts,
        _prefetched,
    ) = asyncio.run(
        runner._build_events_with_traces(
            request=request,
            classifier=_PromotionClassifier(),
            messages=[parent, reply],
            progress_callback=None,
            counts=counts,
            warnings=[],
            prefetched_candles_by_symbol={},
        )
    )

    open_events = [
        event
        for event in events
        if event.source_message_id == 10 and event.action.value == "open"
    ]
    assert open_events, "reply parent should have been promoted into an OPEN event"
    promoted = open_events[0]
    assert promoted.signal_id is not None
    assert "promoted_from_reply" in promoted.debug_notes

    reply_events = [event for event in events if event.source_message_id == 11]
    assert reply_events
    assert reply_events[-1].related_signal_id == promoted.signal_id

    parent_trace = traces_by_message_id[10]
    # The parent was promoted from "ambiguous/ignored" into a tracked, simulated
    # signal (which the reply then closes), so it must no longer be ambiguous.
    assert parent_trace.final_status not in {"ambiguous", "ignored", "queued"}
    assert "signal_tracking_activated_from_follow_up" in parent_trace.debug_notes
    assert promoted.signal_id in signal_trace_map


class _OpenThenStopUpdateClassifier:
    """OPEN signal followed by an update_sl follow-up (AI-style labelling)."""

    def classify(self, message, context):  # type: ignore[no-untyped-def]
        from triak_trade.agents.classifier import ClassifiedMessage
        from triak_trade.domain.enums import (
            EntryType,
            MarketType,
            SignalAction,
            TradeSide,
        )
        from triak_trade.domain.models import ParsedSignal

        if message.message_id == 20:
            parsed = ParsedSignal(
                action=SignalAction.OPEN,
                market=MarketType.FUTURES,
                symbol="HOMEUSDT",
                side=TradeSide.LONG,
                entry_type=EntryType.RANGE,
                entry_low=Decimal("1.00"),
                entry_high=Decimal("1.05"),
                stop_loss=Decimal("0.95"),
                take_profits=[Decimal("1.20")],
                leverage=10,
                confidence=Decimal("0.9"),
                invalid_reason=None,
                source_channel_id=message.channel_id,
                source_message_id=message.message_id,
                parser_version="ai-v1",
            )
            return ClassifiedMessage(
                raw_message=message,
                normalized_message=None,
                parsed_signal=parsed,
                is_potential_new_signal=True,
                is_related_to_existing_signal=False,
                related_signal_id=None,
                relation_reason=None,
                confidence=Decimal("0.9"),
                debug_notes=["classifier=ai"],
            )
        parsed = ParsedSignal(
            action=SignalAction.UPDATE_SL,
            market=MarketType.FUTURES,
            symbol="HOMEUSDT",
            side=TradeSide.UNKNOWN,
            entry_type=EntryType.UNKNOWN,
            entry_low=None,
            entry_high=None,
            stop_loss=Decimal("0.97"),
            take_profits=[],
            leverage=None,
            confidence=Decimal("0.85"),
            invalid_reason=None,
            source_channel_id=message.channel_id,
            source_message_id=message.message_id,
            parser_version="ai-v1",
        )
        return ClassifiedMessage(
            raw_message=message,
            normalized_message=None,
            parsed_signal=parsed,
            is_potential_new_signal=False,
            is_related_to_existing_signal=True,
            related_signal_id=None,
            relation_reason="reply",
            confidence=Decimal("0.85"),
            debug_notes=["classifier=ai"],
        )


def test_followup_update_does_not_corrupt_base_open_event(tmp_path: Path) -> None:
    # An update_sl follow-up must NOT overwrite the base OPEN event's action;
    # otherwise the simulator skips it and the signal silently never trades.
    import asyncio

    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    open_msg = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=20,
        text="HOMEUSDT LONG signal",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    sl_update = RawTelegramMessage(
        channel_id="https://t.me/Tofan_Trade",
        channel_username="Tofan_Trade",
        message_id=21,
        text="استاپ 0.97",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=20,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Tofan_Trade": [open_msg, sl_update]}
    )
    provider = FakeMarketDataProvider(candles_by_symbol={"HOMEUSDT": _home_candles(now)})
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=provider,
    )
    request = RealBacktestRunRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=now - timedelta(minutes=1),
        to_date=now + timedelta(minutes=10),
        interval="1m",
        max_messages=100,
        use_ai=True,
        send_telegram_summary=False,
        send_log_channel=False,
        log_per_message=False,
    )
    counts = {
        "total_messages": 2,
        "caption_media_candidates": 0,
        "classified_messages": 0,
        "parsed_signals": 0,
        "valid_signals": 0,
        "invalid_signals": 0,
        "ignored_messages": 0,
        "ambiguous_messages": 0,
        "ai_failed_messages": 0,
        "trades_simulated": 0,
        "trades_filled": 0,
    }

    events, _traces, signal_trace_map, _sym, _counts, _pref = asyncio.run(
        runner._build_events_with_traces(
            request=request,
            classifier=_OpenThenStopUpdateClassifier(),
            messages=[open_msg, sl_update],
            progress_callback=None,
            counts=counts,
            warnings=[],
            prefetched_candles_by_symbol={},
        )
    )

    open_events = [
        event
        for event in events
        if event.source_message_id == 20 and event.signal_id is not None
    ]
    assert open_events
    # Despite the update_sl follow-up merging into the signal, the base event
    # must remain an OPEN so the simulator actually opens (and trades) it.
    assert open_events[0].action.value == "open"
    assert open_events[0].parsed_signal.action.value == "open"
    assert open_events[0].signal_id in signal_trace_map


class _DuplicateOpenSameSymbolClassifier:
    """Two OPEN-like messages for the same symbol; second must become follow-up."""

    def classify(self, message, context):  # type: ignore[no-untyped-def]
        from triak_trade.agents.classifier import ClassifiedMessage
        from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
        from triak_trade.domain.models import ParsedSignal

        if message.message_id == 30:
            parsed = ParsedSignal(
                action=SignalAction.OPEN,
                market=MarketType.FUTURES,
                symbol="DOGEUSDT",
                side=TradeSide.SHORT,
                entry_type=EntryType.MARKET,
                entry_low=None,
                entry_high=None,
                stop_loss=Decimal("0.08915"),
                take_profits=[Decimal("0.087"), Decimal("0.085")],
                leverage=10,
                confidence=Decimal("0.90"),
                invalid_reason=None,
                source_channel_id=message.channel_id,
                source_message_id=message.message_id,
                parser_version="ai-v1",
            )
        else:
            parsed = ParsedSignal(
                action=SignalAction.OPEN,
                market=MarketType.FUTURES,
                symbol="DOGEUSDT",
                side=TradeSide.SHORT,
                entry_type=EntryType.UNKNOWN,
                entry_low=None,
                entry_high=None,
                stop_loss=Decimal("0.08880"),
                take_profits=[Decimal("0.0865"), Decimal("0.0845")],
                leverage=10,
                confidence=Decimal("0.87"),
                invalid_reason=None,
                source_channel_id=message.channel_id,
                source_message_id=message.message_id,
                parser_version="ai-v1",
            )
        return ClassifiedMessage(
            raw_message=message,
            normalized_message=None,
            parsed_signal=parsed,
            is_potential_new_signal=True,
            is_related_to_existing_signal=False,
            related_signal_id=None,
            relation_reason=None,
            confidence=parsed.confidence,
            debug_notes=["classifier=ai"],
        )


def test_second_open_for_same_symbol_is_rerouted_to_followup(tmp_path: Path) -> None:
    import asyncio

    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    first = RawTelegramMessage(
        channel_id="https://t.me/Crypto_Etehad",
        channel_username="Crypto_Etehad",
        message_id=30,
        text="DOGE/USDT SHORT MARKET SL 0.08915 TP 0.087 0.085",
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    second = RawTelegramMessage(
        channel_id="https://t.me/Crypto_Etehad",
        channel_username="Crypto_Etehad",
        message_id=31,
        text="DOGE update TP 0.0865 0.0845 SL 0.08880",
        date=now + timedelta(minutes=1),
        edited_at=None,
        reply_to_msg_id=None,
    )
    telegram = FakeTelegramClient(
        history_by_channel={"https://t.me/Crypto_Etehad": [first, second]}
    )
    runner = RealBacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(),
    )
    request = RealBacktestRunRequest(
        channel="https://t.me/Crypto_Etehad",
        from_date=now - timedelta(minutes=1),
        to_date=now + timedelta(minutes=10),
        interval="1m",
        max_messages=50,
        use_ai=True,
        send_telegram_summary=False,
        send_log_channel=False,
        log_per_message=False,
    )
    counts = {
        "total_messages": 2,
        "caption_media_candidates": 0,
        "classified_messages": 0,
        "parsed_signals": 0,
        "valid_signals": 0,
        "invalid_signals": 0,
        "ignored_messages": 0,
        "ambiguous_messages": 0,
        "ai_failed_messages": 0,
        "trades_simulated": 0,
        "trades_filled": 0,
    }

    events, traces, _signal_trace_map, _sym, _counts, _pref = asyncio.run(
        runner._build_events_with_traces(
            request=request,
            classifier=_DuplicateOpenSameSymbolClassifier(),
            messages=[first, second],
            progress_callback=None,
            counts=counts,
            warnings=[],
            prefetched_candles_by_symbol={},
        )
    )

    open_events = [event for event in events if event.action is SignalAction.OPEN]
    assert len(open_events) == 1
    followup_events = [event for event in events if event.source_message_id == 31]
    assert followup_events
    assert followup_events[0].related_signal_id == open_events[0].signal_id
    assert followup_events[0].action is not SignalAction.OPEN
    second_trace = traces[31]
    assert "rerouted_open_to_followup; symbol_owner=" in " ".join(second_trace.debug_notes)


def test_report_store_writes_json_and_markdown_and_latest(tmp_path: Path) -> None:
    store = BacktestReportStore(str(tmp_path))
    stored = store.write({"channel": "https://t.me/Tofan_Trade", "generated_at": "x"})
    assert Path(stored.json_path).exists()
    assert Path(stored.markdown_path).exists()
    assert store.latest() is not None
