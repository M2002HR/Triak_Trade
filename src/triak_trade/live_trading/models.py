"""Live / demo trading data models."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MessageAttribution(BaseModel):
    """Tracks which Telegram message caused a change to a position."""

    message_id: int
    channel_id: str
    channel_label: str
    message_preview: str
    message_date: datetime
    action: str  # "opened", "updated_sl", "updated_tp", "partial_close", "closed", "set_leverage"
    applied_at: datetime = Field(default_factory=_utc_now)
    notes: list[str] = Field(default_factory=list)


class LiveTrade(BaseModel):
    """A single position opened by a live/demo trading session."""

    trade_id: str
    session_id: str
    signal_id: str
    channel_id: str
    channel_input: str
    channel_label: str

    # Position details
    symbol: str
    side: str  # "long" or "short"
    leverage: int = 1
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal | None = None
    take_profits: list[Decimal] = Field(default_factory=list)

    # Status
    status: str = "waiting_entry"
    # waiting_entry → open → partial_close → closed

    # Exchange order IDs (real mode)
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    tp_order_ids: list[str] = Field(default_factory=list)

    # Attribution - every message that affected this trade
    message_history: list[MessageAttribution] = Field(default_factory=list)

    # P&L tracking
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    exit_price: Decimal | None = None
    close_reason: str | None = None  # "sl_hit", "tp_hit", "manual_close", "partial_tp_X"
    targets_hit: int = 0

    # Remaining quantity (for partial closes)
    remaining_quantity: Decimal = Decimal("0")

    # Balance at time of entry (for pnl_pct calculation)
    balance_at_entry: Decimal = Decimal("0")

    # Timing
    opened_at: datetime = Field(default_factory=_utc_now)
    closed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=_utc_now)

    # Live price data (updated periodically)
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal = Decimal("0")

    # Margin used
    margin: Decimal = Decimal("0")

    def model_post_init(self, __context: Any) -> None:
        if self.remaining_quantity == Decimal("0"):
            self.remaining_quantity = self.quantity

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def total_pnl_pct(self) -> Decimal:
        if self.balance_at_entry <= 0:
            return Decimal("0")
        return (self.total_pnl / self.balance_at_entry) * Decimal("100")

    @property
    def is_open(self) -> bool:
        return self.status in ("waiting_entry", "open", "partial_close")

    def add_attribution(self, attribution: MessageAttribution) -> None:
        self.message_history.append(attribution)
        self.updated_at = _utc_now()

    def last_attribution(self) -> MessageAttribution | None:
        return self.message_history[-1] if self.message_history else None


class LiveSessionConfig(BaseModel):
    """Configuration for starting a live/demo trading session."""

    channels: list[str]
    trading_mode: str = "demo"  # "demo" or "live"
    initial_balance: Decimal = Decimal("100")
    risk_per_trade_pct: Decimal = Decimal("120")
    strategy_key: str = "tp_trailing_risk_managed"
    use_ai: bool = True
    interval: str = "1m"
    label: str | None = None

    @model_validator(mode="after")
    def normalize_by_mode(self) -> LiveSessionConfig:
        mode = self.trading_mode.strip().lower()
        if mode not in {"demo", "live"}:
            raise ValueError("trading_mode must be 'demo' or 'live'")
        if len(self.channels) != 1:
            raise ValueError("each live session must contain exactly one channel")
        self.trading_mode = mode
        if mode == "live":
            # Live sessions must derive capital from the connected account balance.
            self.initial_balance = Decimal("0")
        return self


class LiveAccountInfo(BaseModel):
    """Account information fetched from Toobit exchange."""

    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    margin_balance: Decimal = Decimal("0")
    total_position_margin: Decimal = Decimal("0")
    max_withdraw: Decimal = Decimal("0")
    fetched_at: datetime = Field(default_factory=_utc_now)
    error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.error is None


class LiveSession(BaseModel):
    """A running live/demo trading session."""

    session_id: str
    channels: list[str]
    channel_labels: list[str] = Field(default_factory=list)
    trading_mode: str  # "demo" or "live"

    initial_balance: Decimal
    risk_per_trade_pct: Decimal
    strategy_key: str
    use_ai: bool
    interval: str
    label: str | None = None

    status: str = "starting"
    # starting → running → stopping → stopped | error

    started_at: datetime = Field(default_factory=_utc_now)
    stopped_at: datetime | None = None
    last_error: str | None = None
    errors: list[str] = Field(default_factory=list)

    # Paper trading balance (demo mode)
    paper_balance: Decimal = Decimal("0")
    paper_initial_balance: Decimal = Decimal("0")

    # Account info from exchange (live mode, or mark-price synced)
    account_info: LiveAccountInfo | None = None

    # Aggregate stats
    total_signals_received: int = 0
    total_signals_opened: int = 0
    open_positions_count: int = 0
    closed_trades_count: int = 0
    wins: int = 0
    losses: int = 0
    total_realized_pnl: Decimal = Decimal("0")
    total_unrealized_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")

    # Processed message count (for display)
    total_messages_processed: int = 0

    last_update_at: datetime = Field(default_factory=_utc_now)

    def model_post_init(self, __context: Any) -> None:
        if self.trading_mode == "demo" and self.paper_balance == Decimal("0"):
            self.paper_balance = self.initial_balance
        if self.trading_mode == "demo" and self.paper_initial_balance == Decimal("0"):
            self.paper_initial_balance = self.initial_balance

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def total_pnl(self) -> Decimal:
        return self.total_realized_pnl + self.total_unrealized_pnl

    def mark_running(self) -> None:
        self.status = "running"
        self.last_update_at = _utc_now()

    def mark_stopped(self, error: str | None = None) -> None:
        self.status = "error" if error else "stopped"
        self.stopped_at = _utc_now()
        self.last_update_at = _utc_now()
        if error:
            self.last_error = error
            self.errors.append(error)


class LiveMessageTrace(BaseModel):
    """Tracks a single Telegram message through the live trading pipeline."""

    session_id: str
    message_id: int
    channel_id: str
    channel_label: str
    message_date: datetime
    preview_text: str = ""
    full_text: str | None = None
    received_at: datetime = Field(default_factory=_utc_now)

    # Classification results
    classification: str | None = None  # "new_signal","follow_up","ignore","ambiguous"
    parsed_action: str | None = None
    symbol: str | None = None
    side: str | None = None
    confidence: str | None = None
    signal_id: str | None = None

    # Effect
    final_status: str = "processing"
    # processing → opened_trade | updated_trade | closed_trade | ignored | invalid
    effect_summary: str | None = None
    trade_id: str | None = None

    debug_notes: list[str] = Field(default_factory=list)


class LiveTradingSnapshot(BaseModel):
    """Full current state snapshot for the dashboard."""

    session: LiveSession
    open_trades: list[LiveTrade] = Field(default_factory=list)
    recent_closed_trades: list[LiveTrade] = Field(default_factory=list)
    account_info: LiveAccountInfo | None = None
    generated_at: datetime = Field(default_factory=_utc_now)

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return sum((t.unrealized_pnl for t in self.open_trades), Decimal("0"))

    @property
    def total_margin_used(self) -> Decimal:
        return sum((t.margin for t in self.open_trades), Decimal("0"))


class LiveSessionDetail(BaseModel):
    """Detailed session payload for per-session modal views."""

    session: LiveSession
    snapshot: LiveTradingSnapshot | None = None
    messages: list[LiveMessageTrace] = Field(default_factory=list)
    open_trades: list[LiveTrade] = Field(default_factory=list)
    closed_trades: list[LiveTrade] = Field(default_factory=list)
