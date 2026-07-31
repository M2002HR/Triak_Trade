from __future__ import annotations

import asyncio
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from queue import Queue

import pytest
from pydantic import ValidationError

from triak_trade.backtesting.backtest_runner import (
    BacktestRunner,
    BacktestRunRequest,
    BacktestSignalRecord,
    _build_backtest_worker_cpu_assignments,
    _initialize_backtest_worker,
    _load_candle_artifact,
    _probe_backtest_worker_cpu,
    _write_candle_artifact,
    backtest_available_cpu_ids,
    backtest_worker_multiprocessing_context,
    default_backtest_parallel_workers,
)
from triak_trade.backtesting.models import BacktestEvent
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import (
    CandleSource,
    EntryType,
    MarketType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import Candle, ParsedSignal, RawTelegramMessage
from triak_trade.telegram.client import FakeTelegramClient


class FakeMarketDataProvider:
    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self.candles_by_symbol = candles_by_symbol

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        candles = self.candles_by_symbol.get(symbol, [])
        return [
            candle
            for candle in candles
            if candle.open_time >= start_time and candle.close_time <= end_time
        ]

    async def get_latest_price(self, symbol: str) -> Decimal:
        return Decimal("0")


class DelayedMarketDataProvider(FakeMarketDataProvider):
    def __init__(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        *,
        delayed_symbols: set[str],
        delay_seconds: float,
    ) -> None:
        super().__init__(candles_by_symbol)
        self.delayed_symbols = delayed_symbols
        self.delay_seconds = delay_seconds
        self.requests: list[str] = []

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        self.requests.append(symbol)
        if symbol in self.delayed_symbols:
            await asyncio.sleep(self.delay_seconds)
        return await super().get_klines(symbol, interval, start_time, end_time)


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
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _message(
    *,
    channel_id: str,
    channel_username: str,
    message_id: int,
    date: datetime,
    text: str,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id=channel_id,
        channel_username=channel_username,
        message_id=message_id,
        text=text,
        date=date,
        edited_at=None,
        reply_to_msg_id=None,
    )


def _candle(
    *,
    symbol: str,
    interval: str,
    open_time: datetime,
    open: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        source=CandleSource.TOOBIT,
    )


def _parsed_signal(
    *,
    action: SignalAction = SignalAction.OPEN,
    stop_loss: str = "95",
    take_profits: list[str] | None = None,
    leverage: int = 5,
) -> ParsedSignal:
    return ParsedSignal(
        action=action,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("100"),
        stop_loss=Decimal(stop_loss),
        take_profits=[
            Decimal(item) for item in (take_profits or ["110"])
        ],
        leverage=leverage,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="channel",
        source_message_id=1,
        parser_version="test",
    )


def test_live_consolidation_delays_entry_and_merges_only_pending_updates(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    signal_id = "signal-1"
    open_event = BacktestEvent(
        timestamp=now,
        action=SignalAction.OPEN,
        signal_id=signal_id,
        parsed_signal=_parsed_signal(),
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    pending_update = BacktestEvent(
        timestamp=now + timedelta(seconds=60),
        action=SignalAction.UPDATE_ENTRY,
        signal_id=signal_id,
        parsed_signal=_parsed_signal(
            action=SignalAction.UPDATE_ENTRY,
            stop_loss="96",
            take_profits=["112"],
            leverage=7,
        ),
        related_signal_id=signal_id,
        debug_notes=[],
        source_message_id=2,
    )
    post_open_update = BacktestEvent(
        timestamp=now + timedelta(seconds=240),
        action=SignalAction.UPDATE_TP,
        signal_id=signal_id,
        parsed_signal=_parsed_signal(
            action=SignalAction.UPDATE_TP,
            take_profits=["115"],
        ),
        related_signal_id=signal_id,
        debug_notes=[],
        source_message_id=3,
    )
    events = [open_event, pending_update, post_open_update]
    records = {
        signal_id: BacktestSignalRecord(
            signal_id=signal_id,
            channel_id="channel",
            symbol="BTCUSDT",
            side="long",
            source_message_id=1,
            source_message_date=now,
            events=list(events),
        )
    }
    runner = BacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=FakeTelegramClient(),
        market_data_provider=FakeMarketDataProvider({}),
    )

    runner._apply_live_consolidation(
        request=BacktestRunRequest(
            channel="channel",
            hours=1,
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
            consolidation_seconds=180,
        ),
        events=events,
        records_by_signal_id=records,
        traces_by_message_id={},
        symbol_trace_map={"BTCUSDT": [1]},
        tracked_signal_ids={signal_id},
        counts={"valid_signals": 1, "invalid_signals": 0},
    )

    consolidated = records[signal_id].events[0]
    assert consolidated.timestamp == now + timedelta(seconds=180)
    assert consolidated.parsed_signal.action is SignalAction.OPEN
    assert consolidated.parsed_signal.stop_loss == Decimal("96")
    assert consolidated.parsed_signal.take_profits == [Decimal("112")]
    assert consolidated.parsed_signal.leverage == 7
    assert len(records[signal_id].events) == 2
    assert len(events) == 2
    assert records[signal_id].events[1].parsed_signal.take_profits == [
        Decimal("115")
    ]


def test_backtest_runner_builds_independent_signal_results(tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    channel = "https://t.me/Tofan_Trade"
    messages = [
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=100,
            date=now,
            text="BTCUSDT LONG Entry: 100 - 100 SL: 98 TP: 104 Leverage: 5x",
        ),
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=101,
            date=now + timedelta(minutes=5),
            text="ETHUSDT SHORT Entry: 200 - 200 SL: 205 TP: 190 Leverage: 5x",
        ),
    ]
    telegram = FakeTelegramClient(history_by_channel={channel: messages})
    candles_by_symbol = {
        "BTCUSDT": [
            _candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time=now,
                open="100",
                high="104.5",
                low="99",
                close="104",
            )
        ],
        "ETHUSDT": [
            _candle(
                symbol="ETHUSDT",
                interval="1m",
                open_time=now + timedelta(minutes=5),
                open="200",
                high="206",
                low="199",
                close="205.5",
            )
        ],
    }
    runner = BacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider(candles_by_symbol),
    )

    result = runner.run_sync(
        BacktestRunRequest(
            channel=channel,
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            capital_per_signal=Decimal("100"),
            risk_per_trade_pct=Decimal("120"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
                log_per_message=False,
                max_parallel_signals=1,
                consolidation_seconds=0,
            )
    )

    assert result.success is True
    assert result.run_type == "backtest"
    assert result.trades_simulated == 2
    assert result.trades_filled == 2
    assert result.wins == 1
    assert result.losses == 1
    assert len(result.signals) == 2
    assert result.aggregate["total_signals"] == 2
    assert result.aggregate["filled_signals"] == 2
    assert result.aggregate["wins"] == 1
    assert result.aggregate["losses"] == 1
    assert result.aggregate["period_pnl"]["daily"][0]["signals"] == 2
    assert result.phase_durations_ms["fetch_history"] >= 0
    assert result.phase_durations_ms["classify_messages"] >= 0
    assert result.phase_durations_ms["fetch_market_data"] >= 0
    assert result.phase_durations_ms["simulate"] >= 0
    assert result.phase_durations_ms["report"] >= 0
    assert result.report_path is not None


def test_backtest_replays_oversized_symbol_windows_in_bounded_segments(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    channel = "https://t.me/Tofan_Trade"
    messages = [
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=100,
            date=now,
            text="BTCUSDT LONG Entry: 100 - 100 SL: 98 TP: 104 Leverage: 5x",
        ),
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=101,
            date=now + timedelta(minutes=8),
            text="ETHUSDT LONG Entry: 200 - 200 SL: 198 TP: 204 Leverage: 5x",
        ),
    ]
    candles = {
        "BTCUSDT": [
            _candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time=now + timedelta(minutes=index),
                open="100",
                high="104.5",
                low="99",
                close="104",
            )
            for index in range(10)
        ],
        "ETHUSDT": [
            _candle(
                symbol="ETHUSDT",
                interval="1m",
                open_time=now + timedelta(minutes=8),
                open="200",
                high="204.5",
                low="199",
                close="204",
            )
        ]
    }
    runner = BacktestRunner(
        settings=_settings(tmp_path, BACKTEST_MAX_CANDLES_PER_SYMBOL=2),
        telegram_client=FakeTelegramClient(history_by_channel={channel: messages}),
        market_data_provider=FakeMarketDataProvider(candles),
    )

    result = runner.run_sync(
        BacktestRunRequest(
            channel=channel,
            from_date=now,
            to_date=now + timedelta(minutes=10),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            capital_per_signal=Decimal("100"),
            risk_per_trade_pct=Decimal("120"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
                log_per_message=False,
                max_parallel_signals=1,
                consolidation_seconds=0,
            )
    )

    assert result.success is True
    assert {signal["symbol"] for signal in result.signals} == {"BTCUSDT", "ETHUSDT"}
    assert result.skipped_reasons == []
    assert result.candles_fetched == 11


def test_backtest_times_out_one_symbol_and_continues_with_the_rest(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    channel = "https://t.me/Tofan_Trade"
    messages = [
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=100,
            date=now,
            text="BTCUSDT LONG Entry: 100 - 100 SL: 98 TP: 104 Leverage: 5x",
        ),
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=101,
            date=now,
            text="ETHUSDT LONG Entry: 200 - 200 SL: 198 TP: 204 Leverage: 5x",
        ),
    ]
    provider = DelayedMarketDataProvider(
        {
            "BTCUSDT": [
                _candle(
                    symbol="BTCUSDT",
                    interval="1m",
                    open_time=now,
                    open="100",
                    high="104.5",
                    low="99",
                    close="104",
                )
            ],
            "ETHUSDT": [
                _candle(
                    symbol="ETHUSDT",
                    interval="1m",
                    open_time=now,
                    open="200",
                    high="204.5",
                    low="199",
                    close="204",
                )
            ],
        },
        delayed_symbols={"BTCUSDT"},
        delay_seconds=1.05,
    )
    runner = BacktestRunner(
        settings=_settings(
            tmp_path,
            BACKTEST_MAX_CANDLES_PER_SYMBOL=100,
            BACKTEST_MARKET_DATA_TIMEOUT_SECONDS=1,
        ),
        telegram_client=FakeTelegramClient(history_by_channel={channel: messages}),
        market_data_provider=provider,
    )

    result = runner.run_sync(
        BacktestRunRequest(
            channel=channel,
            from_date=now,
            to_date=now + timedelta(minutes=1),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            capital_per_signal=Decimal("100"),
            risk_per_trade_pct=Decimal("120"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
                log_per_message=False,
                max_parallel_signals=1,
                consolidation_seconds=0,
            )
    )

    assert result.success is True
    assert [signal["symbol"] for signal in result.signals] == ["ETHUSDT"]
    assert (
        any(
            reason.endswith("candle fetch failed (MarketDataFetchDeadlineExceeded)")
            for reason in result.skipped_reasons
        )
    )
    assert len(provider.requests) >= 2


def test_backtest_candle_artifact_round_trip_streams_each_candle(tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    candles = [
        _candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now + timedelta(minutes=index),
            open="100",
            high="101",
            low="99",
            close="100",
        )
        for index in range(3)
    ]

    artifact = _write_candle_artifact(
        artifact_root=tmp_path,
        symbol="BTCUSDT",
        candles=candles,
    )

    assert _load_candle_artifact(artifact) == candles


@pytest.mark.parametrize(
    ("setting_name", "invalid_value"),
    [
        ("BACKTEST_MAX_CANDLES_PER_SYMBOL", 0),
        ("BACKTEST_MARKET_DATA_TIMEOUT_SECONDS", 0),
        ("BACKTEST_MARKET_DATA_MAX_CONCURRENCY", 0),
    ],
)
def test_backtest_market_data_safety_limits_cannot_be_disabled(
    tmp_path: Path,
    setting_name: str,
    invalid_value: int,
) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, **{setting_name: invalid_value})


