"""Backtesting models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from triak_trade.domain.enums import BacktestFillPolicy, SignalAction
from triak_trade.domain.models import ParsedSignal
from triak_trade.market_data.intervals import validate_interval


class BacktestRequest(BaseModel):
    channel: str
    from_date: datetime
    to_date: datetime
    initial_balance: Decimal
    interval: str
    fill_policy: BacktestFillPolicy
    risk_per_trade_pct: Decimal
    use_ai_classifier: bool
    use_regex_fallback: bool
    max_messages: int | None
    symbols: list[str] | None

    @field_validator("interval")
    @classmethod
    def validate_interval_value(cls, value: str) -> str:
        return validate_interval(value)

    @model_validator(mode="after")
    def validate_values(self) -> BacktestRequest:
        if self.to_date <= self.from_date:
            raise ValueError("to_date must be after from_date")
        if self.initial_balance <= Decimal("0"):
            raise ValueError("initial_balance must be positive")
        if self.risk_per_trade_pct <= Decimal("0"):
            raise ValueError("risk_per_trade_pct must be positive")
        return self


class BacktestEvent(BaseModel):
    timestamp: datetime
    action: SignalAction
    signal_id: str | None
    parsed_signal: ParsedSignal
    related_signal_id: str | None
    debug_notes: list[str]
    source_message_id: int | None = None
    source_text: str | None = None
    close_fraction: Decimal | None = None
    close_all: bool = False
    move_stop_to_entry: bool = False
    leverage: int | None = None

    @field_validator("leverage")
    @classmethod
    def validate_leverage(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("leverage must be positive")
        return value

    @field_validator("close_fraction")
    @classmethod
    def validate_close_fraction(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if value <= Decimal("0") or value > Decimal("1"):
            raise ValueError("close_fraction must be between 0 and 1")
        return value
