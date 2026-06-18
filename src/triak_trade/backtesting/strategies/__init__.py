"""Trade strategy module for backtesting and live execution."""

from triak_trade.backtesting.strategies.base import TargetHitAction, TradeStrategy
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy
from triak_trade.backtesting.strategies.registry import load_strategy, load_strategy_from_dict

__all__ = [
    "DefaultRiskManagedStrategy",
    "TargetHitAction",
    "TradeStrategy",
    "load_strategy",
    "load_strategy_from_dict",
]
