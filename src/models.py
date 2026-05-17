from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

CacheMode = Literal["use", "refresh", "only"]
Direction = Literal["incoming", "outgoing", "any"]


class ErrorInfo(BaseModel):
    code: str
    message: str
    retry_after_seconds: float | None = None
    next_available_at: datetime | None = None


class AccountSummary(BaseModel):
    account: str
    alias: str | None = None
    bank_account: str | None = None
    currency: str | None = None
    iban: str | None = None
    token_count: int
    tokens: list[TokenSummary] = Field(default_factory=list)


class TokenSummary(BaseModel):
    token_key: str
    available: bool | None = None
    next_available_at: datetime | None = None


class TokenPoolStatus(BaseModel):
    token_count: int
    available_tokens: int
    next_available_at: datetime | None = None


class AccountInfo(BaseModel):
    account: str | None = None
    alias: str | None = None
    bank_account: str | None = None
    currency: str | None = None
    iban: str | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    date_start: str | None = None
    date_end: str | None = None
    id_from: int | None = None
    id_to: int | None = None

    @field_serializer("opening_balance", "closing_balance", when_used="json")
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return _format_decimal(value)


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    posted_date: str | None = None
    amount: Decimal
    currency: str | None = None
    direction: Literal["incoming", "outgoing"]
    counterparty_bank_account: str | None = None
    counterparty_name: str | None = None
    constant_symbol: str | None = None
    variable_symbol: str | None = None
    specific_symbol: str | None = None
    user_identification: str | None = None
    message: str | None = None
    transaction_type: str | None = None
    specification: str | None = None
    comment: str | None = None
    order_id: str | None = None
    raw: dict[str, Any] | None = None

    @field_serializer("amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> str:
        return _format_decimal(value) or "0.00"


class AccountStatement(BaseModel):
    account: AccountInfo
    transactions: list[Transaction] = Field(default_factory=list)


class CacheInfo(BaseModel):
    mode: CacheMode
    hit: bool
    key: str | None = None
    cached_at: datetime | None = None
    expires_at: datetime | None = None


class RateLimitInfo(BaseModel):
    consumed_lease: bool = False
    waited_seconds: float = 0
    next_available_at: datetime | None = None


class ControlTotals(BaseModel):
    count: int
    incoming_count: int
    outgoing_count: int
    by_currency: dict[str, dict[str, str | int]] = Field(default_factory=dict)


class FindTransactionsResult(BaseModel):
    account: AccountInfo | None = None
    transactions: list[Transaction] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    cache: CacheInfo
    rate_limit: RateLimitInfo = Field(default_factory=RateLimitInfo)
    error: ErrorInfo | None = None


class TestConnectionResult(BaseModel):
    ok: bool
    account: AccountInfo | None = None
    cache: CacheInfo
    rate_limit: RateLimitInfo = Field(default_factory=RateLimitInfo)
    error: ErrorInfo | None = None


def _format_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))
