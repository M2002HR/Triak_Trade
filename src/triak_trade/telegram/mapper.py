"""Map Telethon-like messages into RawTelegramMessage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from triak_trade.domain.models import RawTelegramMessage


def _safe_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _extract_channel_id(message: Any) -> str:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return str(chat_id)
    peer = getattr(message, "peer_id", None)
    if peer is not None and getattr(peer, "channel_id", None) is not None:
        return str(peer.channel_id)
    return "unknown-channel"


def _extract_reply_to(message: Any) -> int | None:
    direct = getattr(message, "reply_to_msg_id", None)
    if isinstance(direct, int):
        return direct
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None:
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(reply_id, int):
            return reply_id
    return None


def telethon_message_to_raw(message: Any, *, channel: str | None = None) -> RawTelegramMessage:
    text = getattr(message, "text", None)
    if text is None:
        text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "caption", None)

    chat = getattr(message, "chat", None)
    username = getattr(chat, "username", None) if chat is not None else None

    payload = {
        "id": getattr(message, "id", None),
        "chat_id": getattr(message, "chat_id", None),
        "date": _safe_dt(getattr(message, "date", None)).isoformat(),
        "edit_date": (
            _safe_dt(getattr(message, "edit_date", None)).isoformat()
            if getattr(message, "edit_date", None) is not None
            else None
        ),
        "reply_to_msg_id": _extract_reply_to(message),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
    }

    return RawTelegramMessage(
        channel_id=channel or _extract_channel_id(message),
        channel_username=username,
        message_id=int(getattr(message, "id", 0) or 0),
        text=text,
        date=_safe_dt(getattr(message, "date", None)),
        edited_at=(
            _safe_dt(getattr(message, "edit_date", None))
            if getattr(message, "edit_date", None) is not None
            else None
        ),
        deleted=False,
        reply_to_msg_id=_extract_reply_to(message),
        raw_payload=payload,
    )
