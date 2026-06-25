"""Admin action formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from triak_trade.core.formatting import format_decimal
from triak_trade.domain.models import ProposedAction, SignalState


@dataclass
class AdminButton:
    text: str
    callback_data: str


@dataclass
class FormattedAdminAction:
    text: str
    buttons: list[AdminButton]


class AdminActionFormatter:
    def format_action(
        self,
        action: ProposedAction,
        signal: SignalState | None = None,
        metrics: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
    ) -> FormattedAdminAction:
        payload = action.payload
        symbol = _pick(payload, "symbol")
        side = _pick(payload, "side")
        entry = _pick(payload, "entry")
        stop_loss = _pick(payload, "stop_loss")
        take_profits = _pick(payload, "take_profits")
        leverage = _pick(payload, "leverage")
        channel_id = _pick(payload, "channel_id")
        signal_id = action.signal_id or _pick(payload, "signal_id") or "n/a"

        lines = [
            "<b>Triak_Trade Admin Approval</b>",
            f"Action: <code>{action.action_type.value}</code>",
            f"Signal ID: <code>{signal_id}</code>",
            f"Channel: <code>{channel_id or (signal.channel_id if signal else 'unknown')}</code>",
            f"Symbol: <code>{symbol or _signal_value(signal, 'symbol') or 'unknown'}</code>",
            f"Side: <code>{side or _signal_value(signal, 'side') or 'unknown'}</code>",
            f"Entry: <code>{entry or _entry_from_signal(signal) or 'unknown'}</code>",
            (
                "Stop Loss: "
                f"<code>{stop_loss or _signal_value(signal, 'stop_loss') or 'unknown'}</code>"
            ),
            (
                "Take Profits: "
                f"<code>{take_profits or _signal_value(signal, 'take_profits') or 'unknown'}</code>"
            ),
            f"Leverage: <code>{leverage or _signal_value(signal, 'leverage') or 'unknown'}</code>",
            f"Confidence: <code>{action.confidence}</code>",
            f"Risk Increasing: <code>{action.risk_increasing}</code>",
            f"Requires Approval: <code>{action.requires_admin_approval}</code>",
            f"Reason: {action.reason}",
            "<b>Safety:</b> Demo only / no live execution.",
        ]

        if metrics:
            lines.append(f"Metrics: <code>{_safe_json(metrics)}</code>")
        if risk:
            lines.append(f"Risk: <code>{_safe_json(risk)}</code>")
        if symbol is None:
            lines.append("Warning: incomplete payload fields.")

        buttons = [
            AdminButton(
                text="Approve demo action",
                callback_data=f"admin:approve:{action.action_id}",
            ),
            AdminButton(text="Reject", callback_data=f"admin:reject:{action.action_id}"),
            AdminButton(text="Watch only", callback_data=f"admin:watch:{action.action_id}"),
        ]
        return FormattedAdminAction(text="\n".join(lines), buttons=buttons)


def _pick(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, list):
        return ",".join(
            (format_decimal(item) or "0") if isinstance(item, Decimal) else str(item)
            for item in value[:5]
        )
    return None


def _signal_value(signal: SignalState | None, key: str) -> str | None:
    if signal is None or signal.current_signal is None:
        return None
    value = getattr(signal.current_signal, key, None)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, list):
        return ",".join(
            (format_decimal(item) or "0") if isinstance(item, Decimal) else str(item)
            for item in value
        )
    return str(value)


def _entry_from_signal(signal: SignalState | None) -> str | None:
    if signal is None or signal.current_signal is None:
        return None
    low = signal.current_signal.entry_low
    high = signal.current_signal.entry_high
    if low is not None and high is not None:
        return f"{format_decimal(low)}-{format_decimal(high)}"
    return format_decimal(low or high) if (low or high) else None


def _safe_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)[:300]