def test_backtest_runner_compacts_long_chart_payloads(tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    channel = "https://t.me/Tofan_Trade"
    messages = [
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=100,
            date=now,
            text="BTCUSDT LONG Entry: 100 - 100 SL: 98 TP: 104 Leverage: 5x",
        ),
    ]
    telegram = FakeTelegramClient(history_by_channel={channel: messages})
    candles = [
        _candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now + timedelta(minutes=index),
            open=str(100 + (index % 3)),
            high=str(101 + (index % 3)),
            low="99",
            close=str(100 + ((index + 1) % 3)),
        )
        for index in range(1305)
    ]
    runner = BacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider({"BTCUSDT": candles}),
    )

    result = runner.run_sync(
        BacktestRunRequest(
            channel=channel,
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=1305),
            interval="1m",
            max_messages=100,
            initial_balance=Decimal("100"),
            capital_per_signal=Decimal("100"),
            risk_per_trade_pct=Decimal("120"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
                log_per_message=False,
                max_parallel_signals=1,
                consolidation_seconds=0,
            )
    )

    signal = result.signals[0]
    assert signal["chart"]["source_points"] == 1305
    assert signal["chart"]["visible_points"] <= 1200
    assert signal["chart"]["visible_points"] < signal["chart"]["source_points"]


