"""Domain validation helpers."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.domain.enums import ProposedActionType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal, ProposedAction

_RISK_INCREASING_ACTIONS = {
    ProposedActionType.CREATE_ORDER,
    ProposedActionType.UPDATE_LEVERAGE,
}


def is_risk_increasing_action(action_type: ProposedActionType) -> bool:
    """Return conservative risk-increase classification."""
    return action_type in _RISK_INCREASING_ACTIONS


def requires_admin_approval(action: ProposedAction) -> bool:
    """Compute whether an action needs admin approval."""
    return action.requires_admin_approval or is_risk_increasing_action(action.action_type)


def is_open_signal_structurally_complete(signal: ParsedSignal) -> tuple[bool, str | None]:
    """Check minimal structural completeness for OPEN signals."""
    if signal.action is not SignalAction.OPEN:
        return False, "action is not OPEN"
    if signal.symbol is None:
        return False, "missing symbol"
    if signal.side is TradeSide.UNKNOWN:
        return False, "unknown side"
    if signal.stop_loss is None:
        return False, "missing stop_loss"

    has_entry_price = signal.entry_low is not None or signal.entry_high is not None
    if not has_entry_price and signal.entry_type.value != "market":
        return False, "missing entry price"

    if signal.confidence < Decimal("0.50"):
        return False, "confidence below threshold"

    return True, None
