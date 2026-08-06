"""Focused tests for live trading engine follow-up and signal syncing."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from triak_trade.backtesting.strategies.trailing_tp import TrailingTakeProfitStrategy
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, SignalStatus, TradeSide
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.errors import ToobitAPIError
from triak_trade.exchange.toobit.futures import FuturesContractSpec, FuturesOrder
from triak_trade.live_trading.account_coordinator import AccountExecutionCoordinator
from triak_trade.live_trading.engine import LiveTradingEngine
from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveEntryOrderPlan,
    LiveMessageTrace,
    LiveSession,
    LiveSignalSnapshot,
    LiveTakeProfitOrderPlan,
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
        LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE=Decimal("5"),
        LIVE_TRADING_MIN_FIRST_TAKE_PROFIT_PCT=Decimal("1.5"),
        LIVE_TRADING_ORDER_FILL_TIMEOUT_SECONDS=8,
        LIVE_TRADING_CLOSE_RECONCILE_ATTEMPTS=3,
        LIVE_TRADING_PROTECTION_SYNC_RETRY_ATTEMPTS=3,
        LIVE_TRADING_PROTECTION_SYNC_RETRY_DELAY_SECONDS=0,
        LIVE_TRADING_EXCHANGE_POSITION_MISS_CONFIRMATIONS=2,
        LIVE_TRADING_EXCHANGE_POSITION_MISS_GRACE_SECONDS=15,
        LIVE_TRADING_STOP_COOLDOWN_BASE_SECONDS=3600,
        LIVE_TRADING_STOP_COOLDOWN_MAX_SECONDS=21600,
        LIVE_TRADING_REQUIRE_AI_CLASSIFIER=False,
        LIVE_TRADING_FAIL_CLOSED_ON_LEVERAGE_SYNC_ERROR=True,
        LIVE_TRADING_FAIL_CLOSED_ON_PROTECTION_SYNC_ERROR=True,
        REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH=True,
        LIVE_TRADING_PRICE_REFRESH_SECONDS=60,
        LIVE_TRADING_ACCOUNT_REFRESH_SECONDS=60,
        LIVE_TRADING_TELEGRAM_POLL_SECONDS=1,
        LIVE_TRADING_STARTUP_REPLAY_SECONDS=180,
        LIVE_TRADING_STARTUP_HISTORY_LIMIT=5,
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


def _target_hit_action_by_remaining(**kwargs: object) -> SimpleNamespace:
    remaining = int(kwargs["remaining_targets_including_this"])
    close_fraction = Decimal("1") if remaining <= 1 else Decimal("0.35")
    return SimpleNamespace(
        close_fraction=close_fraction,
        move_sl_to_entry=False,
        new_stop_loss=None,
    )


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
async def test_stop_requested_before_start_prevents_engine_restart(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._setup_components = MagicMock()

    engine.stop()
    await engine.start()

    assert engine.session.status == "stopped"
    assert engine._running is False
    engine._setup_components.assert_not_called()


def _stopped_session_blocker(engine: LiveTradingEngine) -> tuple[LiveSession, LiveTrade]:
    session = _session().model_copy(
        update={
            "session_id": "ls_stopped",
            "status": "stopped",
            "open_positions_count": 1,
            "health_status": "critical",
            "new_entries_blocked": True,
        }
    )
    stale_trade = _trade(session.session_id).model_copy(
        update={
            "trade_id": "trade_stale",
            "signal_id": "sig_stale",
            "entry_order_id": "entry_stale",
        }
    )
    issue = (
        f"unresolved open trade {stale_trade.trade_id} belongs to a stopped session"
    )
    session.health_issues = [issue]
    session.last_error = issue
    engine.store.save_session(session)
    engine.store.save_trade(stale_trade)
    engine._account_coordinator.block_trade_bucket(
        stale_trade,
        trading_mode=session.trading_mode,
        reason=issue,
    )
    return session, stale_trade


@pytest.mark.asyncio
async def test_stopped_session_bucket_auto_reconciles_when_exchange_is_flat(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session, stale_trade = _stopped_session_blocker(engine)
    candidate = _trade(engine.session.session_id).model_copy(update={"trade_id": "candidate"})
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    engine._futures_client.get_open_orders.return_value = []
    engine._futures_client.get_open_algo_orders.return_value = []

    await engine._reconcile_stopped_session_bucket_if_flat(candidate)

    reconciled = engine.store.load_trade(session.session_id, stale_trade.trade_id)
    assert reconciled is not None
    assert reconciled.status == "closed"
    assert reconciled.remaining_quantity == Decimal("0")
    assert reconciled.pending_fill_audit_quantity == stale_trade.remaining_quantity
    assert reconciled.close_reason == "stopped_session_exchange_flat_auto_reconciled"
    assert (
        engine._account_coordinator.bucket_block_reason(
            candidate,
            trading_mode=engine.session.trading_mode,
        )
        is None
    )


@pytest.mark.asyncio
async def test_stopped_session_bucket_is_retained_when_exchange_position_exists(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session, stale_trade = _stopped_session_blocker(engine)
    candidate = _trade(engine.session.session_id).model_copy(update={"trade_id": "candidate"})
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = [SimpleNamespace()]
    engine._position_snapshot = MagicMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
    engine._find_matching_exchange_position = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace()
    )

    await engine._reconcile_stopped_session_bucket_if_flat(candidate)

    retained = engine.store.load_trade(session.session_id, stale_trade.trade_id)
    assert retained is not None
    assert retained.is_open


@pytest.mark.asyncio
async def test_stopped_session_bucket_is_retained_when_owned_order_exists(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session, stale_trade = _stopped_session_blocker(engine)
    candidate = _trade(engine.session.session_id).model_copy(update={"trade_id": "candidate"})
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    engine._futures_client.get_open_orders.return_value = [
        SimpleNamespace(order_id=stale_trade.entry_order_id)
    ]
    engine._futures_client.get_open_algo_orders.return_value = []

    await engine._reconcile_stopped_session_bucket_if_flat(candidate)

    retained = engine.store.load_trade(session.session_id, stale_trade.trade_id)
    assert retained is not None
    assert retained.is_open


def test_stop_cooldown_grows_after_consecutive_same_direction_stops(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(timezone.utc)
    for index, minutes_ago in enumerate((20, 10), start=1):
        trade = _trade(engine.session.session_id).model_copy(
            update={
                "trade_id": f"stopped_{index}",
                "signal_id": f"stopped_signal_{index}",
                "status": "closed",
                "close_reason": "sl_hit",
                "closed_at": now - timedelta(minutes=minutes_ago),
                "remaining_quantity": Decimal("0"),
            }
        )
        engine.store.save_trade(trade)

    decision = engine._stop_cooldown_decision(_open_signal(), "@testchan")

    assert decision.blocked is True
    assert decision.consecutive_stops == 2
    assert 6500 <= decision.remaining_seconds <= 6600
    assert decision.source_trade_id == "stopped_2"


def test_profitable_close_resets_stop_cooldown(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(timezone.utc)
    stopped = _trade(engine.session.session_id).model_copy(
        update={
            "trade_id": "stopped",
            "signal_id": "stopped_signal",
            "status": "closed",
            "close_reason": "sl_hit",
            "closed_at": now - timedelta(minutes=20),
            "remaining_quantity": Decimal("0"),
        }
    )
    profitable = stopped.model_copy(
        update={
            "trade_id": "profitable",
            "signal_id": "profitable_signal",
            "close_reason": "tp2_hit",
            "closed_at": now - timedelta(minutes=5),
        }
    )
    engine.store.save_trade(stopped)
    engine.store.save_trade(profitable)

    decision = engine._stop_cooldown_decision(_open_signal(), "@testchan")

    assert decision.blocked is False


@pytest.mark.asyncio
async def test_try_open_signal_blocks_during_stop_cooldown_before_exchange_call(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    stopped = _trade(engine.session.session_id).model_copy(
        update={
            "trade_id": "recent_stop",
            "signal_id": "recent_stop_signal",
            "status": "closed",
            "close_reason": "sl_hit",
            "closed_at": datetime.now(timezone.utc) - timedelta(minutes=5),
            "remaining_quantity": Decimal("0"),
        }
    )
    engine.store.save_trade(stopped)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._futures_client = AsyncMock()

    await engine._try_open_signal("sig_test", state, context)

    assert state.status is SignalStatus.CANCELLED
    engine._futures_client.validate_symbol_tradable.assert_not_awaited()
    snapshot = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert snapshot is not None
    assert snapshot.status == SignalStatus.CANCELLED.value


def test_normalize_open_geometry_infers_long_side_for_unknown_signal(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    parsed, error = engine._normalize_or_reject_open_geometry(
        _open_signal().model_copy(
            update={
                "side": TradeSide.UNKNOWN,
                "entry_low": Decimal("0.5895"),
                "entry_high": Decimal("0.6282"),
                "stop_loss": None,
                "take_profits": [
                    Decimal("0.6450"),
                    Decimal("0.6679"),
                    Decimal("0.6859"),
                    Decimal("0.7412"),
                ],
            }
        )
    )

    assert error is None
    assert parsed.side is TradeSide.LONG


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
async def test_try_open_signal_invalid_symbol_emits_structured_log(
    tmp_path: Path,
    caplog,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.PENDING_CONSOLIDATION)
    context.add_signal(state, pending=True)
    engine._futures_client = AsyncMock()
    engine._futures_client.validate_symbol_tradable.side_effect = ValueError("symbol not tradable")

    with caplog.at_level(logging.INFO):
        await engine._try_open_signal("sig_test", state, context)

    record = next(rec for rec in caplog.records if rec.msg == "live_trading.signal_rejected")
    assert record.session_id == "ls_test"
    assert record.signal_id == "sig_test"
    assert record.reason == "invalid_exchange_symbol"
    assert record.symbol == "BTCUSDT"


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
        note.startswith("synthetic_stop_strategy=") for note in trade.message_history[-1].notes
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
async def test_process_message_ignore_emits_structured_logs(
    tmp_path: Path,
    caplog,
) -> None:
    engine = _engine(tmp_path)
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )
    message = _message(301, "noise")

    with caplog.at_level(logging.INFO):
        await engine._process_message(message)

    events = [rec.msg for rec in caplog.records]
    assert "live_trading.message_processing_started" in events
    assert "live_trading.message_classified" in events
    assert "live_trading.message_ignored" in events
    assert "live_trading.message_processing_completed" in events
    completed = next(
        rec for rec in caplog.records if rec.msg == "live_trading.message_processing_completed"
    )
    assert completed.final_status == "ignored"
    assert completed.message_id == 301


@pytest.mark.asyncio
async def test_recent_dexe_open_is_not_merged_into_opposite_side_stale_signal(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    channel_id = "https://t.me/Scalping_300"
    context = engine._get_or_create_context(channel_id)
    old_short = _open_signal().model_copy(
        update={
            "symbol": "DEXEUSDT",
            "side": TradeSide.SHORT,
            "entry_low": Decimal("3.203"),
            "entry_high": Decimal("3.36"),
            "stop_loss": Decimal("3.45924"),
            "take_profits": [Decimal("3.186985")],
            "source_channel_id": channel_id,
            "source_message_id": 158516,
        }
    )
    old_state = _state(old_short, status=SignalStatus.OPEN).model_copy(
        update={
            "signal_id": "sig_dexe_old",
            "channel_id": channel_id,
            "created_from_message_id": 158516,
            "related_message_ids": [158516],
        }
    )
    context.add_signal(old_state, pending=False)
    new_long = old_short.model_copy(
        update={
            "side": TradeSide.LONG,
            "entry_low": Decimal("3.063"),
            "entry_high": Decimal("3.105"),
            "stop_loss": Decimal("2.8566"),
            "take_profits": [
                Decimal("3.120525"),
                Decimal("3.13605"),
                Decimal("3.1671"),
                Decimal("3.19815"),
                Decimal("3.2292"),
                Decimal("3.26025"),
                Decimal("3.2913"),
                Decimal("3.32235"),
            ],
            "source_message_id": 158712,
            "parser_version": "ai-v1",
        }
    )
    engine._classifier = SimpleNamespace(
        classify=lambda raw, current_context: SimpleNamespace(
            parsed_signal=new_long,
            classification="open",
            related_signal_id="sig_from_context",
            debug_notes=["classifier=ai", "ai_required=true"],
        )
    )
    message = _message(
        158712,
        (
            "DEXEUSDT\nDirection: LONG\nLeverage: Cross 20x\n"
            "Entry: 3.063 — 3.105\nStop Loss: 2.8566\n"
            "Target 1 - 3.120525\nTarget 2 - 3.13605\n"
            "Target 3 - 3.1671\nTarget 4 - 3.19815\n"
            "Target 5 - 3.2292\nTarget 6 - 3.26025\n"
            "Target 7 - 3.2913\nTarget 8 - 3.32235"
        ),
    ).model_copy(
        update={
            "channel_id": channel_id,
            "channel_username": "Scalping_300",
        }
    )

    await engine._process_message(message)

    expected_signal_id = make_signal_id(channel_id, 158712)
    trace = engine.store.list_message_traces(engine.session.session_id, limit=1)[0]
    assert trace.final_status == "pending_consolidation"
    assert trace.signal_id == expected_signal_id
    assert trace.correlation_method == "new_signal"
    assert "classifier=ai" in trace.debug_notes
    assert expected_signal_id in context.pending_signal_ids
    assert 158712 not in old_state.related_message_ids


@pytest.mark.asyncio
async def test_new_open_detaches_same_side_signal_without_open_trade(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    stale_state = _state(_open_signal(), status=SignalStatus.OPEN)
    context.add_signal(stale_state, pending=False)
    engine._classifier = SimpleNamespace(
        classify=lambda raw, current_context: SimpleNamespace(
            parsed_signal=_open_signal().model_copy(update={"source_message_id": raw.message_id}),
            classification="open",
            related_signal_id=None,
            debug_notes=["classifier=ai"],
        )
    )

    await engine._process_message(_message(302, "BTCUSDT LONG Entry 50000"))

    new_signal_id = make_signal_id("@testchan", 302)
    trace = engine.store.list_message_traces(engine.session.session_id, limit=1)[0]
    assert trace.final_status == "pending_consolidation"
    assert trace.signal_id == new_signal_id
    assert trace.correlation_method == "new_signal"
    assert trace.correlation_note is None
    assert "stale_related_signal_detached=sig_test" in trace.debug_notes
    assert new_signal_id in context.pending_signal_ids


@pytest.mark.asyncio
async def test_authoritative_ai_ignore_cannot_be_promoted_to_close_all(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal(), status=SignalStatus.OPEN)
    trade = _trade(engine.session.session_id)
    context.add_signal(state, pending=False)
    engine._open_trades[state.signal_id] = trade
    ignored = _open_signal(action=SignalAction.IGNORE).model_copy(
        update={"symbol": None, "side": TradeSide.UNKNOWN}
    )
    engine._classifier = SimpleNamespace(
        classify=lambda raw, current_context: SimpleNamespace(
            parsed_signal=ignored,
            classification="UNRELATED",
            related_signal_id=None,
            debug_notes=[
                "classifier=ai",
                "classification=UNRELATED",
                "reasoning_summary=conversational message",
            ],
        )
    )

    await engine._process_message(
        _message(303, "داستان همه پوزیشن‌ها را ببندید جالبه")  # noqa: RUF001
    )

    trace = engine.store.list_message_traces(engine.session.session_id, limit=1)[0]
    assert trace.final_status == "ignored"
    assert trade.is_open


@pytest.mark.asyncio
async def test_poll_messages_once_persists_heartbeat_without_new_messages(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._restore_runtime_state()
    engine._telegram_client = FakeTelegramClient(history_by_channel={"https://t.me/testchan": []})
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
            if limit is not None and min_message_id is None:
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
async def test_start_replays_recent_startup_messages_without_waiting_for_next_poll(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._setup_components = MagicMock()  # type: ignore[method-assign]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._price_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._account_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._consolidation_tick_loop = AsyncMock()  # type: ignore[method-assign]

    recent_message = _message(222, "fresh startup message").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "date": engine.session.started_at - timedelta(seconds=30),
        }
    )

    class StartupReplayTelegramClient(FakeTelegramClient):
        async def fetch_history(
            self,
            channel: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
            min_message_id: int | None = None,
        ) -> list[RawTelegramMessage]:
            if limit is not None and min_message_id is None:
                return [recent_message]
            return []

    engine._telegram_client = StartupReplayTelegramClient()
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )

    poll_started = asyncio.Event()

    async def _blocking_poll_loop() -> None:
        poll_started.set()
        await asyncio.sleep(3600)

    engine._poll_messages_loop = _blocking_poll_loop  # type: ignore[method-assign]

    task = asyncio.create_task(engine.start())
    await poll_started.wait()
    await asyncio.sleep(0)
    engine.stop()
    await task

    traces = engine.store.list_message_traces(engine.session.session_id, limit=10)
    assert len(traces) == 1
    assert traces[0].message_id == 222
    assert engine.session.total_messages_processed == 1


@pytest.mark.asyncio
async def test_start_records_startup_backlog_messages_in_stream_without_trading(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._running = True
    engine._setup_components = MagicMock()  # type: ignore[method-assign]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._price_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._account_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._consolidation_tick_loop = AsyncMock()  # type: ignore[method-assign]

    old_message = _message(223, "older backlog message").model_copy(
        update={
            "channel_id": "https://t.me/testchan",
            "channel_username": "testchan",
            "date": engine.session.started_at - timedelta(hours=5),
        }
    )

    class StartupBacklogTelegramClient(FakeTelegramClient):
        async def fetch_history(
            self,
            channel: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
            min_message_id: int | None = None,
        ) -> list[RawTelegramMessage]:
            if limit is not None and min_message_id is None:
                return [old_message]
            return []

    engine._telegram_client = StartupBacklogTelegramClient()
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_ignored_signal().model_copy(
                update={"source_message_id": raw.message_id}
            ),
            classification="ignore",
            related_signal_id=None,
        )
    )

    poll_started = asyncio.Event()

    async def _blocking_poll_loop() -> None:
        poll_started.set()
        await asyncio.sleep(3600)

    engine._poll_messages_loop = _blocking_poll_loop  # type: ignore[method-assign]

    task = asyncio.create_task(engine.start())
    await poll_started.wait()
    await asyncio.sleep(0)
    engine.stop()
    await task

    traces = engine.store.list_message_traces(engine.session.session_id, limit=10)
    assert len(traces) == 1
    assert traces[0].message_id == 223
    assert traces[0].classification == "startup_backlog"
    assert traces[0].final_status == "startup_backlog"
    assert engine.session.total_messages_processed == 1


@pytest.mark.asyncio
async def test_recovery_only_start_disables_telegram_and_entry_workers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.session.recovery_only = True
    engine._open_trades["sig_test"] = _trade(engine.session.session_id)
    engine._setup_components = MagicMock()  # type: ignore[method-assign]
    engine._restore_runtime_state = MagicMock()  # type: ignore[method-assign]
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]
    engine._bootstrap_channel_cursors = AsyncMock()  # type: ignore[method-assign]
    engine._poll_messages_loop = AsyncMock()  # type: ignore[method-assign]
    engine._price_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._account_refresh_loop = AsyncMock()  # type: ignore[method-assign]
    engine._pending_entry_monitor_loop = AsyncMock()  # type: ignore[method-assign]
    engine._consolidation_tick_loop = AsyncMock()  # type: ignore[method-assign]
    recovery_started = asyncio.Event()

    async def _blocking_recovery_loop() -> None:
        recovery_started.set()
        await asyncio.sleep(3600)

    engine._recovery_only_monitor_loop = _blocking_recovery_loop  # type: ignore[method-assign]

    task = asyncio.create_task(engine.start())
    await recovery_started.wait()
    engine.stop()
    await task

    engine._bootstrap_channel_cursors.assert_not_awaited()
    engine._poll_messages_loop.assert_not_awaited()
    engine._pending_entry_monitor_loop.assert_not_awaited()
    engine._consolidation_tick_loop.assert_not_awaited()


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
async def test_process_message_promotes_unmatched_visual_followup_to_new_signal(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_open_signal(action=SignalAction.UPDATE_SL).model_copy(
                update={
                    "side": TradeSide.SHORT,
                    "entry_type": EntryType.UNKNOWN,
                    "entry_low": None,
                    "entry_high": None,
                    "stop_loss": Decimal("66200"),
                    "take_profits": [],
                    "source_message_id": raw.message_id,
                }
            ),
            classification="follow_up",
            related_signal_id=None,
        )
    )
    message = _message(203, "استاپ 66200").model_copy(
        update={
            "raw_payload": {
                "has_media": True,
                "caption_present": True,
                "image_data_urls": [
                    {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,abc"}
                ],
            }
        }
    )

    await engine._process_message(message)

    signal_id = make_signal_id("@testchan", 203)
    snapshot = engine.store.load_signal_snapshot(engine.session.session_id, signal_id)
    traces = engine.store.list_message_traces(engine.session.session_id, limit=5)

    assert snapshot is not None
    assert snapshot.status == "pending_consolidation"
    assert snapshot.stop_loss == Decimal("66200")
    assert traces[0].signal_id == signal_id
    assert traces[0].parsed_action == "open"
    assert traces[0].correlation_method == "promoted_visual_signal"
    assert traces[0].final_status == "pending_consolidation"
    assert traces[0].correlation_note is not None
    assert "caption_media" in traces[0].correlation_note
    assert "stop_loss" in traces[0].correlation_note


@pytest.mark.asyncio
async def test_process_message_visual_followup_keeps_existing_signal_attachment(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    context.add_signal(state, pending=False)
    engine._classifier = SimpleNamespace(
        classify=lambda raw, context: SimpleNamespace(
            parsed_signal=_open_signal(action=SignalAction.UPDATE_SL).model_copy(
                update={
                    "stop_loss": Decimal("48000"),
                    "source_message_id": raw.message_id,
                }
            ),
            classification="follow_up",
            related_signal_id="sig_test",
        )
    )
    message = _message(204, "استاپ 48000").model_copy(
        update={
            "raw_payload": {
                "has_media": True,
                "caption_present": True,
                "image_data_urls": [
                    {"mime_type": "image/jpeg", "data_url": "data:image/jpeg;base64,abc"}
                ],
            }
        }
    )

    await engine._process_message(message)

    traces = engine.store.list_message_traces(engine.session.session_id, limit=5)
    assert traces[0].signal_id == "sig_test"
    assert traces[0].parsed_action == "update_sl"
    assert traces[0].correlation_method == "ai"
    assert traces[0].final_status == "no_open_trade"


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


def test_explicit_pending_entry_without_price_is_never_promoted_to_market(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    parsed = _open_signal().model_copy(
        update={
            "entry_type": EntryType.TRIGGER,
            "entry_low": None,
            "entry_high": None,
        }
    )

    normalized = engine._normalize_missing_entry_to_market(parsed)

    assert normalized.entry_type is EntryType.TRIGGER
    assert normalized.entry_low is None
    assert normalized.entry_high is None


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
async def test_real_limit_entry_is_submitted_pending_without_market_fill_wait(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.LIMIT.value
    trade.entry_price = Decimal("0.2941")
    trade.quantity = Decimal("100")
    trade.remaining_quantity = Decimal("100")
    trade.requested_entry_low = Decimal("0.2941")
    trade.requested_entry_high = Decimal("0.2941")
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "BANK-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )
    engine._futures_client.open_long.return_value = SimpleNamespace(
        order_id="entry_limit_1",
        client_order_id="triak_entry_trade_test_limit",
        exchange_symbol="TBV_BANK-SWAP-TBV_USDT",
        status="NEW",
    )
    engine._ensure_exchange_quantity_supports_market_entry = MagicMock()  # type: ignore[method-assign]
    engine._ensure_exchange_quantity_supports_take_profit_ladder = MagicMock()  # type: ignore[method-assign]
    engine._apply_exchange_risk_limit_to_open_trade = MagicMock()  # type: ignore[method-assign]
    engine._ensure_supported_exchange_leverage = AsyncMock()  # type: ignore[method-assign]
    engine._ensure_trade_protection_after_open = AsyncMock()  # type: ignore[method-assign]

    await engine._real_open_position(
        trade,
        current_mark_price=Decimal("0.30751"),
    )

    assert trade.status == "waiting_entry"
    assert trade.entry_order_type == "LIMIT"
    assert trade.entry_order_status == "NEW"
    assert trade.entry_order_id == "entry_limit_1"
    engine._futures_client.open_long.assert_awaited_once()
    call = engine._futures_client.open_long.await_args.kwargs
    assert call["order_type"] == "LIMIT"
    assert call["price"] == Decimal("0.2941")
    assert call["stop_price"] is None
    engine._futures_client.wait_for_order_fill.assert_not_awaited()
    engine._ensure_trade_protection_after_open.assert_not_awaited()


def test_range_entry_plan_splits_exact_risk_quantity_25_50_25(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_type = EntryType.RANGE.value
    trade.requested_entry_low = Decimal("90")
    trade.requested_entry_high = Decimal("110")
    trade.quantity = Decimal("100")
    spec = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        contract_min_qty=Decimal("1"),
        contract_step_size=Decimal("1"),
    )

    plan = engine._build_range_entry_order_plan(trade=trade, spec=spec)

    assert [leg.label for leg in plan] == [
        "range_start",
        "range_midpoint",
        "range_end",
    ]
    assert [leg.price for leg in plan] == [Decimal("90"), Decimal("100"), Decimal("110")]
    assert [leg.quantity for leg in plan] == [Decimal("25"), Decimal("50"), Decimal("25")]
    assert sum((leg.quantity for leg in plan), Decimal("0")) == trade.quantity


def test_range_entry_plan_falls_back_when_three_orders_cannot_meet_minimum(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_type = EntryType.RANGE.value
    trade.requested_entry_low = Decimal("90")
    trade.requested_entry_high = Decimal("110")
    trade.quantity = Decimal("3")
    spec = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        contract_min_qty=Decimal("1"),
        contract_step_size=Decimal("1"),
    )

    assert engine._build_range_entry_order_plan(trade=trade, spec=spec) == []


def test_range_entry_target_policy_thresholds_are_exact(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=label,
            price=price,
            quantity=quantity,
            fraction=fraction,
            order_id=f"range_{index}",
            status="FILLED" if index == 0 else "NEW",
            executed_quantity=Decimal("25") if index == 0 else Decimal("0"),
        )
        for index, (label, price, quantity, fraction) in enumerate(
            (
                ("range_start", Decimal("90"), Decimal("25"), Decimal("0.25")),
                ("range_midpoint", Decimal("100"), Decimal("50"), Decimal("0.50")),
                ("range_end", Decimal("110"), Decimal("25"), Decimal("0.25")),
            )
        )
    ]

    assert engine._range_entry_target_cancel_leg_indexes(trade, target_hits=1) == set()
    assert engine._range_entry_target_cancel_leg_indexes(trade, target_hits=2) == {1, 2}

    trade.entry_order_plan[1].status = "FILLED"
    trade.entry_order_plan[1].executed_quantity = Decimal("50")
    assert engine._range_entry_target_cancel_leg_indexes(trade, target_hits=1) == {2}


@pytest.mark.asyncio
async def test_range_entry_target_policy_cancels_only_required_future_legs(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("90") + Decimal(index * 10),
            quantity=quantity,
            fraction=fraction,
            order_id=f"range_{index}",
            status="FILLED" if index == 0 else "NEW",
            executed_quantity=Decimal("25") if index == 0 else Decimal("0"),
        )
        for index, (quantity, fraction) in enumerate(
            (
                (Decimal("25"), Decimal("0.25")),
                (Decimal("50"), Decimal("0.50")),
                (Decimal("25"), Decimal("0.25")),
            )
        )
    ]
    engine._cancel_range_entry_orders = AsyncMock()  # type: ignore[method-assign]

    await engine._apply_range_entry_target_cancellation_policy(
        trade=trade,
        target_hits=1,
        reason="first_leg_tp1",
    )
    engine._cancel_range_entry_orders.assert_not_awaited()

    await engine._apply_range_entry_target_cancellation_policy(
        trade=trade,
        target_hits=2,
        reason="first_leg_tp2",
    )
    engine._cancel_range_entry_orders.assert_awaited_once_with(
        trade=trade,
        reason="first_leg_tp2",
        reconcile_fills=True,
        ensure_protection=True,
        leg_indexes={1, 2},
    )

    engine._cancel_range_entry_orders.reset_mock()
    trade.entry_order_plan[1].status = "FILLED"
    trade.entry_order_plan[1].executed_quantity = Decimal("50")
    await engine._apply_range_entry_target_cancellation_policy(
        trade=trade,
        target_hits=1,
        reason="midpoint_leg_tp1",
    )
    engine._cancel_range_entry_orders.assert_awaited_once_with(
        trade=trade,
        reason="midpoint_leg_tp1",
        reconcile_fills=True,
        ensure_protection=True,
        leg_indexes={2},
    )


@pytest.mark.asyncio
async def test_selective_range_cancellation_leaves_untargeted_leg_open(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("90") + Decimal(index * 10),
            quantity=Decimal("25"),
            fraction=Decimal("0.25"),
            order_id=f"range_{index}",
            status="FILLED" if index == 0 else "NEW",
            executed_quantity=Decimal("25") if index == 0 else Decimal("0"),
        )
        for index in range(3)
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_order.return_value = SimpleNamespace(
        order_id="range_2",
        status="CANCELED",
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
    )

    await engine._cancel_range_entry_orders(
        trade=trade,
        reason="tp1_after_midpoint_fill",
        reconcile_fills=False,
        leg_indexes={2},
    )

    engine._futures_client.cancel_order.assert_awaited_once_with(
        symbol=trade.symbol,
        order_id="range_2",
        use_demo_symbol=True,
    )
    assert trade.entry_order_plan[1].status == "NEW"
    assert trade.entry_order_plan[2].status == "CANCELED"


@pytest.mark.asyncio
async def test_real_range_entry_submits_three_durable_limit_legs(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.RANGE.value
    trade.entry_price = Decimal("100")
    trade.requested_entry_low = Decimal("90")
    trade.requested_entry_high = Decimal("110")
    trade.quantity = Decimal("100")
    trade.remaining_quantity = Decimal("100")
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        contract_min_qty=Decimal("1"),
        contract_step_size=Decimal("1"),
    )
    engine._futures_client.open_long.side_effect = [
        SimpleNamespace(order_id=f"range_{index}", client_order_id=None, status="NEW")
        for index in range(3)
    ]
    engine._ensure_exchange_quantity_supports_market_entry = MagicMock()  # type: ignore[method-assign]
    engine._ensure_exchange_quantity_supports_take_profit_ladder = MagicMock()  # type: ignore[method-assign]
    engine._apply_exchange_risk_limit_to_open_trade = MagicMock()  # type: ignore[method-assign]
    engine._ensure_supported_exchange_leverage = AsyncMock()  # type: ignore[method-assign]

    await engine._real_open_position(trade)

    assert trade.status == "waiting_entry"
    assert trade.entry_order_type == "LIMIT_LADDER"
    assert trade.entry_order_status == "LADDER_PENDING"
    assert trade.entry_order_id == "range_1"
    assert trade.entry_planned_quantity == Decimal("100")
    assert trade.remaining_quantity == Decimal("100")
    assert engine._account_coordinator.bucket_logical_quantity(
        trading_mode=engine.session.trading_mode,
        symbol=trade.symbol,
        side=trade.side,
    ) == Decimal("100")

    assert [leg.order_id for leg in trade.entry_order_plan] == [
        "range_0",
        "range_1",
        "range_2",
    ]
    submitted_quantities = [
        call.kwargs["quantity"] for call in engine._futures_client.open_long.await_args_list
    ]
    assert submitted_quantities == [
        Decimal("25"),
        Decimal("50"),
        Decimal("25"),
    ]
    assert [call.kwargs["price"] for call in engine._futures_client.open_long.await_args_list] == [
        Decimal("90"),
        Decimal("100"),
        Decimal("110"),
    ]


@pytest.mark.asyncio
async def test_range_entry_reconciliation_accumulates_partial_leg_fills_once(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.RANGE.value
    trade.quantity = Decimal("100")
    trade.remaining_quantity = Decimal("0")
    trade.entry_planned_quantity = Decimal("100")
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=label,
            price=price,
            quantity=quantity,
            fraction=fraction,
            order_id=f"range_{index}",
            status="NEW",
        )
        for index, (label, price, quantity, fraction) in enumerate(
            (
                ("range_start", Decimal("90"), Decimal("25"), Decimal("0.25")),
                ("range_midpoint", Decimal("100"), Decimal("50"), Decimal("0.50")),
                ("range_end", Decimal("110"), Decimal("25"), Decimal("0.25")),
            )
        )
    ]
    spec = SimpleNamespace(contract_multiplier=Decimal("1"))
    engine._futures_client = AsyncMock()
    engine._pm.rebase_trade_protection_after_entry_fill = MagicMock(return_value=[])
    engine._enforce_trade_protection_or_flatten = AsyncMock()  # type: ignore[method-assign]
    first_snapshot = [
        SimpleNamespace(
            order_id="range_0",
            status="FILLED",
            executed_qty=Decimal("25"),
            avg_price=Decimal("90"),
        ),
        SimpleNamespace(
            order_id="range_1",
            status="NEW",
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        ),
        SimpleNamespace(
            order_id="range_2",
            status="NEW",
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        ),
    ]

    activated = await engine._reconcile_range_entry_orders(
        trade=trade,
        history_orders=first_snapshot,
        open_orders=[],
        symbol_user_trades=[],
        spec=spec,
    )
    assert activated is True
    assert trade.quantity == Decimal("25")
    assert trade.remaining_quantity == Decimal("25")
    assert trade.entry_price == Decimal("90")
    assert engine._account_coordinator.bucket_logical_quantity(
        trading_mode=engine.session.trading_mode,
        symbol=trade.symbol,
        side=trade.side,
    ) == Decimal("100")

    trade.targets_hit = 1
    trade.stop_loss = Decimal("95")
    engine._apply_range_entry_target_cancellation_policy = AsyncMock()  # type: ignore[method-assign]

    second_snapshot = [
        first_snapshot[0],
        SimpleNamespace(
            order_id="range_1",
            status="FILLED",
            executed_qty=Decimal("50"),
            avg_price=Decimal("100"),
        ),
        first_snapshot[2],
    ]
    await engine._reconcile_range_entry_orders(
        trade=trade,
        history_orders=second_snapshot,
        open_orders=[],
        symbol_user_trades=[],
        spec=spec,
    )
    await engine._reconcile_range_entry_orders(
        trade=trade,
        history_orders=second_snapshot,
        open_orders=[],
        symbol_user_trades=[],
        spec=spec,
    )

    assert trade.quantity == Decimal("75")
    assert trade.remaining_quantity == Decimal("75")
    assert trade.entry_filled_quantity == Decimal("75")
    assert trade.entry_price == Decimal("96.66666666666666666666666667")
    assert trade.targets_hit == 1
    assert trade.stop_loss == Decimal("95")
    assert engine._account_coordinator.bucket_logical_quantity(
        trading_mode=engine.session.trading_mode,
        symbol=trade.symbol,
        side=trade.side,
    ) == Decimal("100")
    assert engine._enforce_trade_protection_or_flatten.await_count == 2
    engine._pm.rebase_trade_protection_after_entry_fill.assert_called_once()
    engine._apply_range_entry_target_cancellation_policy.assert_awaited_once_with(
        trade=trade,
        target_hits=1,
        reason="range_entry_fill_after_target_progress",
    )


@pytest.mark.asyncio
async def test_range_entry_submission_timeout_keeps_known_fill_protected_and_blocks(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.RANGE.value
    trade.entry_price = Decimal("100")
    trade.requested_entry_low = Decimal("90")
    trade.requested_entry_high = Decimal("110")
    trade.quantity = Decimal("100")
    engine._futures_client = AsyncMock()
    spec = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        contract_min_qty=Decimal("1"),
        contract_step_size=Decimal("1"),
    )
    engine._futures_client.get_contract_spec.return_value = spec
    filled_order = SimpleNamespace(
        order_id="range_0",
        client_order_id="entry_start",
        status="FILLED",
        executed_qty=Decimal("25"),
        avg_price=Decimal("90"),
    )
    engine._futures_client.open_long.side_effect = [
        filled_order,
        TimeoutError("submission response lost"),
    ]
    engine._futures_client.get_user_trades.return_value = []
    engine._futures_client.get_order.side_effect = [
        ToobitAPIError("unknown client order state"),
        filled_order,
    ]
    engine._ensure_exchange_quantity_supports_market_entry = MagicMock()  # type: ignore[method-assign]
    engine._ensure_exchange_quantity_supports_take_profit_ladder = MagicMock()  # type: ignore[method-assign]
    engine._apply_exchange_risk_limit_to_open_trade = MagicMock()  # type: ignore[method-assign]
    engine._ensure_supported_exchange_leverage = AsyncMock()  # type: ignore[method-assign]
    engine._pm.rebase_trade_protection_after_entry_fill = MagicMock(return_value=[])
    engine._enforce_trade_protection_or_flatten = AsyncMock()  # type: ignore[method-assign]

    await engine._real_open_position(trade)

    assert trade.status == "open"
    assert trade.quantity == Decimal("25")
    assert trade.remaining_quantity == Decimal("25")
    assert trade.entry_price == Decimal("90")
    assert trade.entry_order_plan[1].client_order_id is not None
    assert trade.entry_order_plan[1].order_id is None
    assert engine.session.health_status == "critical"
    assert engine.session.new_entries_blocked is True
    assert engine._enforce_trade_protection_or_flatten.await_count >= 2


@pytest.mark.asyncio
async def test_expired_unfilled_range_entry_cancels_all_legs(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.RANGE.value
    trade.quantity = Decimal("100")
    trade.remaining_quantity = Decimal("0")
    trade.entry_planned_quantity = Decimal("100")
    trade.entry_order_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("90") + Decimal(index * 10),
            quantity=quantity,
            fraction=fraction,
            order_id=f"range_{index}",
            status="NEW",
        )
        for index, (quantity, fraction) in enumerate(
            (
                (Decimal("25"), Decimal("0.25")),
                (Decimal("50"), Decimal("0.50")),
                (Decimal("25"), Decimal("0.25")),
            )
        )
    ]
    engine._open_trades[trade.signal_id] = trade
    engine.session.open_positions_count = 1
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1")
    )
    engine._futures_client.get_order.side_effect = [
        SimpleNamespace(
            order_id=f"range_{index}",
            status="CANCELED",
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )
        for index in range(3)
    ]
    engine._futures_client.get_user_trades.return_value = []

    activated = await engine._reconcile_pending_entry(
        trade=trade,
        history_orders=[],
        open_orders=[],
        symbol_user_trades=[],
    )

    assert activated is False
    assert trade.status == "expired"
    assert trade.close_reason == "entry_order_expired"
    assert trade.signal_id not in engine._open_trades
    assert engine._futures_client.cancel_order.await_count == 3


def test_trigger_entry_waits_with_stop_order_until_price_reached(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_type = EntryType.TRIGGER.value
    trade.entry_price = Decimal("51000")

    request = engine._build_entry_order_request(
        trade=trade,
        current_mark_price=Decimal("50000"),
    )

    assert request.order_type == "STOP"
    assert request.stop_price == Decimal("51000")
    assert request.price is None


def test_trigger_entry_rejects_stale_market_chase(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_type = EntryType.TRIGGER.value
    trade.entry_price = Decimal("50000")

    with pytest.raises(ValueError, match="Trigger entry is stale"):
        engine._build_entry_order_request(
            trade=trade,
            current_mark_price=Decimal("51000"),
        )


@pytest.mark.asyncio
async def test_pending_entry_fill_activates_then_builds_protection(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.LIMIT.value
    trade.entry_order_id = "entry_limit_1"
    trade.entry_order_status = "NEW"
    trade.entry_order_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    trade.requested_stop_loss = Decimal("49000")
    trade.requested_take_profits = [Decimal("51000"), Decimal("52000")]
    engine._open_trades[trade.signal_id] = trade
    context = engine._get_or_create_context(trade.channel_id)
    state = _state(_open_signal(), status=SignalStatus.ORDER_SUBMITTED)
    context.add_signal(state, pending=False)
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
    engine._ensure_trade_protection_after_open = AsyncMock()  # type: ignore[method-assign]
    filled_order = SimpleNamespace(
        order_id="entry_limit_1",
        status="FILLED",
        executed_qty=Decimal("10"),
        avg_price=Decimal("50100"),
        exchange_symbol="BTC-SWAP-USDT",
    )

    activated = await engine._reconcile_pending_entry(
        trade=trade,
        history_orders=[filled_order],
        open_orders=[],
        symbol_user_trades=[],
    )

    assert activated is True
    assert trade.status == "open"
    assert trade.entry_price == Decimal("50100")
    assert trade.quantity == Decimal("0.01")
    assert trade.entry_filled_at is not None
    assert state.status is SignalStatus.OPEN
    assert all(target > trade.entry_price for target in trade.take_profits)
    engine._ensure_trade_protection_after_open.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_expired_pending_entry_is_cancelled_without_opening_position(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.LIMIT.value
    trade.entry_order_id = "entry_expired"
    trade.entry_order_status = "NEW"
    trade.entry_order_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    engine._open_trades[trade.signal_id] = trade
    engine.session.open_positions_count = 1
    context = engine._get_or_create_context(trade.channel_id)
    state = _state(_open_signal(), status=SignalStatus.ORDER_SUBMITTED)
    context.add_signal(state, pending=False)
    engine._futures_client = AsyncMock()
    engine._futures_client.get_order.return_value = SimpleNamespace(
        order_id="entry_expired",
        status="CANCELED",
        executed_qty=Decimal("0"),
    )

    activated = await engine._reconcile_pending_entry(
        trade=trade,
        history_orders=[],
        open_orders=[],
        symbol_user_trades=[],
    )

    assert activated is False
    assert trade.status == "expired"
    assert trade.close_reason == "entry_order_expired"
    assert trade.signal_id not in engine._open_trades
    assert state.status is SignalStatus.EXPIRED
    engine._futures_client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_partial_entry_without_fill_details_blocks_for_reconciliation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.LIMIT.value
    trade.entry_order_id = "entry_partial_cancelled"
    trade.entry_order_status = "PARTIALLY_CANCELED"
    trade.entry_order_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    trade.exchange_position = None
    engine._open_trades[trade.signal_id] = trade
    engine.session.open_positions_count = 1
    context = engine._get_or_create_context(trade.channel_id)
    state = _state(_open_signal(), status=SignalStatus.ORDER_SUBMITTED)
    context.add_signal(state, pending=False)
    engine._futures_client = AsyncMock()
    historical_order = SimpleNamespace(
        order_id=trade.entry_order_id,
        status="PARTIALLY_CANCELED",
        executed_qty=Decimal("56"),
    )

    activated = await engine._reconcile_pending_entry(
        trade=trade,
        history_orders=[historical_order],
        open_orders=[],
        symbol_user_trades=[],
    )

    assert activated is False
    assert trade.status == "waiting_entry"
    assert trade.close_reason is None
    assert trade.signal_id in engine._open_trades
    assert state.status is SignalStatus.ORDER_SUBMITTED
    assert "partial_entry_fill_details_missing" in (trade.last_exchange_sync_error or "")
    assert engine.session.health_status == "critical"
    assert engine.session.new_entries_blocked is True
    engine._futures_client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_exchange_sync_does_not_close_unfilled_pending_entry(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "waiting_entry"
    trade.entry_type = EntryType.LIMIT.value
    trade.entry_order_id = "entry_pending"
    trade.entry_order_type = "LIMIT"
    trade.entry_order_status = "NEW"
    trade.entry_order_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    engine._open_trades[trade.signal_id] = trade
    pending_order = FuturesOrder(
        {
            "orderId": "entry_pending",
            "clientOrderId": "triak_entry_trade_test",
            "symbol": "BTC-SWAP-USDT",
            "side": "BUY_OPEN",
            "type": "LIMIT",
            "status": "NEW",
            "origQty": "10",
            "executedQty": "0",
            "avgPrice": "0",
            "price": "50000",
            "stopPrice": "0",
            "leverage": "10",
        }
    )
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_positions.return_value = []
    engine._futures_client.get_order_history.return_value = [pending_order]
    engine._futures_client.get_open_orders.side_effect = [[pending_order], []]
    engine._futures_client.get_user_trades.return_value = []

    await engine._sync_exchange_state()

    assert trade.status == "waiting_entry"
    assert trade.signal_id in engine._open_trades
    assert trade.close_reason is None
    assert trade.entry_order_status == "NEW"


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
        "exchange_leverage_fallback=50x->25x" in note for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_real_open_position_keeps_risk_sized_quantity_and_uses_feasible_targets(
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
        executed_qty=Decimal("0.1"),
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="ord_btc_5tp",
            exchange_symbol="TBV_BTC-SWAP-TBV_USDT",
            status="FILLED",
            executed_qty=Decimal("0.1"),
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
    engine._futures_client.place_order.return_value = SimpleNamespace(order_id="tp_btc_1")
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
        quantity=Decimal("0.0001"),
        leverage=85,
        use_demo_symbol=True,
    )
    assert trade.quantity == Decimal("0.0001")
    assert trade.tp_order_ids == ["tp_btc_1"]
    assert not any(
        note.startswith("exchange_entry_quantity_promoted=")
        for note in trade.message_history[-1].notes
    )
    assert "exchange_tp_ladder_partial=1/5_due_to_pre_entry_capacity" in (
        trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_real_open_position_rejects_below_minimum_without_increasing_volume(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("4.00"),
        available_balance=Decimal("4.00"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "SOLUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("81.7455")
    trade.quantity = Decimal("0.03434287")
    trade.remaining_quantity = Decimal("0.03434287")
    trade.leverage = 20
    trade.margin = Decimal("0.14036331")
    trade.stop_loss = Decimal("84.20")
    trade.take_profits = [Decimal("79.80")]
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="sol short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "SOL-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )
    engine._futures_client.set_leverage.return_value = {"code": 200}
    with pytest.raises(ValueError) as excinfo:
        await engine._real_open_position(trade)

    assert "refusing to increase volume" in str(excinfo.value)
    engine._futures_client.open_short.assert_not_awaited()
    assert trade.quantity == Decimal("0.03434287")
    assert trade.message_history[-1].notes == []


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
    engine._futures_client.get_contract_spec.return_value = None
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
    engine._futures_client.place_order.side_effect = [
        SimpleNamespace(order_id="tp_doge_1"),
        SimpleNamespace(order_id="tp_doge_2"),
    ]
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        [
            SimpleNamespace(
                order_id="tp_doge_1",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
                price=Decimal("0.07209"),
            ),
            SimpleNamespace(
                order_id="tp_doge_2",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_2_existing",
                stop_price=Decimal("0"),
                price=Decimal("0.07062"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_doge_1",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("0.07656"),
            ),
        ],
    ]
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
        call.kwargs["price"] for call in engine._futures_client.place_order.await_args_list
    ]
    assert submitted_prices == [Decimal("0.07209"), Decimal("0.07062")]
    assert [item.target_index for item in trade.tp_order_plan] == [0, 1]
    assert [item.quantity for item in trade.tp_order_plan] == [
        Decimal("0.00350000"),
        Decimal("0.00650000"),
    ]
    engine._futures_client.set_trading_stop.assert_awaited_once_with(
        symbol="DOGEUSDT",
        side="LONG",
        stop_loss=Decimal("0.07656"),
        sl_quantity=trade.remaining_quantity,
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_sync_trade_protection_places_all_eight_executable_targets(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_price = Decimal("100")
    trade.stop_loss = Decimal("95")
    trade.take_profits = [Decimal(str(price)) for price in range(101, 109)]
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        trade.stop_loss,
        list(trade.take_profits),
    )
    engine._futures_client.get_contract_spec.return_value = None
    engine._futures_client.place_order.side_effect = [
        SimpleNamespace(order_id=f"tp_{index}") for index in range(1, 9)
    ]
    exchange_tp_orders = [
        SimpleNamespace(
            order_id=f"tp_{index}",
            order_type="LIMIT",
            side="SELL_CLOSE",
            client_order_id=f"triak_tp_trade_test_{index}_existing",
            stop_price=Decimal("0"),
            price=Decimal(str(100 + index)),
        )
        for index in range(1, 9)
    ]
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [],
        exchange_tp_orders,
        [
            SimpleNamespace(
                order_id="sl_all_targets",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("95"),
            )
        ],
    ]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}

    await engine._sync_trade_protection(trade)

    assert engine._futures_client.place_order.await_count == 8
    submitted_prices = [
        call.kwargs["price"] for call in engine._futures_client.place_order.await_args_list
    ]
    assert submitted_prices == [Decimal(str(price)) for price in range(101, 109)]
    assert trade.take_profits == [Decimal(str(price)) for price in range(101, 109)]
    assert trade.required_tp_order_count == 8
    assert len(trade.tp_order_ids) == 8


@pytest.mark.asyncio
async def test_sync_trade_protection_rejects_stop_rewrite_over_risk_budget(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_price = Decimal("100")
    trade.quantity = Decimal("12")
    trade.remaining_quantity = Decimal("12")
    trade.balance_at_entry = Decimal("1000")
    trade.stop_loss = Decimal("95")
    trade.take_profits = [Decimal("110")]
    trade.tp_order_ids = ["tp_existing"]
    trade.required_tp_order_count = 1
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="open",
            message_date=datetime.now(timezone.utc),
            action="opened",
        )
    ]
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        Decimal("90"),
        [Decimal("110")],
    )
    engine._futures_client.get_open_orders.side_effect = [
        [],
        [
            SimpleNamespace(
                order_id="tp_existing",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
                price=Decimal("110"),
            ),
        ],
        [
            SimpleNamespace(
                order_id="sl_live_new",
                order_type="STOP_LONG_LOSS",
                side="SELL_CLOSE",
                stop_price=Decimal("95"),
            ),
        ],
    ]
    engine._futures_client.set_trading_stop.return_value = {"ok": True}

    with pytest.raises(ValueError, match="refusing to move the requested stop"):
        await engine._sync_trade_protection(trade, refresh_take_profits=False)

    assert trade.stop_loss == Decimal("95")
    engine._futures_client.set_trading_stop.assert_not_awaited()


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
    engine._futures_client.cancel_order.assert_not_awaited()
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
    trade.take_profits = [Decimal("0.09")]
    trade.tp_order_ids = ["tp_existing"]
    trade.required_tp_order_count = 1
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
        [Decimal("0.09")],
    )
    engine._futures_client.get_open_orders.side_effect = [
        [
            SimpleNamespace(
                order_id="tp_existing",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
                price=Decimal("0.09"),
            ),
        ],
        [],
        [
            SimpleNamespace(
                order_id="tp_existing",
                order_type="LIMIT",
                side="SELL_CLOSE",
                client_order_id="triak_tp_trade_test_1_existing",
                stop_price=Decimal("0"),
                price=Decimal("0.09"),
            ),
        ],
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
async def test_sync_trade_protection_preserves_tiny_tp_ladder_and_places_valid_subset(
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
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
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
    assert trade.take_profits == [
        Decimal("58850"),
        Decimal("58200"),
        Decimal("56980"),
        Decimal("54600"),
    ]
    assert any(
        note == "exchange_tp_ladder_partial=1/4_due_to_contract_size"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_sync_trade_protection_preflight_leaves_existing_orders_when_no_tp_is_executable(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.remaining_quantity = Decimal("0.00001")
    trade.sl_order_id = "sl_existing"
    trade.tp_order_ids = ["tp_existing"]
    engine._futures_client = AsyncMock()
    engine._futures_client.normalize_trade_protection.return_value = (
        trade.stop_loss,
        list(trade.take_profits),
    )
    engine._futures_client.get_contract_spec.return_value = FuturesContractSpec(
        {
            "symbol": "BTC-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
    engine._cancel_existing_trade_protection = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="existing protection was left untouched"):
        await engine._sync_trade_protection(trade)

    engine._cancel_existing_trade_protection.assert_not_awaited()
    assert trade.sl_order_id == "sl_existing"
    assert trade.tp_order_ids == ["tp_existing"]


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
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("49000") + Decimal(index * 500),
            quantity=quantity,
            fraction=fraction,
            order_id=f"entry_{index}",
            status="FILLED" if index == 0 else "NEW",
            executed_quantity=Decimal("0.0025") if index == 0 else Decimal("0"),
        )
        for index, (quantity, fraction) in enumerate(
            (
                (Decimal("0.0025"), Decimal("0.25")),
                (Decimal("0.005"), Decimal("0.50")),
                (Decimal("0.0025"), Decimal("0.25")),
            )
        )
    ]
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
    engine._apply_range_entry_target_cancellation_policy = AsyncMock()  # type: ignore[method-assign]

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
    engine._apply_range_entry_target_cancellation_policy.assert_awaited_once_with(
        trade=trade,
        target_hits=1,
        reason="exchange_tp1_fill",
    )


@pytest.mark.asyncio
async def test_stop_fill_still_cancels_every_pending_range_leg(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=index,
            label=f"range_{index}",
            price=Decimal("49000") + Decimal(index * 500),
            quantity=Decimal("0.003"),
            fraction=Decimal("0.25"),
            order_id=f"entry_{index}",
            status="NEW",
        )
        for index in range(3)
    ]
    engine._futures_client = AsyncMock()
    engine._cancel_range_entry_orders = AsyncMock()  # type: ignore[method-assign]
    stop_order = SimpleNamespace(
        order_id="sl1",
        executed_order_id="close_sl1",
        order_type="STOP_LONG_LOSS",
    )

    with pytest.raises(ValueError, match="no userTrades"):
        await engine._apply_exchange_protection_fill(
            trade=trade,
            protection_order=stop_order,
            symbol_user_trades=[],
        )

    engine._cancel_range_entry_orders.assert_awaited_once_with(
        trade=trade,
        reason="exchange_stop_loss_fill",
        reconcile_fills=True,
        ensure_protection=False,
    )


@pytest.mark.asyncio
async def test_reconcile_exchange_trade_protection_applies_multiple_tp_fills_before_rearming(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.remaining_quantity = Decimal("1")
    trade.tp_order_ids = ["tp1", "tp2", "tp3"]
    trade.tp_order_plan = [
        LiveTakeProfitOrderPlan(
            target_index=0,
            price=Decimal("51000"),
            quantity=Decimal("0.35"),
            order_id="tp1",
            client_order_id="triak_tp_trade_test_1_a",
        ),
        LiveTakeProfitOrderPlan(
            target_index=1,
            price=Decimal("52000"),
            quantity=Decimal("0.26"),
            order_id="tp2",
            client_order_id="triak_tp_trade_test_2_b",
        ),
        LiveTakeProfitOrderPlan(
            target_index=2,
            price=Decimal("53000"),
            quantity=Decimal("0.39"),
            order_id="tp3",
            client_order_id="triak_tp_trade_test_3_c",
        ),
    ]
    trade.sl_order_id = "sl1"
    trade.take_profits = [Decimal("51000"), Decimal("52000"), Decimal("53000")]
    engine._futures_client = AsyncMock()
    engine._futures_client.get_order.side_effect = [
        SimpleNamespace(
            order_id="tp2",
            executed_order_id="close_tp2",
            order_type="LIMIT",
            status="ORDER_FILLED",
        ),
        SimpleNamespace(
            order_id="tp1",
            executed_order_id="close_tp1",
            order_type="LIMIT",
            status="ORDER_FILLED",
        ),
    ]
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1")
    )
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(
            close_fraction=Decimal("0.35"),
            move_sl_to_entry=True,
            new_stop_loss=None,
        ),
        SimpleNamespace(
            close_fraction=Decimal("0.40"),
            move_sl_to_entry=False,
            new_stop_loss=Decimal("51000"),
        ),
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[SimpleNamespace(order_id="tp3")],
        open_protection_orders=[],
        symbol_user_trades=[
            SimpleNamespace(
                order_id="close_tp1",
                qty=Decimal("0.35"),
                realized_pnl=Decimal("1.25"),
                commission=Decimal("0.01"),
                price=Decimal("51000"),
            ),
            SimpleNamespace(
                order_id="close_tp2",
                qty=Decimal("0.26"),
                realized_pnl=Decimal("1.75"),
                commission=Decimal("0.02"),
                price=Decimal("52000"),
            ),
        ],
    )

    assert trade.realized_pnl == Decimal("3.00")
    assert trade.fees == Decimal("0.03")
    assert trade.remaining_quantity == Decimal("0.39")
    assert trade.targets_hit == 2
    assert trade.stop_loss == Decimal("51000")
    assert trade.tp_order_ids == ["tp3"]
    assert [item.order_id for item in trade.tp_order_plan] == ["tp3"]
    engine._sync_trade_protection.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_protection_fills_do_not_double_reduce_snapshot_clamped_quantity(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "symbol": "BICOUSDT",
            "side": "short",
            "entry_price": Decimal("0.0166"),
            "quantity": Decimal("1966"),
            "remaining_quantity": Decimal("767"),
            "pending_fill_audit_quantity": Decimal("1199"),
            "stop_loss": Decimal("0.01777"),
            "take_profits": [Decimal("0.01638"), Decimal("0.01630"), Decimal("0.01613")],
            "tp_order_ids": ["tp1", "tp2", "tp3"],
            "tp_order_plan": [
                LiveTakeProfitOrderPlan(
                    target_index=index,
                    price=price,
                    quantity=quantity,
                    order_id=f"tp{index + 1}",
                )
                for index, (price, quantity) in enumerate(
                    (
                        (Decimal("0.01638"), Decimal("688")),
                        (Decimal("0.01630"), Decimal("511")),
                        (Decimal("0.01613"), Decimal("767")),
                    )
                )
            ],
        }
    )
    engine._futures_client = AsyncMock()
    engine._futures_client.get_order.side_effect = [
        SimpleNamespace(
            order_id="tp1",
            executed_order_id="tp1",
            order_type="LIMIT",
            status="ORDER_FILLED",
        ),
        SimpleNamespace(
            order_id="tp2",
            executed_order_id="tp2",
            order_type="LIMIT",
            status="ORDER_FILLED",
        ),
    ]
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1")
    )
    engine._strategy.get_target_hit_action.side_effect = [
        SimpleNamespace(
            close_fraction=Decimal("0.35"),
            move_sl_to_entry=True,
            new_stop_loss=None,
        ),
        SimpleNamespace(
            close_fraction=Decimal("0.40"),
            move_sl_to_entry=False,
            new_stop_loss=Decimal("0.01638"),
        ),
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[SimpleNamespace(order_id="tp3")],
        open_protection_orders=[],
        symbol_user_trades=[
            SimpleNamespace(
                trade_id="fill1",
                order_id="tp1",
                qty=Decimal("688"),
                realized_pnl=Decimal("0.15136"),
                commission=Decimal("0.00225388"),
                price=Decimal("0.01638"),
            ),
            SimpleNamespace(
                trade_id="fill2",
                order_id="tp2",
                qty=Decimal("511"),
                realized_pnl=Decimal("0.1533"),
                commission=Decimal("0.00166586"),
                price=Decimal("0.01630"),
            ),
        ],
    )

    assert trade.status == "partial_close"
    assert trade.remaining_quantity == Decimal("767")
    assert trade.pending_fill_audit_quantity == Decimal("0")
    assert trade.targets_hit == 2
    assert trade.stop_loss == Decimal("0.01638")
    assert trade.realized_pnl == Decimal("0.30466")
    assert trade.fees == Decimal("0.00391974")
    assert trade.tp_order_ids == ["tp3"]
    engine._sync_trade_protection.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_missing_exchange_position_reconciles_stop_fill_and_real_pnl(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(
        _open_signal().model_copy(update={"symbol": "BANKUSDT", "side": TradeSide.SHORT})
    )
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "symbol": "BANKUSDT",
            "side": "short",
            "entry_price": Decimal("0.19514"),
            "quantity": Decimal("511"),
            "remaining_quantity": Decimal("511"),
            "stop_loss": Decimal("0.209736"),
            "take_profits": [Decimal("0.19334"), Decimal("0.19160")],
        }
    )
    context.add_signal(state, pending=False)
    engine._open_trades[trade.signal_id] = trade
    engine.session.open_positions_count = 1
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        tick_size=Decimal("0.00001"),
    )
    fill = SimpleNamespace(
        trade_id="bank_stop_fill",
        order_id="bank_market_close",
        time=int(datetime.now(timezone.utc).timestamp() * 1000),
        side="BUY_CLOSE",
        order_type="MARKET",
        qty=Decimal("511"),
        price=Decimal("0.19993"),
        realized_pnl=Decimal("-2.44769"),
        commission=Decimal("0.100"),
    )

    reconciled = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=[fill],
    )

    assert reconciled is True
    assert trade.status == "closed"
    assert trade.close_reason == "exchange_close_reconciled"
    assert trade.remaining_quantity == Decimal("0")
    assert trade.realized_pnl == Decimal("-2.44769")
    assert trade.fees == Decimal("0.100")
    assert trade.exit_price == Decimal("0.19993")
    assert trade.processed_exchange_fill_ids == ["trade:bank_stop_fill"]
    assert trade.signal_id not in engine._open_trades
    assert engine.session.losses == 1


@pytest.mark.asyncio
async def test_missing_exchange_position_reconciles_tp_then_stop_without_double_counting(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(
        _open_signal().model_copy(update={"symbol": "BANKUSDT", "side": TradeSide.SHORT})
    )
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "symbol": "BANKUSDT",
            "side": "short",
            "entry_price": Decimal("0.19523"),
            "quantity": Decimal("511"),
            "remaining_quantity": Decimal("511"),
            "take_profits": [Decimal("0.19334"), Decimal("0.19160")],
            "tp_order_ids": ["bank_tp_1"],
            "tp_order_plan": [
                LiveTakeProfitOrderPlan(
                    target_index=0,
                    price=Decimal("0.19334"),
                    quantity=Decimal("278"),
                    order_id="bank_tp_1",
                )
            ],
        }
    )
    context.add_signal(state, pending=False)
    engine._open_trades[trade.signal_id] = trade
    engine.session.open_positions_count = 1
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        tick_size=Decimal("0.00001"),
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fills = [
        SimpleNamespace(
            trade_id="bank_tp_fill",
            order_id="bank_tp_1",
            time=now_ms,
            side="BUY_CLOSE",
            order_type="LIMIT",
            qty=Decimal("278"),
            price=Decimal("0.19334"),
            realized_pnl=Decimal("0.52542"),
            commission=Decimal("0.020"),
        ),
        SimpleNamespace(
            trade_id="bank_stop_after_tp",
            order_id="bank_market_close",
            time=now_ms + 1,
            side="BUY_CLOSE",
            order_type="MARKET",
            qty=Decimal("233"),
            price=Decimal("0.19983"),
            realized_pnl=Decimal("-1.07180"),
            commission=Decimal("0.025"),
        ),
    ]

    reconciled = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=fills,
    )
    repeated = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=fills,
    )

    assert reconciled is True
    assert repeated is False
    assert trade.status == "closed"
    assert trade.close_reason == "exchange_close_reconciled"
    assert trade.targets_hit == 1
    assert trade.realized_pnl == Decimal("-0.54638")
    assert trade.fees == Decimal("0.045")
    assert trade.exit_price == Decimal("0.19983")
    assert len(trade.processed_exchange_fill_ids) == 2
    assert engine.session.total_realized_pnl == Decimal("-0.54638")
    assert engine.session.total_fees == Decimal("0.045")


@pytest.mark.asyncio
async def test_owned_duplicate_tp_fills_reconcile_while_position_still_exists(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "symbol": "SOLUSDT",
            "quantity": Decimal("0.43"),
            "remaining_quantity": Decimal("0.43"),
            "take_profits": [Decimal("72.53"), Decimal("75")],
        }
    )
    engine._open_trades[trade.signal_id] = trade
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        tick_size=Decimal("0.01"),
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history = [
        SimpleNamespace(
            order_id="sol_tp_first",
            client_order_id="triak_tp_trade_test_1_first",
        ),
        SimpleNamespace(
            order_id="sol_tp_rearmed",
            client_order_id="triak_tp_trade_test_1_rearmed",
        ),
    ]
    fills = [
        SimpleNamespace(
            trade_id="sol_fill_first",
            order_id="sol_tp_first",
            time=now_ms,
            side="SELL_CLOSE",
            order_type="LIMIT",
            qty=Decimal("0.15"),
            price=Decimal("72.53"),
            realized_pnl=Decimal("0.30"),
            commission=Decimal("0.01"),
        ),
        SimpleNamespace(
            trade_id="sol_fill_rearmed",
            order_id="sol_tp_rearmed",
            time=now_ms + 1,
            side="SELL_CLOSE",
            order_type="LIMIT",
            qty=Decimal("0.15"),
            price=Decimal("72.53"),
            realized_pnl=Decimal("0.31"),
            commission=Decimal("0.01"),
        ),
    ]

    reconciled = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=fills,
        history_orders=history,
        require_owned_order=True,
    )
    repeated = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=fills,
        history_orders=history,
        require_owned_order=True,
    )

    assert reconciled is True
    assert repeated is False
    assert trade.status == "partial_close"
    assert trade.remaining_quantity == Decimal("0.13")
    assert trade.targets_hit == 1
    assert trade.realized_pnl == Decimal("0.61")
    assert trade.fees == Decimal("0.02")
    assert len(trade.processed_exchange_fill_ids) == 2


@pytest.mark.asyncio
async def test_delayed_fills_do_not_reduce_quantity_twice_after_snapshot_clamp(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "symbol": "SOLUSDT",
            "side": "short",
            "entry_price": Decimal("72.93"),
            "stop_loss": Decimal("78.99"),
            "quantity": Decimal("0.43"),
            "remaining_quantity": Decimal("0.43"),
            "take_profits": [Decimal("72.53")],
        }
    )
    engine._open_trades[trade.signal_id] = trade
    engine._futures_client = AsyncMock()
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1"),
        tick_size=Decimal("0.01"),
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
            new_stop_loss=Decimal("72.53"),
        ),
    ]
    assert engine._reconcile_trade_quantity_to_exchange(
        trade=trade,
        exchange_quantity=Decimal("0.13"),
    )
    assert trade.pending_fill_audit_quantity == Decimal("0.30")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history = [
        SimpleNamespace(
            order_id=f"sol_tp_{index}",
            client_order_id=f"triak_tp_trade_test_{index}_recovered",
        )
        for index in (1, 2)
    ]
    fills = [
        SimpleNamespace(
            trade_id=f"sol_delayed_{index}",
            order_id=f"sol_tp_{index}",
            time=now_ms + index,
            side="BUY_CLOSE",
            order_type="LIMIT",
            qty=Decimal("0.15"),
            price=Decimal("72.53"),
            realized_pnl=Decimal("0.30"),
            commission=Decimal("0.01"),
        )
        for index in (1, 2)
    ]

    reconciled = await engine._reconcile_missing_exchange_position_fills(
        trade=trade,
        symbol_user_trades=fills,
        history_orders=history,
        require_owned_order=True,
    )

    assert reconciled is True
    assert trade.remaining_quantity == Decimal("0.13")
    assert trade.pending_fill_audit_quantity == Decimal("0")
    assert trade.realized_pnl == Decimal("0.60")
    assert trade.fees == Decimal("0.02")
    assert trade.targets_hit == 2
    assert trade.stop_loss == Decimal("72.53")
    assert engine.session.health_status == "healthy"
    assert engine.session.new_entries_blocked is False
    assert (
        engine._account_coordinator.bucket_block_reason(trade, trading_mode="demo")
        is None
    )


def test_position_snapshot_converts_contracts_to_asset_quantity(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    position = SimpleNamespace(
        symbol_internal="SOLUSDT",
        exchange_symbol="SOL-SWAP-USDT",
        side="LONG",
        position=Decimal("430"),
        available=Decimal("130"),
        avg_price=Decimal("70"),
        mark_price=Decimal("71"),
        leverage=10,
        unrealized_pnl=Decimal("1"),
        realized_pnl=Decimal("0"),
        margin=Decimal("3"),
        margin_type="CROSS",
    )

    snapshot = engine._position_snapshot(
        position,
        spec=SimpleNamespace(contract_multiplier=Decimal("0.001")),
    )

    assert snapshot.quantity == Decimal("0.430")
    assert snapshot.available == Decimal("0.130")


def test_protection_circuit_uses_exponential_backoff_and_blocks_entries(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)

    engine._schedule_protection_retry(trade, RuntimeError("protection failed"))
    first_retry = trade.protection_retry_at
    engine._schedule_protection_retry(trade, RuntimeError("protection failed again"))

    assert first_retry is not None
    assert trade.protection_retry_at is not None
    assert trade.protection_retry_at > first_retry
    assert trade.consecutive_protection_failures == 2
    assert engine._protection_retry_is_deferred(trade) is True
    assert engine.session.health_status == "critical"
    assert engine.session.new_entries_blocked is True
    assert engine._account_coordinator.bucket_block_reason(trade, trading_mode="demo")


def test_finalizing_trade_clears_trade_scoped_health_blocks(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    engine._open_trades[trade.signal_id] = trade
    issues = [
        f"protection unavailable for {trade.trade_id}: invalid quantity",
        f"exchange quantity drift for {trade.trade_id} requires fill audit: pending=1",
        f"exchange reconciliation required for {trade.trade_id}: missing fills",
    ]
    for issue in issues:
        engine._account_coordinator.block_trade_bucket(
            trade,
            trading_mode=engine.session.trading_mode,
            reason=issue,
        )
        engine._set_session_health_issue(issue, critical=True)
    trade.status = "closed"
    trade.remaining_quantity = Decimal("0")

    engine._finalize_closed_trade(trade)

    assert engine.session.health_issues == []
    assert engine.session.health_status == "healthy"
    assert engine.session.new_entries_blocked is False
    assert (
        engine._account_coordinator.bucket_block_reason(
            trade,
            trading_mode=engine.session.trading_mode,
        )
        is None
    )


def test_restore_prunes_stale_health_issue_for_terminal_trade(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "closed"
    trade.remaining_quantity = Decimal("0")
    engine.store.save_trade(trade)
    stale_issue = f"protection unavailable for {trade.trade_id}: invalid quantity"
    active_issue = "recovery-only: reconcile pre-existing exposure"
    engine.session.health_issues = [stale_issue, active_issue]
    engine.session.health_status = "critical"
    engine.session.new_entries_blocked = True
    engine.session.last_error = stale_issue
    engine.store.save_session(engine.session)

    engine._prune_terminal_trade_health_issues()

    assert engine.session.health_issues == [active_issue]
    assert engine.session.health_status == "critical"
    assert engine.session.new_entries_blocked is True
    assert engine.session.last_error == active_issue


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


@pytest.mark.asyncio
async def test_owned_v2_stop_cancellation_only_cancels_trade_owned_stop(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine.settings.LIVE_TRADING_USE_OWNED_V2_STOP_ORDERS = True
    trade = _trade(engine.session.session_id)
    trade.sl_order_id = "sl_owned"
    trade.sl_order_client_id = "triak_sl_trade_test_owned"
    trade.sl_order_api_version = "v2"
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_orders.return_value = []
    engine._futures_client.get_open_algo_orders.return_value = [
        SimpleNamespace(
            order_id="sl_other",
            client_order_id="triak_sl_other_trade_owned",
        ),
        SimpleNamespace(
            order_id="sl_owned",
            client_order_id="triak_sl_trade_test_owned",
        ),
    ]

    await engine._cancel_existing_trade_protection(trade)

    engine._futures_client.cancel_algo_order.assert_awaited_once_with(
        "sl_owned",
        use_demo_symbol=True,
    )
    engine._futures_client.cancel_order.assert_not_awaited()
    assert trade.sl_order_id is None
    assert trade.sl_order_client_id is None
    assert trade.sl_order_api_version is None


@pytest.mark.asyncio
async def test_full_close_cleanup_cancels_only_unowned_triak_tp_reservations(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    other = trade.model_copy(
        update={
            "trade_id": "trade_other",
            "signal_id": "sig_other",
            "channel_id": "@other",
            "tp_order_ids": ["tp_active_other"],
        }
    )
    engine._account_coordinator.register_trade(other, trading_mode="demo")
    engine._futures_client = AsyncMock()
    engine._futures_client.get_open_orders.return_value = [
        SimpleNamespace(
            order_id="tp_orphan",
            client_order_id="triak_tp_trade_closed_1_deadbeef",
            order_type="LIMIT",
            side="SELL_CLOSE",
            orig_qty=Decimal("0.01"),
            price=Decimal("51000"),
        ),
        SimpleNamespace(
            order_id="tp_active_other",
            client_order_id="triak_tp_trade_other_1_deadbeef",
            order_type="LIMIT",
            side="SELL_CLOSE",
            orig_qty=Decimal("0.01"),
            price=Decimal("52000"),
        ),
        SimpleNamespace(
            order_id="manual_order",
            client_order_id="manual-close-order",
            order_type="LIMIT",
            side="SELL_CLOSE",
            orig_qty=Decimal("0.01"),
            price=Decimal("53000"),
        ),
    ]

    canceled = await engine._cancel_unowned_take_profit_reservations_for_full_close(trade)

    assert canceled == 1
    engine._futures_client.cancel_order.assert_awaited_once_with(
        symbol="BTCUSDT",
        order_id="tp_orphan",
        use_demo_symbol=True,
    )


@pytest.mark.asyncio
async def test_full_logical_close_does_not_flatten_other_same_side_leg(
    tmp_path: Path,
) -> None:
    coordinator = AccountExecutionCoordinator(symbol_risk_cap_pct=Decimal("0"))
    settings = _settings()
    engine = LiveTradingEngine(
        settings=settings,
        session=_session(),
        store=LiveTradingStore(tmp_path),
        account_coordinator=coordinator,
    )
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "quantity": Decimal("1"),
            "remaining_quantity": Decimal("1"),
            "balance_at_entry": Decimal("1000"),
        }
    )
    trade.entry_order_plan = [
        LiveEntryOrderPlan(
            leg_index=2,
            label="range_end",
            price=Decimal("52000"),
            quantity=Decimal("0.25"),
            fraction=Decimal("0.25"),
            order_id="pending_range_end",
            status="NEW",
        )
    ]
    trade.entry_filled_quantity = Decimal("1")
    other = trade.model_copy(
        update={
            "trade_id": "trade_other",
            "signal_id": "sig_other",
            "channel_id": "@other",
            "entry_order_plan": [],
            "entry_filled_quantity": Decimal("0"),
        }
    )
    coordinator.register_trade(trade, trading_mode="demo")
    coordinator.register_trade(other, trading_mode="demo")
    engine._futures_client = AsyncMock()
    engine._cancel_range_entry_orders = AsyncMock()  # type: ignore[method-assign]
    engine._cancel_existing_trade_protection = AsyncMock()  # type: ignore[method-assign]
    engine._submit_exchange_close_order = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(order_id="close_owned"), Decimal("1"))
    )
    fill = SimpleNamespace(
        trade_id="fill_owned",
        order_id="close_owned",
        qty=Decimal("1"),
        price=Decimal("51000"),
        realized_pnl=Decimal("10"),
        commission=Decimal("0.1"),
        time=1,
        side="SELL_CLOSE",
    )
    engine._futures_client.wait_for_order_fill.return_value = (
        SimpleNamespace(
            order_id="close_owned",
            status="FILLED",
            executed_qty=Decimal("1"),
            avg_price=Decimal("51000"),
        ),
        [fill],
    )
    engine._futures_client.get_contract_spec.return_value = SimpleNamespace(
        contract_multiplier=Decimal("1")
    )
    engine._fetch_trade_exchange_position_quantity = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(), Decimal("1"))
    )
    engine._refresh_account = AsyncMock()  # type: ignore[method-assign]

    result = await engine._execute_exchange_close(
        trade=trade,
        fraction=Decimal("1"),
        reason="owned_logical_close",
    )

    assert result.executed_quantity == Decimal("1")
    assert trade.status == "closed"
    assert coordinator.bucket_logical_quantity(
        trading_mode="demo",
        symbol=other.symbol,
        side=other.side,
    ) == Decimal("1")
    engine._fetch_trade_exchange_position_quantity.assert_not_awaited()
    engine._submit_exchange_close_order.assert_awaited_once()
    engine._cancel_range_entry_orders.assert_awaited_once_with(
        trade=trade,
        reason="exit_started:owned_logical_close",
        reconcile_fills=True,
        ensure_protection=False,
    )


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
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
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


def _target_hit_action_for_promoted_ladder(**kwargs: object) -> SimpleNamespace:
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


def test_plan_exchange_take_profit_ladder_uses_maximum_count_for_fixed_quantity(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.side = "short"
    trade.entry_price = Decimal("4185")
    trade.quantity = Decimal("6")
    trade.remaining_quantity = Decimal("6")
    trade.take_profits = [
        Decimal("4180"),
        Decimal("4177"),
        Decimal("4174"),
        Decimal("4171"),
        Decimal("4168"),
        Decimal("4165"),
        Decimal("4162"),
    ]
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
    spec = FuturesContractSpec(
        {
            "symbol": "XAU-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    plan = engine._plan_exchange_take_profit_ladder(
        trade=trade,
        spec=spec,
    )

    assert plan is not None
    assert plan.original_count == 7
    assert plan.target_count == 6
    assert plan.pending_take_profits == trade.take_profits
    assert plan.required_quantity == Decimal("6.00000000")


def test_plan_exchange_take_profit_ladder_falls_back_to_three_then_one_when_needed(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.side = "short"
    trade.entry_price = Decimal("4185")
    trade.quantity = Decimal("1")
    trade.remaining_quantity = Decimal("1")
    trade.take_profits = [
        Decimal("4180"),
        Decimal("4177"),
        Decimal("4174"),
        Decimal("4171"),
        Decimal("4168"),
        Decimal("4165"),
        Decimal("4162"),
    ]
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_by_remaining
    spec = FuturesContractSpec(
        {
            "symbol": "XAU-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    trade.quantity = Decimal("3")
    trade.remaining_quantity = Decimal("3")
    plan_three = engine._plan_exchange_take_profit_ladder(trade=trade, spec=spec)
    trade.quantity = Decimal("1")
    trade.remaining_quantity = Decimal("1")
    plan_one = engine._plan_exchange_take_profit_ladder(
        trade=trade,
        spec=spec,
    )

    assert plan_three is not None
    assert plan_three.target_count == 3
    assert plan_three.pending_take_profits == trade.take_profits
    assert plan_three.required_quantity == Decimal("3.00000000")

    assert plan_one is not None
    assert plan_one.target_count == 1
    assert plan_one.pending_take_profits == trade.take_profits
    assert plan_one.required_quantity == Decimal("1.00000000")


def test_bank_tp_ladder_uses_dynamic_subset_without_increasing_quantity() -> None:
    engine = _engine(Path("/tmp"))
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("100"),
        available_balance=Decimal("100"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "BANKUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("0.19953")
    trade.leverage = 20
    trade.quantity = Decimal("138")
    trade.remaining_quantity = Decimal("138")
    trade.margin = Decimal("1.376757")
    trade.take_profits = [
        Decimal("0.19323"),
        Decimal("0.19226"),
        Decimal("0.19032"),
        Decimal("0.18838"),
        Decimal("0.18644"),
        Decimal("0.18449"),
        Decimal("0.18255"),
        Decimal("0.18061"),
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
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_for_promoted_ladder
    spec = FuturesContractSpec(
        {
            "symbol": "BANK-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "100",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    engine._ensure_exchange_quantity_supports_take_profit_ladder(trade, spec)

    assert trade.quantity == Decimal("138")
    assert trade.remaining_quantity == Decimal("138")
    assert trade.margin == Decimal("1.376757")
    assert trade.required_tp_order_count == 1
    assert trade.take_profits == [
        Decimal("0.19323"),
        Decimal("0.19226"),
        Decimal("0.19032"),
        Decimal("0.18838"),
        Decimal("0.18644"),
        Decimal("0.18449"),
        Decimal("0.18255"),
        Decimal("0.18061"),
    ]
    assert "exchange_tp_ladder_partial=1/8_due_to_pre_entry_capacity" in (
        trade.message_history[-1].notes
    )


def test_bank_tp_ladder_places_maximum_orders_for_fixed_quantity() -> None:
    engine = _engine(Path("/tmp"))
    trade = _trade(engine.session.session_id)
    trade.symbol = "BANKUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("0.19953")
    trade.quantity = Decimal("350")
    trade.remaining_quantity = Decimal("350")
    trade.take_profits = [
        Decimal("0.19323"),
        Decimal("0.19226"),
        Decimal("0.19032"),
        Decimal("0.18838"),
        Decimal("0.18644"),
        Decimal("0.18449"),
        Decimal("0.18255"),
        Decimal("0.18061"),
    ]
    engine._strategy.get_target_hit_action.side_effect = _target_hit_action_for_promoted_ladder
    spec = FuturesContractSpec(
        {
            "symbol": "BANK-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "100",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    orders = engine._exchange_take_profit_orders(trade, spec=spec)

    assert len(orders) == 3
    assert [target_index for target_index, _, _ in orders] == [0, 1, 2]
    assert all(quantity >= Decimal("100") for _, _, quantity in orders)
    assert sum((quantity for _, _, quantity in orders), Decimal("0")) == Decimal("350")
    assert trade.quantity == Decimal("350")
    assert trade.remaining_quantity == Decimal("350")


def test_market_entry_at_exact_minimum_keeps_risk_sized_quantity() -> None:
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

    assert trade.quantity == Decimal("0.0001")
    assert trade.remaining_quantity == Decimal("0.0001")
    assert trade.margin == Decimal("0.07072741")
    assert trade.message_history[-1].notes == []


def test_market_entry_below_minimum_is_rejected_without_volume_change() -> None:
    engine = _engine(Path("/tmp"))
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("5.00"),
        available_balance=Decimal("5.00"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "SOLUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("81.7455")
    trade.leverage = 20
    trade.quantity = Decimal("0.03434287")
    trade.remaining_quantity = Decimal("0.03434287")
    trade.margin = Decimal("0.14036331")
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="sol short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    spec = FuturesContractSpec(
        {
            "symbol": "SOL-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="refusing to increase volume"):
        engine._ensure_exchange_quantity_supports_market_entry(trade, spec)

    assert trade.quantity == Decimal("0.03434287")
    assert trade.remaining_quantity == Decimal("0.03434287")
    assert trade.margin == Decimal("0.14036331")
    assert trade.message_history[-1].notes == []


def test_market_entry_below_minimum_does_not_defer_rejection_to_exchange() -> None:
    engine = _engine(Path("/tmp"))
    engine.session.account_info = LiveAccountInfo(
        wallet_balance=Decimal("4.00"),
        available_balance=Decimal("4.00"),
    )
    trade = _trade(engine.session.session_id)
    trade.symbol = "SOLUSDT"
    trade.side = "short"
    trade.entry_price = Decimal("81.7455")
    trade.leverage = 20
    trade.quantity = Decimal("0.03434287")
    trade.remaining_quantity = Decimal("0.03434287")
    trade.margin = Decimal("0.14036331")
    trade.message_history = [
        MessageAttribution(
            message_id=1,
            channel_id="@testchan",
            channel_label="@testchan",
            message_preview="sol short",
            message_date=datetime.now(timezone.utc),
            action="opened",
            notes=[],
        )
    ]
    spec = FuturesContractSpec(
        {
            "symbol": "SOL-SWAP-USDT",
            "status": "TRADING",
            "apiStatus": "TRADING",
            "contractMultiplier": "0.1",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "1",
                    "maxQty": "1000000",
                    "stepSize": "1",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="refusing to increase volume"):
        engine._ensure_exchange_quantity_supports_market_entry(trade, spec)

    assert trade.quantity == Decimal("0.03434287")
    assert trade.remaining_quantity == Decimal("0.03434287")
    assert trade.margin == Decimal("0.14036331")
    assert trade.message_history[-1].notes == []


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


def test_strategy_stop_reconciliation_repairs_multi_target_progress(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._strategy = TrailingTakeProfitStrategy(risk_free_on_first_tp=True)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "side": "long",
            "entry_price": Decimal("100"),
            "stop_loss": Decimal("90"),
            "take_profits": [
                Decimal("110"),
                Decimal("120"),
                Decimal("130"),
                Decimal("140"),
            ],
            "targets_hit": 3,
        }
    )

    changed = engine._reconcile_strategy_stop_loss_after_targets(trade)

    assert changed is True
    assert trade.stop_loss == Decimal("120")


def test_strategy_stop_reconciliation_uses_two_target_gap_for_large_ladder(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._strategy = TrailingTakeProfitStrategy(risk_free_on_first_tp=True)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "side": "long",
            "entry_price": Decimal("100"),
            "stop_loss": Decimal("90"),
            "take_profits": [
                Decimal("110"),
                Decimal("120"),
                Decimal("130"),
                Decimal("140"),
                Decimal("150"),
            ],
            "targets_hit": 3,
        }
    )

    changed = engine._reconcile_strategy_stop_loss_after_targets(trade)

    assert changed is True
    assert trade.stop_loss == Decimal("110")


def test_strategy_stop_reconciliation_never_loosens_existing_stop(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._strategy = TrailingTakeProfitStrategy(risk_free_on_first_tp=True)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "side": "short",
            "entry_price": Decimal("100"),
            "stop_loss": Decimal("75"),
            "take_profits": [
                Decimal("90"),
                Decimal("80"),
                Decimal("70"),
            ],
            "targets_hit": 2,
        }
    )

    changed = engine._reconcile_strategy_stop_loss_after_targets(trade)

    assert changed is False
    assert trade.stop_loss == Decimal("75")


def test_exchange_target_recovery_applies_large_ladder_gap_after_third_tp(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine._strategy = TrailingTakeProfitStrategy(risk_free_on_first_tp=True)
    trade = _trade(engine.session.session_id).model_copy(
        update={
            "side": "long",
            "entry_price": Decimal("100"),
            "stop_loss": Decimal("90"),
            "take_profits": [
                Decimal("110"),
                Decimal("120"),
                Decimal("130"),
                Decimal("140"),
                Decimal("150"),
            ],
        }
    )
    attribution = MessageAttribution(
        message_id=0,
        channel_id=trade.channel_id,
        channel_label=trade.channel_label,
        message_preview="recovered TP3 fill",
        message_date=datetime.now(timezone.utc),
        action="partial_close",
    )

    engine._apply_strategy_after_exchange_target_fill(
        trade=trade,
        target_index=2,
        attribution=attribution,
    )

    assert trade.targets_hit == 3
    assert trade.stop_loss == Decimal("110")
    assert "next_stop_loss=110" in attribution.notes


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
async def test_handle_followup_invalid_tp_update_keeps_existing_targets(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    original_targets = list(trade.take_profits)
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade

    parsed = _open_signal(action=SignalAction.UPDATE_TP).model_copy(
        update={"stop_loss": None, "take_profits": [Decimal("40"), Decimal("80")]}
    )
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=24,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(24, "tp 40% 80%"),
        context=context,
        trace=trace,
    )

    assert trace.final_status == "invalid_tp_update_ignored"
    assert trade.take_profits == original_targets
    assert "TP update ignored: no valid price targets" in trade.message_history[-1].notes


@pytest.mark.asyncio
async def test_handle_followup_update_tp_preserves_explicit_targets_below_synthetic_floor(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    context = engine._get_or_create_context("@testchan")
    state = _state(_open_signal())
    trade = _trade(engine.session.session_id)
    trade.entry_price = Decimal("50000")
    trade.stop_loss = Decimal("49000")
    trade.take_profits = [Decimal("51000"), Decimal("52000")]
    context.add_signal(state, pending=False)
    engine._open_trades["sig_test"] = trade
    engine._sync_signal_snapshot(context=context, state=state, trade=trade)
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=122,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    parsed = _open_signal(action=SignalAction.UPDATE_TP).model_copy(
        update={"take_profits": [Decimal("50100"), Decimal("50500")]}
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(122, "tp 50100 50500"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trace.final_status == "updated_tp"
    assert trade.take_profits == [Decimal("50100"), Decimal("50500")]
    assert trace.effect_summary == "SL=49000 · TPs=['50100', '50500']"
    assert signal is not None
    assert signal.take_profits == [Decimal("50100"), Decimal("50500")]
    assert "tp1_min_profit_pct=1.5" not in trade.message_history[-1].notes


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
    assert trade.last_exchange_sync_error is None
    assert trace.final_status == "update_sl_failed"
    assert "Duplicate clientOrderId" in (trace.effect_summary or "")
    assert signal is not None
    assert signal.stop_loss == Decimal("49000")
    assert engine._sync_trade_protection.await_args_list[0].kwargs == {
        "refresh_take_profits": False,
        "refresh_stop_loss": True,
    }
    assert engine._sync_trade_protection.await_args_list[1].kwargs == {}


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
async def test_process_message_auto_closes_missing_trade_and_accepts_new_signal(
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
    engine._futures_client.get_user_trades.return_value = []
    engine._futures_client.get_order_history.return_value = []
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
    assert trade_reloaded.remaining_quantity == Decimal("0")
    assert trade_reloaded.pending_fill_audit_quantity == Decimal("0.01")
    assert "sig_test" not in engine._open_trades
    assert traces[0].final_status == "pending_consolidation"
    assert any(
        note.startswith("related_signal_detached_confirmed_exchange_position_missing=")
        for note in traces[0].debug_notes
    )
    assert engine.session.new_entries_blocked is False


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
async def test_handle_followup_auto_closes_stale_exchange_trade_without_blocking(
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
    engine._futures_client.get_user_trades.return_value = []
    engine._futures_client.get_order_history.return_value = []
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
    assert engine.session.new_entries_blocked is False


@pytest.mark.asyncio
async def test_sync_exchange_state_auto_closes_confirmed_missing_position_without_fills(
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
    stale_issue = (
        f"exchange reconciliation required for {trade.trade_id}: "
        "followup_exchange_position_missing; exchange position is missing and no complete "
        "owned close-fill history was available"
    )
    engine._account_coordinator.block_trade_bucket(
        trade,
        trading_mode=engine.session.trading_mode,
        reason=stale_issue,
    )
    engine._set_session_health_issue(stale_issue, critical=True)

    await engine._sync_exchange_state()

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    trade_reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    assert signal is not None
    assert signal.status == "closed"
    assert trade_reloaded is not None
    assert trade_reloaded.status == "closed"
    assert trade_reloaded.close_reason == (
        "confirmed_exchange_position_missing:exchange_position_missing_snapshot"
    )
    assert trade_reloaded.last_exchange_sync_error is None
    assert trade_reloaded.pending_fill_audit_quantity == Decimal("0.01")
    assert engine._open_trades == {}
    assert engine.session.health_status == "healthy"
    assert engine.session.new_entries_blocked is False
    assert engine.session.health_issues == []
    assert (
        engine._account_coordinator.bucket_block_reason(
            trade,
            trading_mode=engine.session.trading_mode,
        )
        is None
    )


@pytest.mark.asyncio
async def test_sync_exchange_state_keeps_trade_open_on_first_missing_exchange_snapshot(
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
    assert trade_reloaded.close_reason is None
    assert trade_reloaded.exchange_position_missing_confirmations == 1
    assert trade_reloaded.exchange_position_missing_since is not None
    assert trade_reloaded.last_exchange_sync_error is not None
    assert "pending_confirmation 1/2" in trade_reloaded.last_exchange_sync_error
    assert signal is not None
    assert signal.status == "open"
    assert engine._open_trades["sig_test"] is trade


@pytest.mark.asyncio
async def test_sync_exchange_state_clears_pending_miss_when_position_reappears(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
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

    assert trade.exchange_position_missing_confirmations == 1
    engine._futures_client.get_open_positions.return_value = [
        SimpleNamespace(
            symbol_internal="BTCUSDT",
            side="LONG",
            position=Decimal("0.01"),
            available=Decimal("0.01"),
            avg_price=Decimal("50000"),
            last_price=Decimal("50010"),
            mark_price=Decimal("50010"),
            leverage=10,
            margin=Decimal("50"),
            unrealized_pnl=Decimal("0.1"),
            realized_pnl=Decimal("0"),
            liquidation_price=Decimal("45000"),
            margin_type="CROSS",
            exchange_symbol="BTC-SWAP-USDT",
        )
    ]
    engine._reconcile_exchange_trade_protection = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        await engine._sync_exchange_state()

    reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    assert reloaded is not None
    assert reloaded.status == "open"
    assert reloaded.exchange_position_missing_since is None
    assert reloaded.exchange_position_missing_confirmations == 0
    assert reloaded.last_exchange_sync_error is None
    assert "live_trading.exchange_position_missing_recovered" in caplog.text


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
    engine._futures_client.get_user_trades.return_value = []
    engine._futures_client.get_order_history.return_value = []

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
    assert trade.close_reason == (
        "confirmed_exchange_position_missing:followup_exchange_position_missing"
    )
    assert trade.last_exchange_sync_error is None
    assert trade.pending_fill_audit_quantity == Decimal("0.01")
    assert signal is not None
    assert signal.status == "closed"
    assert "sig_test" not in engine._open_trades
    assert engine.session.new_entries_blocked is False


def test_finalize_closed_trade_clears_pending_exchange_position_missing_state(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.status = "closed"
    trade.closed_at = datetime.now(timezone.utc)
    trade.exchange_position_missing_since = datetime.now(timezone.utc) - timedelta(seconds=5)
    trade.exchange_position_missing_confirmations = 1
    trade.last_exchange_sync_error = (
        "exchange_position_missing: pending_confirmation 1/2 elapsed=0s"
    )
    engine._open_trades["sig_test"] = trade

    engine._finalize_closed_trade(trade)

    trade_reloaded = engine.store.load_trade(engine.session.session_id, "trade_test")
    assert trade_reloaded is not None
    assert trade_reloaded.exchange_position_missing_since is None
    assert trade_reloaded.exchange_position_missing_confirmations == 0
    assert trade_reloaded.last_exchange_sync_error is None
    assert "sig_test" not in engine._open_trades


@pytest.mark.asyncio
async def test_ensure_trade_protection_after_open_keeps_trade_open_when_required_targets_exist(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.stop_loss = Decimal("49000")
    trade.required_tp_order_count = 1
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
        item.tp_order_ids = ["tp_live_1"]

    engine._refresh_trade_protection_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=_refresh_ids
    )

    await engine._ensure_trade_protection_after_open(trade)

    assert trade.status == "open"
    assert trade.sl_order_id == "sl_live_1"
    assert trade.tp_order_ids == ["tp_live_1"]
    assert trade.protection_sync_failures == 1
    assert trade.last_exchange_sync_error == "tp order rejected"
    assert any(
        note == "protection_sync_degraded=tp order rejected"
        for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_ensure_trade_protection_after_open_raises_when_required_targets_never_exist(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.stop_loss = Decimal("49000")
    trade.required_tp_order_count = 1
    engine._futures_client = AsyncMock()
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("stop rejected")
    )

    async def _refresh_ids(item: LiveTrade) -> None:
        item.sl_order_id = "sl_live_1"
        item.tp_order_ids = []

    engine._refresh_trade_protection_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=_refresh_ids
    )

    with pytest.raises(ValueError, match="stop rejected"):
        await engine._ensure_trade_protection_after_open(trade)

    assert engine._sync_trade_protection.await_count == 3
    assert trade.sl_order_id == "sl_live_1"
    assert trade.tp_order_ids == []
    assert trade.protection_sync_failures == 3


@pytest.mark.asyncio
async def test_ensure_trade_protection_rejects_partial_target_tracking(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.take_profits = [Decimal(str(price)) for price in range(51000, 59000, 1000)]
    trade.required_tp_order_count = 8
    engine._futures_client = AsyncMock()
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("target placement interrupted")
    )

    async def _refresh_partial_ids(item: LiveTrade) -> None:
        item.sl_order_id = "sl_live_1"
        item.tp_order_ids = [f"tp_live_{index}" for index in range(1, 6)]

    engine._refresh_trade_protection_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=_refresh_partial_ids
    )

    with pytest.raises(ValueError, match="target placement interrupted"):
        await engine._ensure_trade_protection_after_open(trade)

    assert engine._sync_trade_protection.await_count == 3
    assert trade.sl_order_id == "sl_live_1"
    assert len(trade.tp_order_ids) == 5
    assert trade.required_tp_order_count == 8


@pytest.mark.asyncio
async def test_reconcile_missing_stop_and_targets_enforces_fail_closed_policy(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.sl_order_id = None
    trade.tp_order_ids = []
    trade.tp_order_plan = []
    engine._futures_client = AsyncMock()
    engine._exchange_trade_has_required_protection = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )
    engine._enforce_trade_protection_or_flatten = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[],
        open_protection_orders=[],
        symbol_user_trades=[],
    )

    engine._enforce_trade_protection_or_flatten.assert_awaited_once_with(
        trade=trade,
        reason="exchange_protection_gap",
    )


@pytest.mark.asyncio
async def test_reconcile_unavailable_default_targets_enforces_fail_closed_policy(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.take_profits = []
    engine._strategy.get_synthetic_take_profits.return_value = []
    engine._futures_client = AsyncMock()
    engine._enforce_trade_protection_or_flatten = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[],
        open_protection_orders=[],
        symbol_user_trades=[],
    )

    engine._enforce_trade_protection_or_flatten.assert_awaited_once_with(
        trade=trade,
        reason="default_take_profits_unavailable",
    )


@pytest.mark.asyncio
async def test_protection_enforcement_flattens_position_when_repair_fails(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    engine._ensure_trade_protection_after_open = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("protection unavailable")
    )

    async def _close_position(**kwargs: object) -> None:
        closed_trade = kwargs["trade"]
        assert isinstance(closed_trade, LiveTrade)
        closed_trade.status = "closed"
        closed_trade.remaining_quantity = Decimal("0")

    engine._execute_exchange_close = AsyncMock(  # type: ignore[method-assign]
        side_effect=_close_position
    )

    protected = await engine._enforce_trade_protection_or_flatten(
        trade=trade,
        reason="test_gap",
    )

    assert protected is False
    assert trade.status == "closed"
    assert trade.remaining_quantity == Decimal("0")
    engine._execute_exchange_close.assert_awaited_once_with(
        trade=trade,
        fraction=Decimal("1"),
        reason="test_gap_fail_closed_flatten",
    )


@pytest.mark.asyncio
async def test_protection_enforcement_persists_critical_error_when_flatten_fails(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    engine._ensure_trade_protection_after_open = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("protection unavailable")
    )
    engine._execute_exchange_close = AsyncMock(  # type: ignore[method-assign]
        side_effect=ToobitAPIError("close unavailable")
    )

    protected = await engine._enforce_trade_protection_or_flatten(
        trade=trade,
        reason="test_gap",
    )

    persisted = engine.store.load_trade(engine.session.session_id, trade.trade_id)
    assert protected is False
    assert trade.is_open
    assert "protection_error=protection unavailable" in (trade.last_exchange_sync_error or "")
    assert "close_error=close unavailable" in (trade.last_exchange_sync_error or "")
    assert persisted is not None
    assert persisted.last_exchange_sync_error == trade.last_exchange_sync_error


@pytest.mark.asyncio
async def test_handle_followup_update_tp_restores_previous_targets_when_exchange_sync_fails(
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
    engine._futures_client = AsyncMock()
    engine._sync_trade_protection = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ValueError("tp order rejected"), None]
    )
    trace = LiveMessageTrace(
        session_id=engine.session.session_id,
        message_id=22,
        channel_id="@testchan",
        channel_label="@testchan",
        message_date=datetime.now(timezone.utc),
    )
    parsed = _open_signal(action=SignalAction.UPDATE_TP).model_copy(
        update={"take_profits": [Decimal("51100"), Decimal("52200"), Decimal("53300")]}
    )

    await engine._handle_followup(
        signal_id="sig_test",
        parsed=parsed,
        message=_message(22, "tp 51100 52200 53300"),
        context=context,
        trace=trace,
    )

    signal = engine.store.load_signal_snapshot(engine.session.session_id, "sig_test")
    assert trade.take_profits == [Decimal("51000"), Decimal("52000")]
    assert signal is not None
    assert signal.take_profits == [Decimal("51000"), Decimal("52000")]
    assert engine._sync_trade_protection.await_count == 2
    assert any(
        note == "exchange_previous_protection_restored" for note in trade.message_history[-1].notes
    )


@pytest.mark.asyncio
async def test_reconcile_exchange_trade_protection_repairs_missing_targets(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.sl_order_id = "sl_live_1"
    trade.tp_order_ids = []
    engine._futures_client = SimpleNamespace()

    async def _refresh_ids(item: LiveTrade) -> None:
        item.sl_order_id = "sl_live_1"
        item.tp_order_ids = []

    engine._refresh_trade_protection_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=_refresh_ids
    )
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[],
        open_protection_orders=[],
        symbol_user_trades=[],
    )

    engine._sync_trade_protection.assert_awaited_once_with(trade)


@pytest.mark.asyncio
async def test_reconcile_restores_default_targets_when_trade_has_none(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    trade = _trade(engine.session.session_id)
    trade.take_profits = []
    trade.sl_order_id = "sl_live_1"
    engine._futures_client = SimpleNamespace()
    engine._strategy.get_synthetic_take_profits.return_value = [
        Decimal("51000"),
        Decimal("52000"),
    ]
    engine._sync_trade_protection = AsyncMock()  # type: ignore[method-assign]

    await engine._reconcile_exchange_trade_protection(
        trade=trade,
        open_regular_orders=[],
        open_protection_orders=[],
        symbol_user_trades=[],
    )

    assert trade.take_profits == [Decimal("51000"), Decimal("52000")]
    engine._sync_trade_protection.assert_awaited_once_with(trade)


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


def test_restore_runtime_state_closes_stale_active_snapshot_without_open_trade(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session_id = engine.session.session_id
    engine.store.save_signal_snapshot(
        session_id,
        LiveSignalSnapshot(
            signal_id="sig_dexe_stale",
            channel_id="https://t.me/Scalping_300",
            channel_label="@Scalping_300",
            created_from_message_id=158516,
            related_message_ids=[158516],
            status="open",
            status_group="active",
            symbol="DEXEUSDT",
            side="short",
            updated_at=datetime.now(timezone.utc),
        ),
    )

    restored = _engine(tmp_path)
    restored._restore_runtime_state()

    context = restored._get_or_create_context("https://t.me/Scalping_300")
    signal = context.get_signal("sig_dexe_stale")
    persisted = restored.store.load_signal_snapshot(session_id, "sig_dexe_stale")
    assert signal is not None
    assert signal.status is SignalStatus.CLOSED
    assert persisted is not None
    assert persisted.status == "closed"
    assert persisted.status_group == "inactive"


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
