"""Domain models and contracts."""

from triak_trade.domain.enums import (
    AdminDecisionType,
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    ExecutionMode,
    MarketType,
    ProposedActionType,
    SignalAction,
    SignalStatus,
    TradeSide,
)
from triak_trade.domain.ids import make_action_id, make_client_order_id, make_signal_id
from triak_trade.domain.models import (
    AdminDecision,
    BacktestReport,
    Candle,
    ChannelMetrics,
    NormalizedMessage,
    ParsedSignal,
    ProposedAction,
    RawTelegramMessage,
    SignalState,
    SimulatedTrade,
)

__all__ = [
    "AdminDecision",
    "AdminDecisionType",
    "BacktestFillPolicy",
    "BacktestReport",
    "Candle",
    "CandleSource",
    "ChannelMetrics",
    "EntryType",
    "ExecutionMode",
    "MarketType",
    "NormalizedMessage",
    "ParsedSignal",
    "ProposedAction",
    "ProposedActionType",
    "RawTelegramMessage",
    "SignalAction",
    "SignalState",
    "SignalStatus",
    "SimulatedTrade",
    "TradeSide",
    "make_action_id",
    "make_client_order_id",
    "make_signal_id",
]
