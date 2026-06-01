from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    ProposedActionType,
    SignalAction,
    SignalStatus,
    TradeSide,
)
from triak_trade.domain.models import (
    BacktestReport,
    Candle,
    ChannelMetrics,
    NormalizedMessage,
    ParsedSignal,
    ProposedAction,
    RawTelegramMessage,
    SignalState,
    SimulatedTrade,
)

NOW = datetime.now(tz=timezone.utc)


def _raw_message() -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="chan-1",
        channel_username="chan",
        message_id=10,
        text="OPEN BTC",
        date=NOW,
        edited_at=None,
        reply_to_msg_id=None,
    )


def _parsed_signal(**overrides: object) -> ParsedSignal:
    base: dict[str, object] = {
        "action": SignalAction.OPEN,
        "market": MarketType.FUTURES,
        "symbol": "btc/usdt",
        "side": TradeSide.LONG,
        "entry_type": EntryType.RANGE,
        "entry_low": "100",
        "entry_high": "105",
        "stop_loss": "95",
        "take_profits": ["110", "120"],
        "leverage": 3,
        "confidence": "0.75",
        "invalid_reason": None,
        "source_channel_id": "chan-1",
        "source_message_id": 10,
        "parser_version": "v1",
    }
    base.update(overrides)
    return ParsedSignal(**base)


def test_valid_raw_telegram_message() -> None:
    msg = _raw_message()
    assert msg.channel_id == "chan-1"


def test_invalid_message_id_rejected() -> None:
    with pytest.raises(ValidationError):
        RawTelegramMessage(
            channel_id="chan-1",
            channel_username=None,
            message_id=0,
            text=None,
            date=NOW,
            edited_at=None,
            reply_to_msg_id=None,
        )


def test_edited_at_before_date_rejected() -> None:
    with pytest.raises(ValidationError):
        RawTelegramMessage(
            channel_id="chan-1",
            channel_username=None,
            message_id=1,
            text=None,
            date=NOW,
            edited_at=NOW - timedelta(seconds=1),
            reply_to_msg_id=None,
        )


def test_normalized_message_symbols_and_keywords() -> None:
    normalized = NormalizedMessage(
        raw=_raw_message(),
        normalized_text="open btc",
        detected_symbols=[" btcusdt", "BTCUSDT", "ethusdt"],
        detected_keywords=[" Open", "open", "Breakout"],
        language_hint="en",
    )
    assert normalized.detected_symbols == ["BTCUSDT", "ETHUSDT"]
    assert normalized.detected_keywords == ["open", "breakout"]


def test_parsed_signal_accepts_decimal_strings() -> None:
    signal = _parsed_signal()
    assert signal.symbol == "BTC/USDT"
    assert signal.entry_low == Decimal("100")


def test_confidence_below_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed_signal(confidence="-0.01")


def test_confidence_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed_signal(confidence="1.01")


def test_leverage_non_positive_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed_signal(leverage=0)


def test_entry_low_greater_than_entry_high_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed_signal(entry_low="106", entry_high="105")


def test_signal_state_requires_origin_message_in_related_ids() -> None:
    with pytest.raises(ValidationError):
        SignalState(
            signal_id="sig_1",
            channel_id="chan-1",
            status=SignalStatus.PENDING_CONSOLIDATION,
            created_from_message_id=100,
            related_message_ids=[101, 102],
            current_signal=_parsed_signal(),
            version=1,
            created_at=NOW,
            updated_at=NOW,
            expires_at=None,
        )


def test_proposed_action_risk_increasing_requires_approval() -> None:
    with pytest.raises(ValidationError):
        ProposedAction(
            action_id="act_1",
            action_type=ProposedActionType.CREATE_ORDER,
            signal_id="sig_1",
            risk_increasing=True,
            requires_admin_approval=False,
            confidence=Decimal("0.8"),
            reason="need to enter",
            payload={},
            created_at=NOW,
        )


def test_candle_high_low_validation() -> None:
    candle = Candle(
        symbol="btcusdt",
        interval="1m",
        open_time=NOW,
        close_time=NOW + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("105"),
        volume=Decimal("10"),
        source=CandleSource.FIXTURE,
    )
    assert candle.symbol == "BTCUSDT"


def test_candle_rejects_invalid_close_time() -> None:
    with pytest.raises(ValidationError):
        Candle(
            symbol="btcusdt",
            interval="1m",
            open_time=NOW,
            close_time=NOW,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        )


def test_backtest_report_rejects_negative_final_balance() -> None:
    metrics = ChannelMetrics(
        channel_id="chan-1",
        from_date=NOW,
        to_date=NOW + timedelta(days=1),
        total_messages=10,
        parsed_signals=4,
        valid_signals=3,
        ignored_messages=3,
        invalid_signals=1,
        win_rate=Decimal("0.5"),
        profit_factor=Decimal("1.2"),
        expectancy=Decimal("0.1"),
        max_drawdown=Decimal("0.2"),
        total_pnl=Decimal("20"),
        conservative_pnl=Decimal("15"),
        optimistic_pnl=Decimal("25"),
        edit_delete_penalty=Decimal("0.05"),
    )
    trade = SimulatedTrade(
        trade_id="t-1",
        signal_id="sig-1",
        channel_id="chan-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_time=NOW,
        exit_time=NOW + timedelta(hours=1),
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("1"),
        pnl=Decimal("1"),
        pnl_pct=Decimal("0.01"),
        fees=Decimal("0.1"),
        status="closed",
        notes=["ok"],
    )
    with pytest.raises(ValidationError):
        BacktestReport(
            channel_id="chan-1",
            from_date=NOW,
            to_date=NOW + timedelta(days=1),
            initial_balance=Decimal("1000"),
            final_balance=Decimal("-1"),
            metrics=metrics,
            trades=[trade],
            fill_policy=BacktestFillPolicy.CONSERVATIVE,
            generated_at=NOW,
            warnings=[],
        )