def test_backtest_runner_failure_keeps_zeroed_aggregate(tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    channel = "https://t.me/Tofan_Trade"
    messages = [
        _message(
            channel_id=channel,
            channel_username="Tofan_Trade",
            message_id=100,
            date=now,
            text="General market update. No trade setup here.",
        ),
    ]
    telegram = FakeTelegramClient(history_by_channel={channel: messages})
    runner = BacktestRunner(
        settings=_settings(tmp_path),
        telegram_client=telegram,
        market_data_provider=FakeMarketDataProvider({}),
    )

    result = runner.run_sync(
        BacktestRunRequest(
            channel=channel,
            from_date=now - timedelta(minutes=1),
            to_date=now + timedelta(minutes=1),
            interval="1m",
            max_messages=10,
            initial_balance=Decimal("100"),
            capital_per_signal=Decimal("100"),
            risk_per_trade_pct=Decimal("1"),
            use_ai=False,
            send_telegram_summary=False,
            send_log_channel=False,
                log_per_message=False,
                max_parallel_signals=1,
                consolidation_seconds=0,
            )
    )

    assert result.success is False
    assert result.signals == []
    assert result.total_pnl == Decimal("0")
    assert result.aggregate == {
        "total_signals": 0,
        "filled_signals": 0,
        "closed_signals": 0,
        "open_signals": 0,
        "not_filled_signals": 0,
        "wins": 0,
        "losses": 0,
        "status_counts": {},
        "total_pnl": "0",
        "avg_pnl": "0",
        "median_pnl": "0",
        "avg_pnl_pct": "0",
        "median_pnl_pct": "0",
        "gross_profit": "0",
        "gross_loss": "0",
        "profit_factor": None,
        "win_rate": "0",
        "max_drawdown": "0",
        "total_initial_balance": "0",
        "total_final_balance": "0",
        "period_pnl": {"daily": [], "weekly": [], "monthly": []},
        "symbol_summary": [],
        "equity_curve": [],
        "best_signals": [],
        "worst_signals": [],
    }


def test_backtest_parallel_worker_default_uses_available_cpu_count(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "triak_trade.backtesting.backtest_runner.backtest_available_cpu_ids",
        lambda: (2, 3, 4, 5),
    )

    request = BacktestRunRequest(
        channel="https://t.me/Tofan_Trade",
        from_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 5, tzinfo=timezone.utc),
        interval="1m",
        max_messages=10,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("3"),
        use_ai=False,
        send_telegram_summary=False,
        send_log_channel=False,
        log_per_message=False,
    )

    assert default_backtest_parallel_workers() == 4
    assert request.max_parallel_signals == 4


