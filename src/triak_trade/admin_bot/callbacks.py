"""Admin callback parser."""

from __future__ import annotations

from pydantic import BaseModel

from triak_trade.admin_bot.errors import AdminCallbackParseError
from triak_trade.domain.enums import AdminDecisionType


class ParsedAdminCallback(BaseModel):
    action_id: str
    decision: AdminDecisionType


def parse_admin_callback(data: str) -> ParsedAdminCallback:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "admin":
        raise AdminCallbackParseError("malformed callback data")
    decision_raw = parts[1].strip().lower()
    action_id = parts[2].strip()
    if not action_id:
        raise AdminCallbackParseError("missing action_id")

    mapping = {
        "approve": AdminDecisionType.APPROVE,
        "reject": AdminDecisionType.REJECT,
        "watch": AdminDecisionType.WATCH_ONLY,
    }
    decision = mapping.get(decision_raw)
    if decision is None:
        raise AdminCallbackParseError("unknown decision")
    return ParsedAdminCallback(action_id=action_id, decision=decision)


def is_supported_menu_callback(data: str) -> bool:
    return data in {
        "menu:backtest",
        "backtest:start",
        "backtest:channel:tofan",
        "backtest:range:7d",
        "backtest:range:30d",
        "backtest:interval:1m",
        "backtest:interval:5m",
        "backtest:confirm",
        "backtest:run",
        "backtest:cancel",
    }
