"""Domain enumerations."""

from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"
    UNKNOWN = "unknown"


class SignalAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    UPDATE_SL = "update_sl"
    UPDATE_TP = "update_tp"
    UPDATE_ENTRY = "update_entry"
    UPDATE_LEVERAGE = "update_leverage"
    CANCEL = "cancel"
    IGNORE = "ignore"
    UNKNOWN = "unknown"


class TradeSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class EntryType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    RANGE = "range"
    UNKNOWN = "unknown"


class SignalStatus(str, Enum):
    PENDING_CONSOLIDATION = "pending_consolidation"
    PROPOSED_TO_ADMIN = "proposed_to_admin"
    APPROVED = "approved"
    REJECTED = "rejected"
    WATCH_ONLY = "watch_only"
    ORDER_PLANNED = "order_planned"
    ORDER_SUBMITTED = "order_submitted"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALID = "invalid"


class ProposedActionType(str, Enum):
    CREATE_ORDER = "create_order"
    CANCEL_PENDING_ORDER = "cancel_pending_order"
    UPDATE_LEVERAGE = "update_leverage"
    PLACE_STOP_LOSS = "place_stop_loss"
    MOVE_STOP_LOSS = "move_stop_loss"
    PLACE_TAKE_PROFIT = "place_take_profit"
    UPDATE_TAKE_PROFIT = "update_take_profit"
    CLOSE_POSITION_PARTIAL = "close_position_partial"
    CLOSE_POSITION_FULL = "close_position_full"
    IGNORE_MESSAGE = "ignore_message"
    REQUEST_ADMIN_CONFIRMATION = "request_admin_confirmation"


class AdminDecisionType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    WATCH_ONLY = "watch_only"


class CandleSource(str, Enum):
    BINANCE = "binance"
    TOOBIT = "toobit"
    FALLBACK = "fallback"
    FIXTURE = "fixture"


class BacktestFillPolicy(str, Enum):
    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"
