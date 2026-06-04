from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.agents.context import ChannelContext
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, SignalStatus, TradeSide
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState


def _signal(signal_id: str, symbol: str, now: datetime) -> SignalState:
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
        leverage=3,
        confidence=Decimal("0.8"),
        invalid_reason=None,
        source_channel_id="c1",
        source_message_id=1,
        parser_version="v1",
    )
    return SignalState(
        signal_id=signal_id,
        channel_id="c1",
        status=SignalStatus.PENDING_CONSOLIDATION,
        created_from_message_id=1,
        related_message_ids=[1],
        current_signal=parsed,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def test_context_message_limit_enforced() -> None:
    ctx = ChannelContext(channel_id="c1", max_message_limit=3, max_update_window_hours=48)
    now = datetime.now(timezone.utc)
    for idx in range(1, 6):
        ctx.add_recent_message(
            RawTelegramMessage(
                channel_id="c1",
                channel_username=None,
                message_id=idx,
                text=f"m{idx}",
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
        )
    assert [m.message_id for m in ctx.recent_messages] == [3, 4, 5]


def test_context_reply_mapping_and_update_window() -> None:
    now = datetime.now(timezone.utc)
    ctx = ChannelContext(channel_id="c1", max_message_limit=10, max_update_window_hours=48)
    state = _signal("sig-1", "BTCUSDT", now)
    ctx.add_signal(state, pending=True)

    found = ctx.find_signal_by_message_reply(1)
    assert found is not None
    assert found.signal_id == "sig-1"

    assert ctx.is_within_update_window(state, now + timedelta(hours=24)) is True
    assert ctx.is_within_update_window(state, now + timedelta(hours=49)) is False


def test_context_reply_chain_and_following_messages() -> None:
    now = datetime.now(timezone.utc)
    ctx = ChannelContext(channel_id="c1", max_message_limit=10, max_update_window_hours=48)
    messages = [
        RawTelegramMessage(
            channel_id="c1",
            channel_username=None,
            message_id=1,
            text="BTCUSDT LONG",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        ),
        RawTelegramMessage(
            channel_id="c1",
            channel_username=None,
            message_id=2,
            text="TP1 69000",
            date=now,
            edited_at=None,
            reply_to_msg_id=1,
        ),
        RawTelegramMessage(
            channel_id="c1",
            channel_username=None,
            message_id=3,
            text="SL 67400",
            date=now,
            edited_at=None,
            reply_to_msg_id=2,
        ),
        RawTelegramMessage(
            channel_id="c1",
            channel_username=None,
            message_id=4,
            text="noise",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        ),
    ]
    ctx.seed_message_catalog(messages)
    for message in messages:
        ctx.add_recent_message(message)

    reply_chain = ctx.get_reply_chain(messages[2], max_depth=3)
    assert [item.message_id for item in reply_chain] == [2, 1]

    following = ctx.get_following_messages(messages[0], limit=3)
    assert [item.message_id for item in following] == [2, 3, 4]
