from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.admin_bot.formatter import AdminActionFormatter
from triak_trade.domain.enums import ProposedActionType
from triak_trade.domain.models import ProposedAction


def test_formatter_includes_action_and_demo_notice_and_risk_warning() -> None:
    formatter = AdminActionFormatter()
    action = ProposedAction(
        action_id="a1",
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="s1",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.9"),
        reason="test",
        payload={"symbol": "BTCUSDT"},
        created_at=datetime.now(timezone.utc),
    )
    out = formatter.format_action(action)
    assert "create_order" in out.text
    assert "Demo only / no live execution" in out.text
    assert "Risk Increasing" in out.text
    assert "replace_me" not in out.text
