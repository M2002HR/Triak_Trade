from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.agents.context import ChannelContext
from triak_trade.backtesting.correlation import (
    is_invalid_ai_related_id,
    resolve_related_signal_id,
)
from triak_trade.domain.enums import (
    EntryType,
    MarketType,
    SignalAction,
    SignalStatus,
    TradeSide,
)
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _parsed(symbol: str | None, action: SignalAction = SignalAction.CLOSE) -> ParsedSignal:
    return ParsedSignal(
        action=action,
        market=MarketType.FUTURES,
        symbol=symbol,
        side=TradeSide.UNKNOWN,
        entry_type=EntryType.UNKNOWN,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profits=[],
        leverage=None,
        confidence=Decimal("0.7"),
        invalid_reason=None,
        source_channel_id="c1",
        source_message_id=99,
        parser_version="v1",
    )


def _signal(signal_id: str, symbol: str, created_msg_id: int, updated_at: datetime) -> SignalState:
    parsed = ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol=symbol,
        side=TradeSide.LONG,
        entry_type=EntryType.RANGE,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95"),
        take_profits=[Decimal("110")],
        leverage=10,
        confidence=Decimal("0.8"),
        invalid_reason=None,
        source_channel_id="c1",
        source_message_id=created_msg_id,
        parser_version="v1",
    )
    return SignalState(
        signal_id=signal_id,
        channel_id="c1",
        status=SignalStatus.PENDING_CONSOLIDATION,
        created_from_message_id=created_msg_id,
        related_message_ids=[created_msg_id],
        current_signal=parsed,
        version=1,
        created_at=updated_at,
        updated_at=updated_at,
        expires_at=None,
    )


def _msg(
    message_id: int, *, reply_to: int | None = None, text: str = "follow up"
) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="c1",
        channel_username=None,
        message_id=message_id,
        text=text,
        date=_NOW + timedelta(minutes=message_id),
        edited_at=None,
        reply_to_msg_id=reply_to,
    )


def _ctx(*signals: SignalState) -> ChannelContext:
    ctx = ChannelContext(channel_id="c1", max_message_limit=100, max_update_window_hours=48)
    for sig in signals:
        ctx.add_signal(sig, pending=True)
        if sig.current_signal is not None:
            ctx.add_recent_message(_msg(sig.created_from_message_id, text="open"))
    return ctx


def test_is_invalid_ai_related_id() -> None:
    assert is_invalid_ai_related_id(None)
    assert is_invalid_ai_related_id("")
    assert is_invalid_ai_related_id("unknown")
    assert is_invalid_ai_related_id("6219")  # message id, not signal id
    assert not is_invalid_ai_related_id("sig_abc123")


def test_resolve_uses_valid_ai_related_id() -> None:
    sig = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("BTCUSDT"),
        raw_related_id="sig_btc",
        message=_msg(11),
        action=SignalAction.CLOSE,
    )
    assert result.signal_id == "sig_btc"
    assert result.method == "ai"


def test_resolve_ai_message_id_falls_back_to_symbol() -> None:
    # AI returned a Telegram message id (digits) instead of a signal id.
    sig = _signal("sig_ondo", "ONDOUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("ONDOUSDT"),
        raw_related_id="6219",
        message=_msg(11),
        action=SignalAction.UPDATE_TP,
    )
    assert result.signal_id == "sig_ondo"
    assert result.method == "symbol_single"


def test_resolve_ai_unknown_falls_back_to_symbol() -> None:
    sig = _signal("sig_ondo", "ONDOUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("ONDOUSDT", SignalAction.UPDATE_SL),
        raw_related_id="unknown",
        message=_msg(11),
        action=SignalAction.UPDATE_SL,
    )
    assert result.signal_id == "sig_ondo"
    assert result.method == "symbol_single"


def test_resolve_ai_empty_close_falls_back_to_symbol() -> None:
    # The real "سیو سود کنید" case: CLOSE with no AI id, one open DOGE signal.
    sig = _signal("sig_doge", "DOGEUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("DOGEUSDT", SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(11),
        action=SignalAction.CLOSE,
    )
    assert result.signal_id == "sig_doge"
    assert result.method == "symbol_single"


def test_resolve_reply_to_correlation() -> None:
    # No symbol on the follow-up, but it replies to the signal's message.
    sig = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed(None, SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(11, reply_to=10),
        action=SignalAction.CLOSE,
    )
    assert result.signal_id == "sig_btc"
    assert result.method == "reply_to"


def test_resolve_reply_to_closed_signal_for_audit_visibility() -> None:
    sig = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    sig.status = SignalStatus.INVALID
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed(None, SignalAction.UPDATE_SL),
        raw_related_id=None,
        message=_msg(11, reply_to=10),
        action=SignalAction.UPDATE_SL,
    )
    assert result.signal_id == "sig_btc"
    assert result.method == "reply_to"


def test_resolve_multi_signal_same_symbol_picks_most_recent() -> None:
    older = _signal("sig_old", "BTCUSDT", 10, _NOW)
    newer = _signal("sig_new", "BTCUSDT", 20, _NOW + timedelta(hours=1))
    ctx = _ctx(older, newer)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("BTCUSDT", SignalAction.UPDATE_SL),
        raw_related_id=None,
        message=_msg(21),
        action=SignalAction.UPDATE_SL,
    )
    assert result.signal_id == "sig_new"
    assert result.method == "symbol_recent"


def test_resolve_unattached_followup_returns_none() -> None:
    # CLOSE with no symbol, no reply, no AI id, AND several open signals (so the
    # single-active shortcut does not apply); last-resort disabled -> unattached.
    older = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    newer = _signal("sig_eth", "ETHUSDT", 20, _NOW + timedelta(hours=1))
    ctx = _ctx(older, newer)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed(None, SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(21),
        action=SignalAction.CLOSE,
        allow_last_resort=False,
    )
    assert result.signal_id is None
    assert result.method == "unattached"


def test_resolve_single_active_attaches_symbolless_followup() -> None:
    # The "سیو سود کنید" case: CLOSE, no symbol/reply/AI id, exactly one open
    # signal -> attach to it without needing the last-resort flag.
    sig = _signal("sig_doge", "DOGEUSDT", 10, _NOW)
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed(None, SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(11),
        action=SignalAction.CLOSE,
        allow_last_resort=False,
    )
    assert result.signal_id == "sig_doge"
    assert result.method == "single_active"


def test_resolve_last_resort_attaches_when_enabled() -> None:
    older = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    newer = _signal("sig_eth", "ETHUSDT", 20, _NOW + timedelta(hours=1))
    ctx = _ctx(older, newer)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed(None, SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(21),
        action=SignalAction.CLOSE,
        allow_last_resort=True,
    )
    assert result.signal_id == "sig_eth"
    assert result.method == "most_recent_followup"


def test_resolve_ignores_terminal_signals_for_symbol_match() -> None:
    sig = _signal("sig_btc", "BTCUSDT", 10, _NOW)
    sig.status = SignalStatus.CLOSED
    ctx = _ctx(sig)
    result = resolve_related_signal_id(
        context=ctx,
        parsed=_parsed("BTCUSDT", SignalAction.CLOSE),
        raw_related_id=None,
        message=_msg(11),
        action=SignalAction.CLOSE,
    )
    assert result.signal_id is None
