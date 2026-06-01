from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.domain.models import RawTelegramMessage
from triak_trade.parsing.normalizer import MessageNormalizer


def _raw(text: str) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="c1",
        channel_username=None,
        message_id=1,
        text=text,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )


def test_digit_conversion_and_whitespace() -> None:
    n = MessageNormalizer().normalize(_raw("BTCUSDT LONG Entry: ۶۸۰۰۰   -  ٦٨٢٠٠"))
    assert "68000" in n.normalized_text
    assert "68200" in n.normalized_text
    assert "  " not in n.normalized_text


def test_symbol_detection_variants() -> None:
    n = MessageNormalizer().normalize(_raw("#btc and BTC/USDT and BTC-USDT and BTC USDT and $eth"))
    assert "BTC" in n.detected_symbols
    assert "BTCUSDT" in n.detected_symbols
    assert "ETH" in n.detected_symbols


def test_keyword_detection_english_and_persian() -> None:
    n = MessageNormalizer().normalize(_raw("LONG Entry SL TP Leverage لانگ ورود حد ضرر تارگت"))
    assert "long" in n.detected_keywords
    assert "entry" in n.detected_keywords
    assert "sl" in n.detected_keywords
    assert "اهرم" in n.detected_keywords or "leverage" in n.detected_keywords
