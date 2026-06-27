from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.domain.enums import (
    EntryType,
    MarketType,
    ProposedActionType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import ParsedSignal
from triak_trade.domain.validation import (
    is_open_signal_structurally_complete,
    is_risk_increasing_action,
)

NOW = datetime.now(tz=timezone.utc)


def _complete_signal(**overrides: object) -> ParsedSignal:
    base: dict[str, object] = {
        "action": SignalAction.OPEN,
        "market": MarketType.FUTURES,
        "symbol": "BTCUSDT",
        "side": TradeSide.LONG,
        "entry_type": EntryType.LIMIT,
        "entry_low": Decimal("100"),
        "entry_high": None,
        "stop_loss": Decimal("95"),
        "take_profits": [Decimal("110")],
        "leverage": 3,
        "confidence": Decimal("0.80"),
        "invalid_reason": None,
        "source_channel_id": "chan-1",
        "source_message_id": 1,
        "parser_version": "v1",
    }
    base.update(overrides)
    return ParsedSignal(**base)


def test_open_signal_structurally_complete_true() -> None:
    ok, reason = is_open_signal_structurally_complete(_complete_signal())
    assert ok is True
    assert reason is None


def test_open_signal_structurally_complete_missing_stop_loss() -> None:
    ok, reason = is_open_signal_structurally_complete(_complete_signal(stop_loss=None))
    assert ok is False
    assert reason == "missing stop_loss"


def test_risk_increasing_action_true_cases() -> None:
    assert is_risk_increasing_action(ProposedActionType.CREATE_ORDER) is True
    assert is_risk_increasing_action(ProposedActionType.UPDATE_LEVERAGE) is True


def test_risk_increasing_action_false_for_move_stop_loss() -> None:
    assert is_risk_increasing_action(ProposedActionType.MOVE_STOP_LOSS) is False