def test_backtest_worker_cpu_assignments_cap_to_available_cpu_slots(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "triak_trade.backtesting.backtest_runner.backtest_available_cpu_ids",
        lambda: (1, 4, 6),
    )

    assert _build_backtest_worker_cpu_assignments(8) == [1, 4, 6]


def test_initialize_backtest_worker_sets_process_affinity(
    monkeypatch,
) -> None:
    assigned: list[tuple[int, set[int]]] = []
    cpu_queue: Queue[int] = Queue()
    cpu_queue.put(7)
    monkeypatch.setattr(
        os,
        "sched_setaffinity",
        lambda pid, cpus: assigned.append((pid, set(cpus))),
    )

    _initialize_backtest_worker(cpu_queue)

    assert assigned == [(0, {7})]


def test_process_pool_initializer_pins_worker_to_assigned_cpu() -> None:
    if not hasattr(os, "sched_setaffinity") or not hasattr(os, "sched_getaffinity"):
        pytest.skip("CPU affinity controls are not available on this platform")

    available_cpu_ids = backtest_available_cpu_ids()
    if not available_cpu_ids:
        pytest.skip("No CPU ids available for affinity test")

    expected_cpu_id = available_cpu_ids[0]
    mp_context = backtest_worker_multiprocessing_context()
    cpu_id_queue = mp_context.Queue()
    cpu_id_queue.put(expected_cpu_id)

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=mp_context,
        initializer=_initialize_backtest_worker,
        initargs=(cpu_id_queue,),
    ) as executor:
        worker_state = executor.submit(_probe_backtest_worker_cpu).result(timeout=10)

    assert worker_state["assigned_cpu_id"] == expected_cpu_id
    assert worker_state["affinity"] == [expected_cpu_id]
