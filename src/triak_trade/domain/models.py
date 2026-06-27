"""Domain data models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from triak_trade.core.symbols import canonical_market_symbol
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    ProposedActionType,
    SignalAction,
    SignalStatus,
    TradeSide,
)


class RawTelegramMessage(BaseModel):
    channel_id: str
    channel_username: str | None
    message_id: int
    text: str | None
    date: datetime
    edited_at: datetime | None
    deleted: bool = False
    reply_to_msg_id: int | None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("channel_id cannot be empty")
        return stripped

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("message_id must be positive")
        return value

    @model_validator(mode="after")
    def validate_edited_time(self) -> RawTelegramMessage:
        if self.edited_at is not None and self.edited_at < self.date:
            raise ValueError("edited_at cannot be before date")
        return self


class NormalizedMessage(BaseModel):
    raw: RawTelegramMessage
    normalized_text: str
    detected_symbols: list[str] = Field(default_factory=list)
    detected_keywords: list[str] = Field(default_factory=list)
    language_hint: str | None

    @field_validator("detected_symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip().upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    @field_validator("detected_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique


class ParsedSignal(BaseModel):
    action: SignalAction
    market: MarketType
    symbol: str | None
    side: TradeSide
    entry_type: EntryType
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop_loss: Decimal | None
    take_profits: list[Decimal] = Field(default_factory=list)
    leverage: int | None
    confidence: Decimal
    invalid_reason: str | None
    source_channel_id: str
    source_message_id: int
    parser_version: str

    model_config = ConfigDict(coerce_numbers_to_str=False)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            return None
        return canonical_market_symbol(normalized) or normalized

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: Decimal) -> Decimal:
        if value < Decimal("0") or value > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("leverage")
    @classmethod
    def validate_leverage(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("leverage must be positive")
        return value

    @model_validator(mode="after")
    def validate_entry_range(self) -> ParsedSignal:
        if (
            self.entry_low is not None
            and self.entry_high is not None
            and self.entry_low > self.entry_high
        ):
            raise ValueError("entry_low must be less than or equal to entry_high")
        return self


class SignalState(BaseModel):
    signal_id: str
    channel_id: str
    status: SignalStatus
    created_from_message_id: int
    related_message_ids: list[int]
    current_signal: ParsedSignal | None
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("version must be positive")
        return value

    @field_validator("related_message_ids")
    @classmethod
    def unique_message_ids(cls, values: list[int]) -> list[int]:
        deduped = list(dict.fromkeys(values))
        if len(deduped) != len(values):
            raise ValueError("related_message_ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_timestamps_and_origin(self) -> SignalState:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if self.created_from_message_id not in self.related_message_ids:
            raise ValueError("created_from_message_id must be included in related_message_ids")
        return self


class ProposedAction(BaseModel):
    action_id: str
    action_type: ProposedActionType
    signal_id: str | None
    risk_increasing: bool
    confidence: Decimal
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: Decimal) -> Decimal:
        if value < Decimal("0") or value > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason cannot be empty")
        return stripped

class Candle(BaseModel):
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: CandleSource

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_price_geometry(self) -> Candle:
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be <= open, close, and high")
        if self.volume < Decimal("0"):
            raise ValueError("volume must be >= 0")
        return self


class SimulatedTrade(BaseModel):
    trade_id: str
    signal_id: str
    channel_id: str
    symbol: str
    side: TradeSide
    entry_time: datetime | None
    exit_time: datetime | None
    entry_price: Decimal | None
    exit_price: Decimal | None
    quantity: Decimal
    pnl: Decimal
    pnl_pct: Decimal
    fees: Decimal
    status: str
    notes: list[str] = Field(default_factory=list)

    @field_validator("quantity", "fees")
    @classmethod
    def non_negative(cls, value: Decimal) -> Decimal:
        if value < Decimal("0"):
            raise ValueError("quantity and fees must be >= 0")
        return value


class ChannelMetrics(BaseModel):
    channel_id: str
    from_date: datetime
    to_date: datetime
    total_messages: int
    parsed_signals: int
    valid_signals: int
    ignored_messages: int
    invalid_signals: int
    win_rate: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal
    max_drawdown: Decimal
    total_pnl: Decimal
    conservative_pnl: Decimal
    optimistic_pnl: Decimal
    edit_delete_penalty: Decimal

    @field_validator(
        "total_messages",
        "parsed_signals",
        "valid_signals",
        "ignored_messages",
        "invalid_signals",
    )
    @classmethod
    def non_negative_counts(cls, value: int) -> int:
        if value < 0:
            raise ValueError("count fields cannot be negative")
        return value

    @field_validator("win_rate")
    @classmethod
    def validate_win_rate(cls, value: Decimal) -> Decimal:
        if value < Decimal("0") or value > Decimal("1"):
            raise ValueError("win_rate must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> ChannelMetrics:
        if self.to_date <= self.from_date:
            raise ValueError("to_date must be after from_date")
        return self


class BacktestReport(BaseModel):
    channel_id: str
    from_date: datetime
    to_date: datetime
    initial_balance: Decimal
    final_balance: Decimal
    interval: str = "1m"
    metrics: ChannelMetrics
    trades: list[SimulatedTrade] = Field(default_factory=list)
    fill_policy: BacktestFillPolicy
    generated_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_balances_and_dates(self) -> BacktestReport:
        if self.initial_balance <= Decimal("0"):
            raise ValueError("initial_balance must be positive")
        if self.final_balance < Decimal("0"):
            raise ValueError("final_balance cannot be negative")
        if self.to_date <= self.from_date:
            raise ValueError("to_date must be after from_date")
        return self
