"""Focused tests for live trading engine follow-up and signal syncing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from triak_trade.domain.enums import EntryType, MarketType, SignalAction, SignalStatus, TradeSide
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.errors import ToobitAPIError
from triak_trade.exchange.toobit.futures import FuturesContractSpec
from triak_trade.live_trading.engine import LiveTradingEngine
from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveMessageTrace,
    LiveSession,
    LiveSignalSnapshot,
    LiveTrade,
    MessageAttribution,
)
from triak_trade.live_trading.position_manager import LivePositionManager
from triak_trade.live_trading.store import LiveTradingStore
from triak_trade.telegram.client import FakeTelegramClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT=500,
        SIGNAL_MAX_UPDATE_WINDOW_HOURS=48,
        LIVE_TRADING_FEE_RATE_PCT=Decimal("0.04"),
        LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE=10,
        LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE=50,
        LIVE_TRADING_MAX_CONCURRENT_POSITIONS=10,
        LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT=Decimal("120"),
        LIVE_TRADING_MIN_ALLOCATION_PCT=Decimal("2"),
        LIVE_TRADING_MAX_ALLOCATION_PCT=Decimal("20"),
        LIVE_TRADING_DEFAULT_STOP_PCT=Decimal("5"),
        LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT=Decimal("5"),
        LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS=8,
        LIVE_TRADING_CLOSE_RECONCILE_ATTEMPTS=3,
        LIVE_TRADING_PROTECTION_SYNC_RETRY_ATTEMPTS=3,
        LIVE_TRADING_PROTECTION_SYNC_RETRY_DELAY_SECONDS=0,
        LIVE_TRADING_EXCHANGE_POSITION_MISS_CONFIRMATIONS=2,
        LIVE_TRADING_EXCHANGE_POSITION_MISS_GRACE_SECONDS=15,
        LIVE_TRADING_REQUIRE_AI_CLASSIFIER=False,
        LIVE_TRADING_FAIL_CLOSED_ON_LEVERAGE_SYNC_ERROR=True,
        LIVE_TRADING_FAIL_CLOSED_ON_PROTECTION_SYNC_ERROR=True,
        REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH=True,
        LIVE_TRADING_PRICE_REFRESH_SECONDS=60,
        LIVE_TRADING_ACCOUNT_REFRESH_SECONDS=60,
        LIVE_TRADING_TELEGRAM_POLL_SECONDS=1,
        SIGNAL_CONSOLIDATION_SECONDS=1,
        AI_GATEWAY_ENABLED=False,
        AI_CLASSIFIER_ENABLED=False,
        LIVE_TRADING_USE_AI=False,
        KILL_SWITCH_ENABLED=False,
        KILL_SWITCH_REASON="",
    )


def _session() -> LiveSession:
    return LiveSession(
        session_id="ls_test",
        channels=["https://t.me/testchan"],
        channel_labels=["@testchan"],
        trading_mode="demo",
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=False,
        interval="1m",
    )


def _open_signal(action: SignalAction = SignalAction.OPEN) -> ParsedSignal:
    return ParsedSignal(
        action=action,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("50000"),
        entry_high=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profits=[Decimal("51000"), Decimal("52000")],
        leverage=10,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="@testchan",
        source_message_id=1,
        parser_version="test",
    )


def _message(message_id: int, text: str) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="@testchan",
        channel_username="testchan",
        message_id=message_id,
        text=text,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
        raw_payload={},
    )


def _state(parsed: ParsedSignal, status: SignalStatus = SignalStatus.OPEN) -> SignalState:
    now = datetime.now(timezone.utc)
    return SignalState(
        signal_id="sig_test",
        channel_id="@testchan",
        status=status,
        created_from_message_id=parsed.source_message_id,
        related_message_ids=[parsed.source_message_id],
        current_signal=parsed,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def _trade(session_id: str) -> LiveTrade:
    return LiveTrade(
        trade_id="trade_test",
        session_id=session_id,
        signal_id="sig_test",
        channel_id="@testchan",
        channel_input="https://t.me/testchan",
        channel_label="@testchan",
        symbol="BTCUSDT",
        side="long",
        leverage=10,
        entry_price=Decimal("50000"),
        quantity=Decimal("0.01"),
        stop_loss=Decimal("49000"),
        take_profits=[Decimal("51000"), Decimal("52000")],
        status="open",
    )


def _engine(tmp_path: Path) -> LiveTradingEngine:
    settings = _settings()
    session = _session()
    store = LiveTradingStore(tmp_path)
    engine = LiveTradingEngine(
        settings=settings,
        session=session,
        store=store,
        notifier=None,
    )
    engine._pm = LivePositionManager(settings)  # type: ignore[arg-type]
    engine._strategy = MagicMock()
    return engine


def _ignored_signal() -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.IGNORE,
        market=MarketType.FUTURES,
        symbol=None,
        side=TradeSide.UNKNOWN,
        entry_type=EntryType.UNKNOWN,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profits=[],
        leverage=None,
        confidence=Decimal("0.10"),
        invalid_reason=None,
        source_channel_id="@testchan",
        source_message_id=1,
        parser_version="test",
    )


@pytest.mark.asyncio
async def test_try_open_signal_rejects_invalid_exchange_symbol(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._futures_client = AsyncMock()
    engine._futures_client.validate_symbol_tradable.side_effect = ValueError("symbol not tradable")

    await engine._try_open_signal("sig_test", state, context)

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert signal is not None
    assert signal.status == "invalid"
    assert signal.status_group == "inactive"
    engine._futures_client.validate_symbol_tradable.assert_awaited_once_with(
        "BTCUSDT",
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_try_open_signal_opens_with_strategy_synthetic_protection_when_missing(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine.session.paper_balance = Decimal("1000")
    context = engine._get_or_create_context("@testchan")
    parsed = _open_signal().model_copy(update={"stop_loss": None, "take_profits": []})
    state = _state(parsed, status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._get_mark_price = AsyncMock(return_value=Decimal("50000"))  # type: ignore[method-assign]
    engine._strategy.get_synthetic_stop.return_value = Decimal("48765")
    engine._strategy.get_synthetic_take_profits.return_value = [
        Decimal("51000"),
        Decimal("52000"),
        Decimal("53000"),
    ]

    await engine._try_open_signal("sig_test", state, context)

    trade = engine._open_trades["sig_test"]
    snapshot = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.stop_loss == Decimal("48765")
    assert trade.take_profits == [Decimal("51000"), Decimal("52000"), Decimal("53000")]
    assert any(
        note.startswith("synthetic_stop_strategy=")
        for note in trade.message_history[-1].notes
    )
    assert any(
        note.startswith("synthetic_take_profits_strategy=")
        for note in trade.message_history[-1].notes
    )
    assert snapshot is not None
    assert snapshot.stop_loss == Decimal("48765")
    assert snapshot.take_profits == [Decimal("51000"), Decimal("52000"), Decimal("53000")]


@pytest.mark.asyncio
async def test_poll_messages_once_replays_messages_from_session_start(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._restore_runtime_state()
    message = _message(101, "noise").model_copy(
        update={"channel_id": "https://t.me/testchan", "channel_username": "testchan"}
    )
    engine._telegram_client = FakeTelegramClient(
        history_by_channel={"https://t.me/testchan": [message]}
    )
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )

    await engine._poll_messages_once()

    traces = engine.store.list_message_traces(engine.session.session_id, limit=10)
    assert len(traces) == 1
    assert traces[0].message_id == 101
    assert engine.session.total_messages_processed == 1
    assert engine._last_seen_message_ids["https://t.me/testchan"] == 101


@pytest.mark.asyncio
async def test_poll_messages_once_persists_heartbeat_without_new_messages(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._restore_runtime_state()
    engine._telegram_client = FakeTelegramClient(
        history_by_channel={"https://t.me/testchan": []}
    )
    before = engine.session.last_update_at

    await engine._poll_messages_once()

    persisted = engine.store.load_session(engine.session.session_id)
    assert persisted is not None
    assert persisted.last_update_at >= before
    assert engine.session.total_messages_processed == 0


@pytest.mark.asyncio
async def test_bootstrap_channel_cursor_uses_latest_message_id_for_live_followup(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._restore_runtime_state()
    latest_existing = _message(14782, "existing latest").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "date": datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        }
    )
    forwarded_after_start = _message(14783, "forwarded after start").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "date": datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        }
    )

    class BootstrapTelegramClient(FakeTelegramClient):
        async def fetch_history(
            self,
            channel: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
            min_message_id: int | None = None,
        ) -> list[RawTelegramMessage]:
            if limit == 1 and min_message_id is None:
                return [latest_existing]
            if min_message_id == 14783:
                return [forwarded_after_start]
            return []

    engine._telegram_client = BootstrapTelegramClient()
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )

    await engine._bootstrap_channel_cursors()
    await engine._poll_messages_once()

    assert engine._last_seen_message_ids["https://t.me/testchan"] == 14783
    traces = engine.store.list_message_traces(engine.session.session_id, limit=10)
    assert len(traces) == 1
    assert traces[0].message_id == 14783
    assert engine.session.total_messages_processed == 1


@pytest.mark.asyncio
async def test_prepare_message_for_classification_downloads_caption_media(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=200,
        channel_id="https://t.me/testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    message = _message(200, "caption text").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "raw_payload": {
                "has_media": True,
                "caption_present": True,
                "image_data_urls": [],
            },
        }
    )

    class HydratingTelegramClient(FakeTelegramClient):
        async def ensure_media_payload(
            self,
            message: RawTelegramMessage,
            *,
            allow_captionless: bool = False,
        ) -> RawTelegramMessage:
            return message.model_copy(
                update={
                    "raw_payload": {
                        **message.raw_payload,
                        "media_downloaded": True,
                        "image_data_urls": [
                            {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,abc"}
                        ],
                    }
                }
            )

    engine._telegram_client = HydratingTelegramClient()

    prepared = await engine._prepare_message_for_classification(
        message=message,
        context=engine._get_or_create_context("https://t.me/testchan"),
        trace=trace,
    )

    assert len(prepared.raw_payload["image_data_urls"]) == 1
    assert "caption_media_downloaded=1" in trace.debug_notes


@pytest.mark.asyncio
async def test_prepare_message_for_classification_attaches_recent_captionless_media_context(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("https://t.me/testchan")
    prior_media = _message(201, "").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "raw_payload": {
                "has_media": True,
                "caption_present": False,
                "image_data_urls": [],
            },
        }
    )
    context.add_recent_message(prior_media)
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=202,
        channel_id="https://t.me/testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    followup = _message(202, "tp 100 200").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
        }
    )

    class RetroTelegramClient(FakeTelegramClient):
        async def ensure_media_payload(
            self,
            message: RawTelegramMessage,
            *,
            allow_captionless: bool = False,
        ) -> RawTelegramMessage:
            assert allow_captionless is True
            return message.model_copy(
                update={
                    "raw_payload": {
                        **message.raw_payload,
                        "media_downloaded": True,
                        "image_data_urls": [
                            {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,retro"}
                        ],
                    }
                }
            )

    engine._telegram_client = RetroTelegramClient()

    prepared = await engine._prepare_message_for_classification(
        message=followup,
        context=context,
        trace=trace,
    )

    assert prepared.raw_payload["context_image_message_ids"] == [201]
    assert len(prepared.raw_payload["image_data_urls"]) == 1
    assert "retro_media_context_from=201" in trace.debug_notes


@pytest.mark.asyncio
async def test_poll_messages_once_only_processes_messages_newer_than_last_seen(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    older = _message(101, "older")
    older = older.model_copy(
        update={"channel_id": "https://t.me/testchan", "channel_username": "testchan"}
    )
    newer = _message(102, "newer").model_copy(
        update={"channel_id": "https://t.me/testchan", "channel_username": "testchan"}
    )
    engine.store.save_message_trace(
        engine.session.session_id,
        LiveMessageTrace(
            session_id=engine.session.session_id,
            message_id=101,
            channel_id="https://t.me/testchan",
            channel_label="@testchan",
            message_date=older.date,
            final_status="ignored",
        ),
    )
    engine._restore_runtime_state()
    engine._telegram_client = FakeTelegramClient(
        history_by_channel={"https://t.me/testchan": [older, newer]}
    )
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )

    await engine._poll_messages_once()

    traces = engine.store.list_message_traces(engine.session.session_id, limit=10)
    assert [trace.message_id for trace in traces] == [102, 101]
    assert engine.session.total_messages_processed == 1
    assert engine._last_seen_message_ids["https://t.me/testchan"] == 102


@pytest.mark.asyncio
async def test_try_open_signal_normalizes_bare_base_symbol_before_exchange_validation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    parsed = _open_signal().model_copy(update={"symbol": "BTC"})
    state = _state(parsed, status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._futures_client = AsyncMock()
    engine._futures_client.validate_symbol_tradable.return_value = SimpleNamespace(
        symbol="TBV_BTC-SWAP-TBV_USDT"
    )
    engine._get_mark_price = AsyncMock(return_value=Decimal("50000"))  # type: ignore[method-assign]
    engine._open_position = AsyncMock()  # type: ignore[method-assign]

    await engine._try_open_signal("sig_test", state, context)

    engine._futures_client.validate_symbol_tradable.assert_awaited_once_with(
        "BTCUSDT",
        use_demo_symbol=True,
    )
    engine._open_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_open_signal_treats_missing_entry_as_market_and_uses_mark_price(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    parsed = _open_signal().model_copy(
        update={
            "entry_type": EntryType.UNKNOWN,
            "entry_low": None,
            "entry_high": None,
        }
    )
    state = _state(parsed, status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._get_mark_price = AsyncMock(return_value=Decimal("50123"))  # type: ignore[method-assign]
    engine._open_position = AsyncMock()  # type: ignore[method-assign]

    await engine._try_open_signal("sig_test", state, context)

    opened = engine._open_position.await_args.kwargs["parsed"]
    assert opened.entry_type is EntryType.MARKET
    assert opened.entry_low == Decimal("50123")
    assert opened.entry_high == Decimal("50123")


@pytest.mark.asyncio
async def test_try_open_signal_rejects_inconsistent_stop_geometry_before_open(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    parsed = _open_signal(action=SignalAction.UPDATE_ENTRY).model_copy(
        update={
            "entry_type": EntryType.MARKET,
            "entry_low": None,
            "entry_high": None,
            "stop_loss": Decimal("51000"),
            "take_profits": [Decimal("52000")],
        }
    )
    state = _state(parsed, status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._get_mark_price = AsyncMock(return_value=Decimal("50000"))  # type: ignore[method-assign]
    engine._open_position = AsyncMock()  # type: ignore[method-assign]

    await engine._try_open_signal("sig_test", state, context)

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert signal is not None
    assert signal.status == "invalid"
    engine._open_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_open_position_uses_demo_exchange_symbol_for_demo_sessions(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    engine._futures_client = AsyncMock()
    engine._futures_client.open_long.return_value = SimpleNamespace(
        order_id="ord_demo",
        exchange_symbol="TBV_BTC-SWAP-TBV_USDT",
        avg_price=Decimal("50100"),
        executed_qty=Decimal("0.01"),
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="ord_demo",
            exchange_symbol="TBV_BTC-SWAP-TBV_USDT",
            status="FILLED",
            executed_qty=Decimal("10"),
            avg_price=Decimal("50100"),
        ),
        [],
    )
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "TBV_BTC-SWAP-TBV_USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "1000000",
                    "stepSize": "0.0001",
                }
            ],
        }
    )
    engine._futures_client.set_trading_stop.return_value = {
        "symbol": "TBV_BTC-SWAP-TBV_USDT",
        "side": "LONG",
        "stopLoss": "49000",
        "slSize": "10",
    }
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("49000"),
        [Decimal("51000"), Decimal("52000")],
    )
    def _target_hit_action(**kwargs: object) -> SimpleNamespace:
        remaining = int(kwargs["remaining_targets_including_this"])
        close_fraction = Decimal("1") if remaining <= 1 else Decimal("0.35")
        return SimpleNamespace(
            close_fraction=close_fraction,
            move_sl_to_entry=False,
            new_stop_loss=None,
        )

    engine._strategy.get_target_hit_action.side_effect = _target_hit_action
    engine._futures_client.place_order.side_effect = [
        SimpleNamespace(order_id="tp_protect_1"),
        SimpleNamespace(order_id="tp_protect_2"),
    ]
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_protect_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_protect_2",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_2",
                stop_price=Decimal("0"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_protect",
                order_type="STOP_LONG_LOSS",
                position_side="LONG",
                side="SELL_CLOSE",
                stop_price=Decimal("49000"),
            ),
        ],
    ]
    engine._futures_client.cancel_order.return_value = {"code": 200}

    await engine._real_open_position(trade)

    assert trade.exchange_symbol == "TBV_BTC-SWAP-TBV_USDT"
    assert trade.entry_order_id == "ord_demo"
    assert trade.entry_price == Decimal("50100")
    engine._futures_client.set_leverage.assert_awaited_once_with(
        "BTCUSDT",
        10,
        use_demo_symbol=True,
    )
    engine._futures_client.open_long.assert_awaited_once_with(
        symbol="BTCUSDT",
        quantity=Decimal("0.01"),
        leverage=10,
        use_demo_symbol=True,
    )
    engine._futures_client.wait_for_order_fill.assert_awaited_once()
    assert engine._futures_client.wait_for_order_fill.await_args.kwargs == {
        "symbol": "BTCUSDT",
        "order_id": "ord_demo",
        "use_demo_symbol": True,
        "timeout_seconds": 8.0,
    }
    assert engine._futures_client.place_order.await_count == 2
    engine._futures_client.set_trading_stop.assert_awaited_once()
    assert trade.tp_order_ids == ["tp_protect_1", "tp_protect_2"]
    assert trade.sl_order_id == "sl_protect"


@pytest.mark.asyncio
async def test_real_open_position_clamps_leverage_and_quantity_to_exchange_risk_limit(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.symbol = "DOGEUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("0.073")
    trade.quantity = Decimal("25000000")
    trade.remaining_quantity = Decimal("25000000")
    trade.leverage = 25
    trade.margin = Decimal("73000")
    trade.take_profits = [Decimal("0.07")]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="doge short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "DOGE-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "63",
                    "maxQty": "30000000",
                    "stepSize": "1",
                }
            ],
            "riskLimits": [
                {"quantity": "21442439", "value": "1568300", "maxLeverage": "25"},
                {"quantity": "42884878", "value": "3136600", "maxLeverage": "20"},
            ],
        }
    )
    engine._futures_client.open_short.return_value = SimpleNamespace(
        order_id="ord_doge",
        exchange_symbol="TBV_DOGE-SWAP-TBV_USDT",
        avg_price=Decimal("0.073"),
        executed_qty=Decimal("20000000"),
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="ord_doge",
            exchange_symbol="TBV_DOGE-SWAP-TBV_USDT",
            status="FILLED",
            executed_qty=Decimal("20000000"),
            avg_price=Decimal("0.073"),
        ),
        [],
    )
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("0.08"),
        [Decimal("0.07")],
    )
    engine._futures_client.place_order.return_value = SimpleNamespace(order_id="tp_doge")
    engine._futures_client.set_trading_stop.return_value = {"ok": True}
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_doge",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_1",
                stop_price=Decimal("0"),
            )
        ],
        [
            SimpleNamespace(
                order_id="sl_doge",
                order_type="STOP_SHORT_LOSS",
                position_side="SHORT",
                side="BUY_CLOSE",
                stop_price=Decimal("0.08"),
            )
        ],
    ]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._strategy.get_target_hit_action.side_effect = lambda **_: SimpleNamespace(
        close_fraction=Decimal("1"),
        move_sl_to_entry=False,
        new_stop_loss=None,
    )

    await engine._real_open_position(trade)

    assert trade.leverage == 20
    assert trade.quantity == Decimal("20000000")
    assert trade.margin == Decimal("73000.00000000")
    engine._futures_client.set_leverage.assert_awaited_once_with(
        "DOGEUSDT",
        20,
        use_demo_symbol=True,
    )
    engine._futures_client.open_short.assert_awaited_once_with(
        symbol="DOGEUSDT",
        quantity=Decimal("20000000"),
        leverage=20,
        use_demo_symbol=True,
    )
    assert "exchange_leverage_clamped=25x->20x" in trade.message_history[-1].notes[-1]


@pytest.mark.asyncio
async def test_real_open_position_falls_back_when_exchange_rejects_target_leverage(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.symbol = "DOGEUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("0.07318")
    trade.quantity = Decimal("1476566.21424335")
    trade.remaining_quantity = Decimal("1476566.21424335")
    trade.leverage = 50
    trade.margin = Decimal("2161.10231117")
    trade.take_profits = [Decimal("0.07172")]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="doge short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "DOGE-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "63",
                    "maxQty": "30000000",
                    "stepSize": "1",
                }
            ],
            "riskLimits": [
                {"quantity": "427912", "value": "31366", "maxLeverage": "100"},
                {"quantity": "893274", "value": "65477", "maxLeverage": "75"},
                {"quantity": "1791882", "value": "131345", "maxLeverage": "50"},
                {"quantity": "7595457", "value": "556747", "maxLeverage": "40"},
                {"quantity": "21395634", "value": "1568300", "maxLeverage": "25"},
            ],
        }
    )
    engine._futures_client.set_leverage.side_effect = [
        ValueError("Toobit API HTTP error: 400: Position size cannot meet target leverage"),
        ValueError("Toobit API HTTP error: 400: Position size cannot meet target leverage"),
        {"code": 200, "symbolId": "TBV_DOGE-SWAP-TBV_USDT", "leverage": "25"},
    ]
    engine._futures_client.open_short.return_value = SimpleNamespace(
        order_id="ord_doge_fb",
        exchange_symbol="TBV_DOGE-SWAP-TBV_USDT",
        avg_price=Decimal("0.07318"),
        executed_qty=Decimal("738283"),
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="ord_doge_fb",
            exchange_symbol="TBV_DOGE-SWAP-TBV_USDT",
            status="FILLED",
            executed_qty=Decimal("738283"),
            avg_price=Decimal("0.07318"),
        ),
        [],
    )
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("0.07617"),
        [Decimal("0.07172")],
    )
    engine._futures_client.place_order.return_value = SimpleNamespace(order_id="tp_doge_fb")
    engine._futures_client.set_trading_stop.return_value = {"ok": True}
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_doge_fb",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_1",
                stop_price=Decimal("0"),
            )
        ],
        [
            SimpleNamespace(
                order_id="sl_doge_fb",
                order_type="STOP_SHORT_LOSS",
                position_side="SHORT",
                side="BUY_CLOSE",
                stop_price=Decimal("0.07617"),
            )
        ],
    ]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._strategy.get_target_hit_action.side_effect = lambda **_: SimpleNamespace(
        close_fraction=Decimal("1"),
        move_sl_to_entry=False,
        new_stop_loss=None,
    )

    await engine._real_open_position(trade)

    assert trade.leverage == 25
    assert trade.quantity == Decimal("738283")
    assert trade.margin == Decimal("2161.10199760")
    assert engine._futures_client.set_leverage.await_args_list[0].args == ("DOGEUSDT", 50)
    assert engine._futures_client.set_leverage.await_args_list[1].args == ("DOGEUSDT", 40)
    assert engine._futures_client.set_leverage.await_args_list[2].args == ("DOGEUSDT", 25)
    engine._futures_client.open_short.assert_awaited_once_with(
        symbol="DOGEUSDT",
        quantity=Decimal("738283"),
        leverage=25,
        use_demo_symbol=True,
    )
    assert any(
        "exchange_leverage_fallback=50x->25x" in note
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_real_open_position_promotes_small_quantity_to_support_five_targets(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("100"),
        available_balance=Decimal("100"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "BTCUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("60118.3")
    trade.quantity = Decimal("0.0001")
    trade.remaining_quantity = Decimal("0.0001")
    trade.leverage = 85
    trade.margin = Decimal("0.07072741")
    trade.stop_loss = Decimal("65106.8")
    trade.take_profits = [
        Decimal("58915.934"),
        Decimal("57713.568"),
        Decimal("56511.202"),
        Decimal("55308.836"),
        Decimal("54106.470"),
    ]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="btc short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "120",
                    "stepSize": "0.0001",
                }
            ],
        }
    )
    engine._futures_client.set_leverage.return_value = {"code": 200}
    engine._futures_client.open_short.return_value = SimpleNamespace(
        order_id="ord_btc_5tp",
        exchange_symbol="TBV_BTC-SWAP-TBV_USDT",
        avg_price=Decimal("60118.3"),
        executed_qty=Decimal("10"),
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="ord_btc_5tp",
            exchange_symbol="TBV_BTC-SWAP-TBV_USDT",
            status="FILLED",
            executed_qty=Decimal("10"),
            avg_price=Decimal("60118.3"),
        ),
        [],
    )
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("65106.8"),
        [
            Decimal("58915.9"),
            Decimal("57713.6"),
            Decimal("56511.2"),
            Decimal("55308.8"),
            Decimal("54106.4"),
        ],
    )
    def _target_hit_action(**kwargs: object) -> SimpleNamespace:
        targets_hit = int(kwargs["targets_hit_so_far"])
        remaining = int(kwargs["remaining_targets_including_this"])
        fractions = [
            Decimal("0.35"),
            Decimal("0.40"),
            Decimal("0.50"),
            Decimal("0.50"),
        ]
        if remaining <= 1:
            close_fraction = Decimal("1")
        else:
            close_fraction = fractions[min(targets_hit, len(fractions) - 1)]
        return SimpleNamespace(
            close_fraction=close_fraction,
            move_sl_to_entry=False,
            new_stop_loss=None,
        )

    engine._strategy.get_target_hit_action.side_effect = _target_hit_action
    engine._futures_client.place_order.side_effect = [
        SimpleNamespace(order_id="tp_btc_1"),
        SimpleNamespace(order_id="tp_btc_2"),
        SimpleNamespace(order_id="tp_btc_3"),
        SimpleNamespace(order_id="tp_btc_4"),
        SimpleNamespace(order_id="tp_btc_5"),
    ]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_btc_1",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_1",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_btc_2",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_2",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_btc_3",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_3",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_btc_4",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_4",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_btc_5",
                order_type="LIMIT",
                side="BUY_CLOSE",
                client_order_id="triak_tp_trade_test_5",
                stop_price=Decimal("0"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_btc_5tp",
                order_type="STOP_SHORT_LOSS",
                position_side="SHORT",
                side="BUY_CLOSE",
                stop_price=Decimal("65106.8"),
            )
        ],
    ]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]

    await engine._real_open_position(trade)

    engine._futures_client.open_short.assert_awaited_once_with(
        symbol="BTCUSDT",
        quantity=Decimal("0.01000000"),
        leverage=85,
        use_demo_symbol=True,
    )
    assert trade.quantity == Decimal("0.01000000")
    assert trade.tp_order_ids == [
        "tp_btc_1",
        "tp_btc_2",
        "tp_btc_3",
        "tp_btc_4",
        "tp_btc_5",
    ]
    assert any(
        note == "exchange_entry_quantity_promoted=0.0001->0.00100000"
        for note in trade.message_history[-1].notes
    )
    assert any(
        note == "exchange_tp_ladder_quantity_promoted=0.00100000->0.01000000"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_sync_trade_protection_normalizes_trade_levels_before_exchange_submit(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.OPEN)
    trade = _trade(engine.session.session_id)
    trade.symbol = "DOGEUSDT"
    trade.stop_loss = Decimal("0.07656495001999200967763434522")
    trade.take_profits = [
        Decimal("0.07208880"),
        Decimal("0.07061760"),
    ]
    context.add_signal(state, pending=False)
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("0.07656"),
        [Decimal("0.07209"), Decimal("0.07062")],
    )
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(
            close_fraction=Decimal("0.35"),
            move_sl_to_entry=False,
            new_stop_loss=None,
        ),
        SimpleNamespace(
            close_fraction=Decimal("1"),
            move_sl_to_entry=False,
            new_stop_loss=None,
        ),
    ]
    engine._futures_client.place_order.side_effect = [
        SimpleNamespace(order_id="tp_doge_1"),
        SimpleNamespace(order_id="tp_doge_2"),
    ]
    engine._futures_client.get_open_orders.side_effect = [[], [], [], []]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}

    await engine._sync_trade_protection(trade)

    assert trade.stop_loss == Decimal("0.07656")
    assert trade.take_profits == [Decimal("0.07209"), Decimal("0.07062")]
    engine._futures_client.normalize_trade_protection.assert_awaited_once_with(
        symbol="DOGEUSDT",
        side="LONG",
        stop_loss=Decimal("0.07656495001999200967763434522"),
        take_profits=[Decimal("0.07208880"), Decimal("0.07061760")],
        use_demo_symbol=True,
    )
    submitted_prices = [
        call.kwargs["price"]
        for call in engine._futures_client.place_order.await_args_list
    ]
    assert submitted_prices == [Decimal("0.07209"), Decimal("0.07062")]
    engine._futures_client.set_trading_stop.assert_awaited_once_with(
        symbol="DOGEUSDT",
        side="LONG",
        stop_loss=Decimal("0.07656"),
        sl_quantity=trade.remaining_quantity,
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_sync_trade_protection_update_sl_only_preserves_existing_take_profit_orders(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.symbol = "DOGEUSDT"
    trade.stop_loss = Decimal("0.07656")
    trade.take_profits = [Decimal("0.07209"), Decimal("0.07062")]
    trade.tp_order_ids = ["tp_live_1", "tp_live_2"]
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("0.09"),
        [Decimal("0.07209"), Decimal("0.07062")],
    )
    engine._futures_client.get_open_orders.side_effect = [
        [
            SimpleNamespace(
                order_id="sl_live_old",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("0.07656"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="tp_live_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_live_2",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_2_existing",
                stop_price=Decimal("0"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_live_new",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("0.09"),
            ),
        ],
    ]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}

    await engine._sync_trade_protection(
        trade,
        refresh_take_profits=False,
        refresh_stop_loss=True,
    )

    engine._futures_client.place_order.assert_not_awaited()
    engine._futures_client.cancel_order.assert_awaited_once_with(
        symbol="DOGEUSDT",
        order_id="sl_live_old",
        order_type="STOP",
        use_demo_symbol=True,
    )
    assert trade.tp_order_ids == ["tp_live_1", "tp_live_2"]
    assert trade.sl_order_id == "sl_live_new"
    engine._futures_client.set_trading_stop.assert_awaited_once_with(
        symbol="DOGEUSDT",
        side="LONG",
        stop_loss=Decimal("0.09"),
        sl_quantity=trade.remaining_quantity,
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_sync_trade_protection_retries_without_quantity_on_position_limit(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.symbol = "DOGEUSDT"
    trade.stop_loss = Decimal("0.07656")
    trade.take_profits = []
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="doge long",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("0.07656"),
        [],
    )
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="sl_live_new",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("0.07656"),
            ),
        ],
    ]
    engine._futures_client.set_trading_stop.side_effect = [
        ToobitAPIError("Toobit API HTTP error: 400: stopProfitLoss order position limit"),
        {"ok": True},
    ]

    await engine._sync_trade_protection(
        trade,
        refresh_take_profits=False,
        refresh_stop_loss=True,
    )

    assert trade.sl_order_id == "sl_live_new"
    assert trade.message_history
    assert any(
        note == "exchange_sl_size_fallback=omit_quantity_after_position_limit"
        for note in trade.message_history[-1].notes
    )
    assert engine._futures_client.set_trading_stop.await_args_list[0].kwargs == {
        "symbol": "DOGEUSDT",
        "side": "LONG",
        "stop_loss": Decimal("0.07656"),
        "sl_quantity": trade.remaining_quantity,
        "use_demo_symbol": True,
    }
    assert engine._futures_client.set_trading_stop.await_args_list[1].kwargs == {
        "symbol": "DOGEUSDT",
        "side": "LONG",
        "stop_loss": Decimal("0.07656"),
        "use_demo_symbol": True,
    }


@pytest.mark.asyncio
async def test_sync_trade_protection_compresses_tiny_tp_ladder_to_valid_exchange_order(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.symbol = "BTCUSDT"
    trade.remaining_quantity = Decimal("0.0001")
    trade.stop_loss = Decimal("61665")
    trade.take_profits = [
        Decimal("58850"),
        Decimal("58200"),
        Decimal("56980"),
        Decimal("54600"),
    ]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="btc short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("61665"),
        [Decimal("58850"), Decimal("58200"), Decimal("56980"), Decimal("54600")],
    )
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.0001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "1000000",
                    "stepSize": "0.0001",
                }
            ],
        }
    )
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(close_fraction=Decimal("0.35"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.40"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.50"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("1"), move_sl_to_entry=False, new_stop_loss=None),
    ]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}
    engine._futures_client.place_order.return_value = SimpleNamespace(order_id="tp_btc_1")
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_btc_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_btc_1",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("61665"),
            ),
        ],
    ]

    await engine._sync_trade_protection(trade)

    assert engine._futures_client.place_order.await_count == 1
    assert engine._futures_client.place_order.await_args.kwargs["price"] == Decimal("58850")
    assert engine._futures_client.place_order.await_args.kwargs["quantity"] == Decimal("0.00010000")
    assert any(
        note == "exchange_tp_ladder_compressed=4->1_due_to_contract_size"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_reconcile_exchange_trade_protection_applies_tp_fill_and_rearms_next_stop(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.remaining_quantity = Decimal("0.01")
    trade.tp_order_ids = ["tp1"]
    trade.sl_order_id = "sl1"
    trade.take_profits = [Decimal("51000"), Decimal("52000")]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_order.return_value = SimpleNamespace(
        order_id="tp1",
        executed_order_id="close_tp1",
        order_type="STOP_LONG_PROFIT",
        status="ORDER_FILLED",
    )
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("0.001")
    )
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(
            close_fraction=Decimal("0.35"),
            move_sl_to_entry=True,
            new_stop_loss=None,
        ),
        SimpleNamespace(
            close_fraction=Decimal("1"),
            move_sl_to_entry=False,
            new_stop_loss=None,
        ),
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[],
        open_protection_orders=[],
        symbol_user_trades=[
            SimpleNamespace(
                order_id="close_tp1",
                qty=Decimal("3.5"),
                realized_pnl=Decimal("8.25"),
                commission=Decimal("0.12"),
                price=Decimal("51000"),
            )
        ],
    )

    assert trade.realized_pnl == Decimal("8.25")
    assert trade.fees == Decimal("0.12")
    assert trade.remaining_quantity == Decimal("0.0065")
    assert trade.targets_hit == 1
    assert trade.stop_loss == Decimal("50000")
    assert trade.tp_order_ids == []
    assert trade.sl_order_id == "sl1"
    engine._sync_trade_protection.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_cancel_existing_trade_protection_discovers_exchange_tp_orders_when_state_is_empty(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.tp_order_ids = []
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_orders.side_effect = [
        [
            SimpleNamespace(
                order_id="tp_live_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1",
            ),
            SimpleNamespace(
                order_id="other_order",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="different_prefix",
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_live_1",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
            )
        ],
    ]

    await engine._cancel_existing_trade_protection(trade)

    cancel_calls = engine._futures_client.cancel_order.await_args_list
    assert [call.kwargs["order_id"] for call in cancel_calls] == ["tp_live_1", "sl_live_1"]
    assert cancel_calls[1].kwargs["order_type"] == "STOP"


def test_exchange_take_profit_orders_builds_partial_ladder_from_strategy() -> None:
    engine = _engine(Path("/tmp"))
    trade = _trade(engine.session.session_id)
    trade.remaining_quantity = Decimal("1")
    trade.take_profits = [
        Decimal("51000"),
        Decimal("52000"),
        Decimal("53000"),
        Decimal("54000"),
    ]
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(close_fraction=Decimal("0.35"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.40"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.50"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("1"), move_sl_to_entry=False, new_stop_loss=None),
    ]

    orders = engine._exchange_take_profit_orders(trade)

    assert orders == [
        (0, Decimal("51000"), Decimal("0.35000000")),
        (1, Decimal("52000"), Decimal("0.26000000")),
        (2, Decimal("53000"), Decimal("0.19500000")),
        (3, Decimal("54000"), Decimal("0.19500000")),
    ]


def test_exchange_take_profit_orders_compresses_small_contract_position_to_valid_orders() -> None:
    engine = _engine(Path("/tmp"))
    trade = _trade(engine.session.session_id)
    trade.symbol = "BTCUSDT"
    trade.remaining_quantity = Decimal("0.0001")
    trade.take_profits = [
        Decimal("58850"),
        Decimal("58200"),
        Decimal("56980"),
        Decimal("54600"),
    ]
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(close_fraction=Decimal("0.35"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.40"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("0.50"), move_sl_to_entry=False, new_stop_loss=None),
        SimpleNamespace(close_fraction=Decimal("1"), move_sl_to_entry=False, new_stop_loss=None),
    ]
    spec = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.0001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "1000000",
                    "stepSize": "0.0001",
                }
            ],
        }
    )

    orders = engine._exchange_take_profit_orders(trade, spec=spec)

    assert orders == [
        (0, Decimal("58850"), Decimal("0.00010000")),
    ]


def test_ensure_exchange_quantity_supports_take_profit_ladder_promotes_open_quantity() -> None:
    engine = _engine(Path("/tmp"))
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("100"),
        available_balance=Decimal("100"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "BTCUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("60118.3")
    trade.leverage = 85
    trade.quantity = Decimal("0.0001")
    trade.remaining_quantity = Decimal("0.0001")
    trade.margin = Decimal("0.07072741")
    trade.take_profits = [
        Decimal("58915.934"),
        Decimal("57713.568"),
        Decimal("56511.202"),
        Decimal("55308.836"),
        Decimal("54106.470"),
    ]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="btc short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    def _target_hit_action(**kwargs: object) -> SimpleNamespace:
        targets_hit = int(kwargs["targets_hit_so_far"])
        remaining = int(kwargs["remaining_targets_including_this"])
        fractions = [
            Decimal("0.35"),
            Decimal("0.40"),
            Decimal("0.50"),
            Decimal("0.50"),
        ]
        if remaining <= 1:
            close_fraction = Decimal("1")
        else:
            close_fraction = fractions[min(targets_hit, len(fractions) - 1)]
        return SimpleNamespace(
            close_fraction=close_fraction,
            move_sl_to_entry=False,
            new_stop_loss=None,
        )

    engine._strategy.get_target_hit_action.side_effect = _target_hit_action
    spec = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "120",
                    "stepSize": "0.0001",
                }
            ],
        }
    )

    engine._ensure_exchange_quantity_supports_take_profit_ladder(trade, spec)

    assert trade.quantity == Decimal("0.01000000")
    assert trade.remaining_quantity == Decimal("0.01000000")
    assert trade.margin == Decimal("7.07274118")
    assert any(
        note == "exchange_tp_ladder_quantity_promoted=0.0001->0.01000000"
        for note in trade.message_history[-1].notes
    )


def test_ensure_exchange_quantity_supports_market_entry_promotes_open_quantity() -> None:
    engine = _engine(Path("/tmp"))
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("10"),
        available_balance=Decimal("10"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "BTCUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("60118.3")
    trade.leverage = 85
    trade.quantity = Decimal("0.0001")
    trade.remaining_quantity = Decimal("0.0001")
    trade.margin = Decimal("0.07072741")
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="btc short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    spec = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.001",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "120",
                    "stepSize": "0.0001",
                }
            ],
        }
    )

    engine._ensure_exchange_quantity_supports_market_entry(trade, spec)

    assert trade.quantity == Decimal("0.00100000")
    assert trade.remaining_quantity == Decimal("0.00100000")
    assert trade.margin == Decimal("0.70727412")
    assert any(
        note == "exchange_entry_quantity_promoted=0.0001->0.00100000"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_refresh_trade_protection_ids_ignores_legacy_stop_profit_orders_for_tp_tracking(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_orders.side_effect = [
        [
            SimpleNamespace(
                order_id="tp_limit_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1",
                stop_price=Decimal("0"),
            ),
            SimpleNamespace(
                order_id="tp_limit_2",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_2",
                stop_price=Decimal("0"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="legacy_tp_stop",
                order_type="STOP_LONG_PROFIT",
                side="SELL_CLOSE",
                stop_price=Decimal("51000"),
            ),
            SimpleNamespace(
                order_id="sl_stop",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("49000"),
            ),
        ],
    ]

    await engine._refresh_trade_protection_ids(trade)

    assert trade.tp_order_ids == ["tp_limit_1", "tp_limit_2"]
    assert trade.sl_order_id == "sl_stop"


@pytest.mark.asyncio
async def test_refresh_account_uses_demo_account_context_and_updates_balance(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_full_account_info.return_value = SimpleNamespace(
        total_wallet_balance=Decimal("100000"),
        available_balance=Decimal("99900"),
        total_unrealized_profit=Decimal("5"),
        total_position_margin=Decimal("100"),
        user_id="136913243",
    )
    engine._sync_exchange_state = AsyncMock()  # type: ignore[method-assign]

    await engine._refresh_account()

    engine._futures_client.get_full_account_info.assert_awaited_once_with(use_demo_account=True)
    assert engine.session.account_info is not None
    assert engine.session.account_info.wallet_balance == Decimal("100000")
    assert engine.session.account_info.available_balance == Decimal("99900")
    assert engine.session.account_info.unrealized_pnl == Decimal("5")
    assert engine.session.account_info.margin_balance == Decimal("100000")
    assert engine.session.account_info.total_position_margin == Decimal("100")
    assert engine.session.account_info.max_withdraw == Decimal("99900")
    assert engine.session.paper_balance == Decimal("99900")
    assert engine.session.paper_initial_balance == Decimal("100000")


@pytest.mark.asyncio
async def test_apply_tp_hit_promotes_stop_to_previous_target_without_double_count(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.targets_hit = 1
    trade.remaining_quantity = trade.quantity
    trade.take_profits = [Decimal("51000"), Decimal("52000"), Decimal("53000")]
    engine._strategy = MagicMock()
    engine._strategy.get_target_hit_action.return_value = SimpleNamespace(
        close_fraction=Decimal("0.40"),
        move_sl_to_entry=False,
        new_stop_loss=Decimal("51000"),
    )
    engine._uses_exchange_execution = MagicMock(return_value=False)  # type: ignore[method-assign]

    await engine._apply_tp_hit(trade, Decimal("52000"), "tp2_hit")

    assert trade.targets_hit == 2
    assert trade.stop_loss == Decimal("51000")


@pytest.mark.asyncio
async def test_handle_followup_updates_leverage_and_signal_snapshot(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)

    parsed = _open_signal(action=SignalAction.UPDATE_LEVERAGE).model_copy(update={"leverage": 25})
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=2,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(2, "lev 25"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.leverage == 25
    assert trace.final_status == "updated_leverage"
    assert signal is not None
    assert signal.leverage == 25


@pytest.mark.asyncio
async def test_handle_followup_updates_take_profits_and_signal_snapshot(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    trade.stop_loss = Decimal("48765")
    trade.take_profits = [Decimal("51000"), Decimal("52000")]
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)

    parsed = _open_signal(action=SignalAction.UPDATE_TP).model_copy(
        update={"take_profits": [Decimal("51100"), Decimal("52200"), Decimal("53300")]}
    )
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=22,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(22, "tp 51100 52200 53300"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trace.final_status == "updated_tp"
    assert trade.take_profits == [Decimal("51100"), Decimal("52200"), Decimal("53300")]
    assert signal is not None
    assert signal.take_profits == [Decimal("51100"), Decimal("52200"), Decimal("53300")]


@pytest.mark.asyncio
async def test_handle_followup_update_tp_also_updates_stop_loss_when_present(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    trade.side = "short"
    trade.entry_price = Decimal("60118.3")
    trade.stop_loss = Decimal("65106.8")
    trade.take_profits = [Decimal("59000")]
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = [
        SimpleNamespace(
            symbol_internal="BTCUSDT",
            side="LONG",
            position=Decimal("1"),
        )
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=23,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    parsed = _open_signal(action=SignalAction.UPDATE_TP).model_copy(
        update={
            "stop_loss": Decimal("61665"),
            "side": TradeSide.SHORT,
            "entry_low": Decimal("60118.3"),
            "entry_high": Decimal("60118.3"),
            "take_profits": [
                Decimal("58850"),
                Decimal("58200"),
                Decimal("56980"),
                Decimal("54600"),
            ],
        }
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(23, "targets updated stop 61665"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trace.final_status == "updated_tp"
    assert trade.stop_loss == Decimal("61665")
    assert trade.take_profits == [
        Decimal("58850"),
        Decimal("58200"),
        Decimal("56980"),
        Decimal("54600"),
    ]
    assert signal is not None
    assert signal.stop_loss == Decimal("61665")
    assert signal.take_profits == [
        Decimal("58850"),
        Decimal("58200"),
        Decimal("56980"),
        Decimal("54600"),
    ]
    engine._sync_trade_protection.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_handle_followup_update_entry_refreshes_live_trade_parameters(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = [
        SimpleNamespace(
            symbol_internal="BTCUSDT",
            side="LONG",
            position=Decimal("10"),
        )
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=6,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    parsed = _open_signal(action=SignalAction.UPDATE_ENTRY).model_copy(
        update={
            "entry_type": EntryType.MARKET,
            "entry_low": None,
            "entry_high": None,
            "stop_loss": Decimal("49500"),
            "take_profits": [Decimal("51100"), Decimal("52200")],
            "leverage": 15,
        }
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(6, "open replay with sl tp lev"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trace.final_status == "updated_entry"
    assert trade.stop_loss == Decimal("49500")
    assert trade.take_profits == [Decimal("51100"), Decimal("52200")]
    assert trade.leverage == 15
    assert signal is not None
    assert signal.stop_loss == Decimal("49500")
    assert signal.take_profits == [Decimal("51100"), Decimal("52200")]
    engine._sync_trade_protection.assert_awaited_once_with(trade)
    engine._futures_client.set_leverage.assert_awaited_once_with(
        "BTCUSDT",
        15,
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_handle_followup_update_sl_rolls_back_local_stop_on_exchange_failure(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    engine._futures_client = AsyncMock()
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._ensure_trade_still_open_on_exchange = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ValueError("Duplicate clientOrderId"),
            None,
        ]
    )
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=7,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    parsed = _open_signal(action=SignalAction.UPDATE_SL).model_copy(
        update={"stop_loss": Decimal("48000")}
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(7, "stop 48000"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.stop_loss == Decimal("49000")
    assert trade.last_exchange_sync_error == "Duplicate clientOrderId"
    assert trace.final_status == "update_sl_failed"
    assert "Duplicate clientOrderId" in (trace.effect_summary or "")
    assert signal is not None
    assert signal.stop_loss == Decimal("49000")
    assert engine._sync_trade_protection.await_args_list[0].kwargs == {
        "refresh_take_profits": False,
        "refresh_stop_loss": True,
    }
    assert engine._sync_trade_protection.await_args_list[1].kwargs == {
        "refresh_take_profits": False,
        "refresh_stop_loss": True,
    }


@pytest.mark.asyncio
async def test_handle_followup_cancel_closes_trade_and_marks_signal_cancelled(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._get_mark_price = AsyncMock(return_value=Decimal("50500"))  # type: ignore[method-assign]

    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=3,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=_open_signal(action=SignalAction.CANCEL),
        message=_message(3, "cancel this signal"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.status == "closed"
    assert trace.final_status == "cancelled_trade"
    assert signal is not None
    assert signal.status == "cancelled"


@pytest.mark.asyncio
async def test_handle_followup_cancel_uses_exchange_reported_pnl_and_confirms_close(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.close_long.return_value = SimpleNamespace(order_id="close_1")
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="close_1",
            status="FILLED",
            executed_qty=Decimal("10"),
            avg_price=Decimal("50550"),
        ),
        [
            SimpleNamespace(
                qty=Decimal("10"),
                realized_pnl=Decimal("12.5"),
                commission=Decimal("0.3"),
                price=Decimal("50550"),
            )
        ],
    )
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("0.001")
    )
    engine._ensure_trade_still_open_on_exchange = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    engine._futures_client.get_open_positions.return_value = []
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]

    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=4,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=_open_signal(action=SignalAction.CANCEL),
        message=_message(4, "cancel via exchange"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.status == "closed"
    assert trade.realized_pnl == Decimal("12.5")
    assert trade.fees == Decimal("0.3")
    assert trade.exit_price == Decimal("50550")
    assert trace.final_status == "cancelled_trade"
    assert "PnL=12.50000000" in (trace.effect_summary or "")
    assert signal is not None
    assert signal.status == "cancelled"
    engine._futures_client.close_long.assert_awaited_once_with(
        symbol="BTCUSDT",
        quantity=Decimal("0.01000000"),
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_handle_followup_close_retries_exchange_residual_until_flat(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._ensure_trade_still_open_on_exchange = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    engine._cancel_existing_trade_protection = AsyncMock()  # type: ignore[method-assign]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._futures_client.close_long.side_effect = [
        SimpleNamespace(order_id="close_1"),
        SimpleNamespace(order_id="close_2"),
    ]
    engine._futures_client.wait_for_order_fill.side_effect = [
        (
            SimpleNamespace(
                order_id="close_1",
                status="FILLED",
                executed_qty=Decimal("9"),
                avg_price=Decimal("50550"),
            ),
            [
                SimpleNamespace(
                    qty=Decimal("9"),
                    realized_pnl=Decimal("12.5"),
                    commission=Decimal("0.3"),
                    price=Decimal("50550"),
                )
            ],
        ),
        (
            SimpleNamespace(
                order_id="close_2",
                status="FILLED",
                executed_qty=Decimal("1"),
                avg_price=Decimal("50560"),
            ),
            [
                SimpleNamespace(
                    qty=Decimal("1"),
                    realized_pnl=Decimal("1.25"),
                    commission=Decimal("0.03"),
                    price=Decimal("50560"),
                )
            ],
        ),
    ]
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("0.001")
    )
    engine._futures_client.get_open_positions.side_effect = [
        [
            SimpleNamespace(
                symbol_internal="BTCUSDT",
                side="LONG",
                position=Decimal("1"),
            )
        ],
        [],
    ]

    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=5,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=_open_signal(action=SignalAction.CLOSE),
        message=_message(5, "close now"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.status == "closed"
    assert trade.remaining_quantity == Decimal("0")
    assert trade.realized_pnl == Decimal("13.75")
    assert trade.fees == Decimal("0.33")
    assert trace.final_status == "closed_trade"
    assert signal is not None
    assert signal.status == "closed"
    close_calls = engine._futures_client.close_long.await_args_list
    assert len(close_calls) == 2
    assert close_calls[0].kwargs["quantity"] == Decimal("0.01000000")
    assert close_calls[1].kwargs["quantity"] == Decimal("0.00100000")


@pytest.mark.asyncio
async def test_process_message_treats_stale_related_open_as_new_signal(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    trade.exchange_position_missing_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    trade.exchange_position_missing_confirmations = 1
    engine._classifier = SimpleNamespace(
        classify=lambda _message, _context: SimpleNamespace(
            parsed_signal=_open_signal(),
            classification="open",
            confidence=Decimal("0.9"),
            related_signal_id="sig_test",
            is_potential_new_signal=True,
            is_related_to_existing_signal=True,
            debug_notes=[],
        )
    )

    await engine._process_message(_message(2, "buy market"))

    new_signal_id = make_signal_id("@testchan", 2)
    new_signal = engine.store.load_signal_snapshot(
        engine.session.session_id,
        new_signal_id,
    )
    old_signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    trade_reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    traces = engine.store.list_message_traces(engine.session.session_id, limit=5)

    assert new_signal is not None
    assert new_signal.status == "pending_consolidation"
    assert old_signal is not None
    assert old_signal.status == "closed"
    assert trade_reloaded is not None
    assert trade_reloaded.status == "closed"
    assert "sig_test" not in engine._open_trades
    assert traces[0].final_status == "pending_consolidation"


@pytest.mark.asyncio
async def test_pending_signal_update_sl_keeps_open_action_and_opens_after_consolidation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._classifier = SimpleNamespace(
        classify=lambda _message, _context: SimpleNamespace(
            parsed_signal=_open_signal(action=SignalAction.UPDATE_SL).model_copy(
                update={"stop_loss": Decimal("48000")}
            ),
            classification="update_sl",
            confidence=Decimal("0.9"),
            related_signal_id="sig_test",
            is_potential_new_signal=False,
            is_related_to_existing_signal=True,
            debug_notes=[],
        )
    )

    await engine._process_message(_message(2, "stop 48000"))

    pending = context.get_signal("sig_test")
    traces = engine.store.list_message_traces(engine.session.session_id, limit=5)
    assert pending is not None
    assert pending.current_signal is not None
    assert pending.current_signal.action is SignalAction.OPEN
    assert pending.current_signal.stop_loss == Decimal("48000")
    assert traces[0].final_status == "signal_updated"
    assert traces[0].effect_summary == "Updated pending signal sig_test"

    engine._futures_client = AsyncMock()
    engine._futures_client.validate_symbol_tradable.return_value = SimpleNamespace(
        symbol="TBV_BTC-SWAP-TBV_USDT"
    )
    engine._get_mark_price = AsyncMock(return_value=Decimal("50000"))  # type: ignore[method-assign]
    engine._open_position = AsyncMock()  # type: ignore[method-assign]

    await engine._try_open_signal("sig_test", pending, context)

    assert "sig_test" not in context.pending_signal_ids
    opened = engine._open_position.await_args.kwargs["parsed"]
    assert opened.action is SignalAction.OPEN
    assert opened.stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_handle_followup_closes_stale_exchange_trade_before_updating(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    trade.exchange_position_missing_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    trade.exchange_position_missing_confirmations = 1
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=5,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=_open_signal(action=SignalAction.UPDATE_TP),
        message=_message(5, "tp update"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trace.final_status == "no_open_trade"
    assert signal is not None
    assert signal.status == "closed"
    assert "sig_test" not in engine._open_trades


@pytest.mark.asyncio
async def test_sync_exchange_state_marks_trade_closed_when_exchange_position_is_missing(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    engine._futures_client.get_order_history.return_value = []
    engine._futures_client.get_open_orders.return_value = []
    engine._futures_client.get_user_trades.return_value = []
    trade.exchange_position_missing_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    trade.exchange_position_missing_confirmations = 1

    await engine._sync_exchange_state()

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    trade_reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    assert signal is not None
    assert signal.status == "closed"
    assert trade_reloaded is not None
    assert trade_reloaded.status == "closed"
    assert engine._open_trades == {}


@pytest.mark.asyncio
async def test_sync_exchange_state_keeps_trade_open_on_first_missing_snapshot(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    engine._futures_client.get_order_history.return_value = []
    engine._futures_client.get_open_orders.return_value = []
    engine._futures_client.get_user_trades.return_value = []

    await engine._sync_exchange_state()

    trade_reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade_reloaded is not None
    assert trade_reloaded.status == "open"
    assert trade_reloaded.exchange_position_missing_confirmations == 1
    assert "pending_confirmation" in (trade_reloaded.last_exchange_sync_error or "")
    assert signal is not None
    assert signal.status == "open"
    assert "sig_test" in engine._open_trades


@pytest.mark.asyncio
async def test_ensure_trade_still_open_on_exchange_requires_confirmed_miss(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []

    first_seen = await engine._ensure_trade_still_open_on_exchange(
        context=context,
        trade=trade,
        reason="followup_exchange_position_missing",
    )

    assert first_seen is True
    assert trade.status == "open"
    assert trade.exchange_position_missing_confirmations == 1

    trade.exchange_position_missing_since = datetime.now(timezone.utc) - timedelta(seconds=30)

    second_seen = await engine._ensure_trade_still_open_on_exchange(
        context=context,
        trade=trade,
        reason="followup_exchange_position_missing",
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert second_seen is False
    assert trade.status == "closed"
    assert signal is not None
    assert signal.status == "closed"
    assert "sig_test" not in engine._open_trades


@pytest.mark.asyncio
async def test_ensure_trade_protection_after_open_keeps_trade_open_when_stop_exists(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.stop_loss = Decimal("49000")
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="buy market",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("tp order rejected")
    )

    async def _refresh_ids(item: LiveTrade) -> None:
        item.sl_order_id = "sl_live_1"
        item.tp_order_ids = []

    engine._refresh_trade_protection_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=_refresh_ids
    )

    await engine._ensure_trade_protection_after_open(trade)

    assert trade.status == "open"
    assert trade.sl_order_id == "sl_live_1"
    assert trade.protection_sync_failures == 1
    assert trade.last_exchange_sync_error == "tp order rejected"
    assert any(
        note == "protection_sync_degraded=tp order rejected"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_ensure_trade_protection_after_open_raises_when_stop_never_exists(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.stop_loss = Decimal("49000")
    engine._futures_client = AsyncMock()
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("stop rejected")
    )
    engine._refresh_trade_protection_ids = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="stop rejected"):
        await engine._ensure_trade_protection_after_open(trade)

    assert engine._sync_trade_protection.await_count == 3
    assert trade.sl_order_id is None
    assert trade.protection_sync_failures == 3


def test_restore_runtime_state_rehydrates_contexts_and_open_trades(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    session_id = engine.session.session_id
    trade = _trade(session_id)
    trade.channel_id = "@restore"
    trade.channel_input = "https://t.me/restore"
    trade.channel_label = "@restore"
    engine.store.save_trade(trade)
    engine.store.save_message_trace(
        session_id,
        LiveMessageTrace(
            session_id=session_id,
            message_id=77,
            channel_id="@restore",
            channel_username="restore",
            channel_label="@restore",
            reply_to_msg_id=None,
            message_date=datetime.now(timezone.utc),
            full_text="BUY BTCUSDT",
            signal_id="sig_test",
            trade_id=trade.trade_id,
            final_status="opened_trade",
        ),
    )
    engine.store.save_signal_snapshot(
        session_id,
        LiveSignalSnapshot(
            signal_id="sig_test",
            channel_id="@restore",
            channel_label="@restore",
            created_from_message_id=77,
            related_message_ids=[77],
            status="open",
            status_group="active",
            symbol="BTCUSDT",
            side="long",
            trade_id=trade.trade_id,
            updated_at=datetime.now(timezone.utc),
        ),
    )

    restored = _engine(tmp_path)
    restored._restore_runtime_state()

    assert "sig_test" in restored._open_trades
    context = restored._get_or_create_context("@restore")
    assert context.get_signal("sig_test") is not None
    assert context.get_message(77) is not None


def test_restore_runtime_state_normalizes_legacy_signal_snapshot_timestamps(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session_id = engine.session.session_id
    trade = _trade(session_id)
    opened_at = datetime.now(timezone.utc)
    updated_at = opened_at - timedelta(minutes=5)
    trade.opened_at = opened_at
    trade.updated_at = opened_at
    trade.channel_id = "@restore"
    trade.channel_input = "https://t.me/restore"
    trade.channel_label = "@restore"
    engine.store.save_trade(trade)
    engine.store.save_signal_snapshot(
        session_id,
        LiveSignalSnapshot(
            signal_id="sig_test",
            channel_id="@restore",
            channel_label="@restore",
            created_from_message_id=77,
            related_message_ids=[77],
            status="open",
            status_group="active",
            symbol="BTCUSDT",
            side="long",
            trade_id=trade.trade_id,
            opened_at=opened_at,
            updated_at=updated_at,
        ),
    )

    restored = _engine(tmp_path)
    restored._restore_runtime_state()

    context = restored._get_or_create_context("@restore")
    signal = context.get_signal("sig_test")
    assert signal is not None
    assert signal.created_at == updated_at
    assert signal.updated_at == updated_at
