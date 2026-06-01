from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from triak_trade.telegram.mapper import telethon_message_to_raw


def test_mapper_handles_text_field() -> None:
    msg = SimpleNamespace(
        id=1,
        chat_id=-100123,
        text="hello",
        message=None,
        caption=None,
        date=datetime.now(timezone.utc),
        edit_date=None,
        reply_to=None,
        chat=SimpleNamespace(username="chan"),
    )
    raw = telethon_message_to_raw(msg)
    assert raw.channel_id == "-100123"
    assert raw.channel_username == "chan"
    assert raw.text == "hello"


def test_mapper_handles_message_caption_reply_and_edited() -> None:
    msg = SimpleNamespace(
        id=2,
        chat_id=-100456,
        text=None,
        message="body",
        caption=None,
        date=datetime.now(timezone.utc),
        edit_date=datetime.now(timezone.utc),
        reply_to=SimpleNamespace(reply_to_msg_id=10),
        reply_to_msg_id=None,
        chat=SimpleNamespace(username=None),
    )
    raw = telethon_message_to_raw(msg)
    assert raw.text == "body"
    assert raw.edited_at is not None
    assert raw.reply_to_msg_id == 10


def test_mapper_handles_caption_and_empty_text() -> None:
    msg = SimpleNamespace(
        id=3,
        chat_id=-100789,
        text=None,
        message=None,
        caption="pic caption",
        date=datetime.now(timezone.utc),
        edit_date=None,
        reply_to=None,
        chat=SimpleNamespace(username="x"),
    )
    raw = telethon_message_to_raw(msg)
    assert raw.text == "pic caption"

    msg2 = SimpleNamespace(
        id=4,
        chat_id=-100789,
        text=None,
        message=None,
        caption=None,
        date=datetime.now(timezone.utc),
        edit_date=None,
        reply_to=None,
        chat=SimpleNamespace(username="x"),
    )
    raw2 = telethon_message_to_raw(msg2)
    assert raw2.text is None
