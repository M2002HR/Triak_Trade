from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.domain.enums import EntryType, SignalAction, TradeSide
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser


def _parse(text: str):
    raw = RawTelegramMessage(
        channel_id="c1",
        channel_username=None,
        message_id=1,
        text=text,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )
    normalized = MessageNormalizer().normalize(raw)
    return RegexSignalParser().parse(normalized)


def test_extract_full_signal_fields() -> None:
    parsed = _parse("BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x")
    assert parsed.action is SignalAction.OPEN
    assert parsed.symbol == "BTCUSDT"
    assert parsed.side is TradeSide.LONG
    assert parsed.entry_type is EntryType.RANGE
    assert parsed.entry_low == Decimal("68000")
    assert parsed.entry_high == Decimal("68200")
    assert parsed.stop_loss == Decimal("67400")
    assert parsed.take_profits == [Decimal("69000"), Decimal("70000")]
    assert parsed.leverage == 5


def test_extract_cancel_close_update_and_ignore() -> None:
    assert _parse("cancel BTC signal").action is SignalAction.CANCEL
    assert _parse("close 50% BTC").action is SignalAction.CLOSE
    assert _parse("move SL to entry").action is SignalAction.UPDATE_SL
    assert _parse("TP updated to 70500").action is SignalAction.UPDATE_TP
    assert _parse("TP1 hit ✅ +120% profit").action is SignalAction.IGNORE


def test_ambiguous_message_unknown() -> None:
    parsed = _parse("BTC looking good")
    assert parsed.action in {SignalAction.UNKNOWN, SignalAction.IGNORE}


def test_decimal_values_no_float() -> None:
    parsed = _parse("BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000")
    assert isinstance(parsed.entry_low, Decimal)
    assert isinstance(parsed.stop_loss, Decimal)
    assert all(isinstance(x, Decimal) for x in parsed.take_profits)


def test_extract_noisy_markdown_signal_fields() -> None:
    parsed = _parse(
        """
        **سیگنال فیوچرز**
        ZAMA/USD
        LONG
        LEVERAGE: Cross 20x
        Entry نقطه ورود
        MARKET
        Targets :
        1 0.03750
        2 0.03820
        3 0.03850
        4 0.04100
        STOPLOSS حد ضرر
        0.03495
        [Trade on Toobit](https://t.me/Tofan_Trade/220)
        """
    )
    assert parsed.action is SignalAction.OPEN
    assert parsed.symbol == "ZAMAUSD"
    assert parsed.entry_type is EntryType.MARKET
    assert parsed.stop_loss == Decimal("0.03495")
    assert parsed.take_profits == [
        Decimal("0.03750"),
        Decimal("0.03820"),
        Decimal("0.03850"),
        Decimal("0.04100"),
    ]
    assert parsed.leverage == 20
