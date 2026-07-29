"""Shared, deterministic trade-risk controls."""

from triak_trade.risk.stop_loss import (
    StopLossRiskResult,
    clamp_stop_loss_to_risk_budget,
    max_quantity_for_stop_risk_budget,
)

__all__ = [
    "StopLossRiskResult",
    "clamp_stop_loss_to_risk_budget",
    "max_quantity_for_stop_risk_budget",
]
