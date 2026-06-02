"""Exchange base contracts and models."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, field_validator


class ExchangeOrderRequest(BaseModel):
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None = None

    @field_validator("symbol", "side", "order_type")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("value cannot be empty")
        return normalized


class ExchangeOrderTestResult(BaseModel):
    accepted: bool
    symbol: str
    side: str
    order_type: str
    status: str
    detail: str | None = None


class ExchangeHealthResult(BaseModel):
    success: bool
    provider: str
    detail: str


class SignedCheckResult(BaseModel):
    success: bool
    skipped: bool
    endpoint_path: str | None
    response_type: str | None
    key_accepted: bool | None
    message: str
