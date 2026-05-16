from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CacheMode = Literal["use", "refresh", "only"]
Direction = Literal["incoming", "outgoing", "any"]


class ErrorInfo(BaseModel):
    code: str
    message: str


class AccountSummary(BaseModel):
    alias: str
    label: str | None = None
    configured: bool = True


class AccountInfo(BaseModel):
    alias: str
    label: str | None = None
    account_id: str | None = None
    bank_id: str | None = None
    currency: str | None = None
    iban: str | None = None
    bic: str | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    date_start: str | None = None
    date_end: str | None = None
    id_from: int | None = None
    id_to: int | None = None


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    posted_date: str | None = None
    amount: Decimal
    currency: str | None = None
    direction: Literal["incoming", "outgoing"]
    counterparty_account: str | None = None
    counterparty_bank_code: str | None = None
    counterparty_bank_name: str | None = None
    counterparty_name: str | None = None
    constant_symbol: str | None = None
    variable_symbol: str | None = None
    specific_symbol: str | None = None
    user_identification: str | None = None
    message: str | None = None
    transaction_type: str | None = None
    performer: str | None = None
    specification: str | None = None
    comment: str | None = None
    bic: str | None = None
    order_id: str | None = None
    payer_reference: str | None = None
    raw: dict[str, Any] | None = None


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
