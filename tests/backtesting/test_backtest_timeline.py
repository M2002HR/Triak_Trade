from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.agents.classifier import ClassifiedMessage
from triak_trade.backtesting.timeline import BacktestTimelineBuilder
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import NormalizedMessage, ParsedSignal, RawTelegramMessage


class FakeClassifier:
    def classify(self, message: RawTelegramMessage, context):
        text = (message.text or "").lower()
        if "promo" in text:
            action = SignalAction.IGNORE
        elif "update" in text:
            action = SignalAction.UPDATE_TP
        else:
            action = SignalAction.OPEN
        parsed = ParsedSignal(
            action=action,
            market=MarketType.FUTURES,
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_type=EntryType.MARKET,
            entry_low=None,
            entry_high=None,
            stop_loss=Decimal("98"),
            take_profits=[Decimal("104")],
            leverage=2,
            confidence=Decimal("0.8"),
            invalid_reason=None,
            source_channel_id=message.channel_id,
            source_message_id=message.message_id,
            parser_version="fake",
        )
        return ClassifiedMessage(
            raw_message=message,
            normalized_message=NormalizedMessage(
                raw=message,
                normalized_text=message.text or "",
                detected_symbols=["BTCUSDT"],
                detected_keywords=["long"],
                language_hint=None,
            ),
            parsed_signal=parsed,
            is_potential_new_signal=action is SignalAction.OPEN,
            is_related_to_existing_signal=action is not SignalAction.OPEN,
            related_signal_id="sig1" if action is not SignalAction.OPEN else None,
            relation_reason=None,
            confidence=Decimal("0.8"),
            debug_notes=[],
        )


def _msg(mid: int, text: str, minute: int) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="c",
        channel_username="u",
        message_id=mid,
        text=text,
        date=datetime(2026, 6, 1, 0, minute, tzinfo=timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )


def test_timeline_orders_and_classifies() -> None:
    builder = BacktestTimelineBuilder(classifier=FakeClassifier(), channel_id="c")
    events = builder.build([_msg(2, "update tp", 2), _msg(1, "new signal", 1), _msg(3, "promo", 3)])
    assert [e.timestamp.minute for e in events] == [1, 2, 3]
    assert events[0].action is SignalAction.OPEN
    assert events[1].action is SignalAction.UPDATE_TP
    assert events[2].action is SignalAction.IGNORE
