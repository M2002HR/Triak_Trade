from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.parsing.fixtures import PARSER_FIXTURES
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser


def test_fixture_count_at_least_40() -> None:
    assert len(PARSER_FIXTURES) >= 40


def test_all_fixtures_parse() -> None:
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()

    for idx, item in enumerate(PARSER_FIXTURES, start=1):
        raw = RawTelegramMessage(
            channel_id="fixture",
            channel_username=None,
            message_id=idx,
            text=item["text"],
            date=datetime.now(timezone.utc),
            edited_at=None,
            reply_to_msg_id=None,
        )
        normalized = normalizer.normalize(raw)
        parsed = parser.parse(normalized)
        assert parsed.confidence >= 0


def test_special_fixture_classifications() -> None:
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()

    lookup = {item["name"]: item["text"] for item in PARSER_FIXTURES}

    cancel = parser.parse(
        normalizer.normalize(
            RawTelegramMessage(
                channel_id="c",
                channel_username=None,
                message_id=1,
                text=lookup["cancel_signal"],
                date=datetime.now(timezone.utc),
                edited_at=None,
                reply_to_msg_id=None,
            )
        )
    )
    assert cancel.action is SignalAction.CANCEL

    profit = parser.parse(
        normalizer.normalize(
            RawTelegramMessage(
                channel_id="c",
                channel_username=None,
                message_id=2,
                text=lookup["profit_report"],
                date=datetime.now(timezone.utc),
                edited_at=None,
                reply_to_msg_id=None,
            )
        )
    )
    assert profit.action is SignalAction.IGNORE
