from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.classifier import ClassifiedMessage, MessageClassifier
from triak_trade.agents.clock import FakeClock
from triak_trade.agents.context import ChannelContext
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import NormalizedMessage, ParsedSignal, RawTelegramMessage


class FakeMessageClassifier(MessageClassifier):
    def classify(self, message: RawTelegramMessage, context: ChannelContext) -> ClassifiedMessage:
        parsed = ParsedSignal(
            action=SignalAction.OPEN,
            market=MarketType.FUTURES,
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_type=EntryType.RANGE,
            entry_low=Decimal("100"),
            entry_high=Decimal("101"),
            stop_loss=Decimal("95"),
            take_profits=[Decimal("110")],
            leverage=3,
            confidence=Decimal("0.9"),
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
                detected_keywords=["long", "entry", "sl", "tp"],
                language_hint=None,
            ),
            parsed_signal=parsed,
            is_potential_new_signal=True,
            is_related_to_existing_signal=False,
            related_signal_id=None,
            relation_reason=None,
            confidence=Decimal("0.9"),
            debug_notes=["fake classifier"],
        )


def test_channel_agent_depends_on_classifier_protocol() -> None:
    clock = FakeClock(datetime.now(timezone.utc))
    settings = Settings()
    agent = ChannelAgent(
        channel_id="c1",
        settings=settings,
        classifier=FakeMessageClassifier(),
        clock=clock,
    )

    raw = RawTelegramMessage(
        channel_id="c1",
        channel_username=None,
        message_id=1,
        text="any",
        date=clock.now(),
        edited_at=None,
        reply_to_msg_id=None,
    )

    actions = agent.ingest_message(raw)
    assert actions == []

    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    tick_actions = agent.tick(clock.now())
    assert len(tick_actions) == 1
    assert tick_actions[0].action_type.value == "create_order"
