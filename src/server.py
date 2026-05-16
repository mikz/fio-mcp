from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, ConfigDict, Field, model_validator

from client import FioApiError, FioCacheMiss, FioClient, last_cache_key, period_cache_key
from models import (
    AccountSummary,
    CacheInfo,
    CacheMode,
    ControlTotals,
    Direction,
    ErrorInfo,
    FindTransactionsResult,
    RateLimitInfo,
    TestConnectionResult,
    Transaction,
)
from settings import load_settings


@lifespan
async def app_lifespan(_server: FastMCP):
    client = FioClient(load_settings())
    try:
        yield {"fio_client": client}
    finally:
        await client.aclose()


mcp = FastMCP("Fio Bank", lifespan=app_lifespan)


class FindTransactionsQuery(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "account": "main",
                    "date_from": "2026-05-01",
                    "date_to": "2026-05-16",
                    "direction": "incoming",
                    "currency": "CZK",
                    "variable_symbol": "2026000001",
                    "limit": 100,
                    "cursor": None,
                    "include_counterparty_details": True,
                    "include_raw": False,
                    "cache": "use",
                },
                {
                    "account": "main",
                    "date_from": "2026-05-01",
                    "date_to": "2026-05-16",
                    "cache": "only",
                },
            ]
        },
    )

    account: str = "main"
    date_from: date
    date_to: date
    direction: Direction = "any"
    currency: str | None = None
    variable_symbol: str | None = None
    constant_symbol: str | None = None
    specific_symbol: str | None = None
    counterparty_search: str | None = None
    message_search: str | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None
    include_counterparty_details: bool = True
    include_raw: bool = False
    cache: CacheMode = "use"

    @model_validator(mode="after")
    def validate_dates(self) -> FindTransactionsQuery:
        if self.date_to < self.date_from:
            raise ValueError("date_to must be on or after date_from")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "account": self.account,
                "date_from": self.date_from.isoformat(),
                "date_to": self.date_to.isoformat(),
                "direction": self.direction,
                "currency": self.currency,
                "variable_symbol": self.variable_symbol,
                "constant_symbol": self.constant_symbol,
                "specific_symbol": self.specific_symbol,
                "counterparty_search": self.counterparty_search,
                "message_search": self.message_search,
                "min_amount": str(self.min_amount) if self.min_amount is not None else None,
                "max_amount": str(self.max_amount) if self.max_amount is not None else None,
                "include_counterparty_details": self.include_counterparty_details,
                "include_raw": self.include_raw,
            }
        )


class NewTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = "main"
    confirm_advances_download_marker: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None
    include_raw: bool = False
    cache: CacheMode = "use"


class SearchCursor(BaseModel):
    v: int = 1
    kind: Literal["period", "last"]
    cache_key: str
    offset: int
    limit: int
    filter_hash: str


class ListAccountsResult(BaseModel):
    accounts: list[AccountSummary]
    rate_limit: dict[str, RateLimitInfo] = Field(default_factory=dict)


class MetadataEntry(BaseModel):
    code: str | int
    name: str
    description: str | None = None


class MetadataResult(BaseModel):
    columns: list[MetadataEntry] = Field(default_factory=list)
    error_codes: list[MetadataEntry] = Field(default_factory=list)
    cache_modes: list[MetadataEntry] = Field(default_factory=list)
    rate_limit_seconds: float
    max_period_days: int


@mcp.tool
async def fio_list_accounts(
    ctx: Context,
    include_status: bool = False,
) -> ListAccountsResult:
    """List configured Fio account aliases. Tokens are never returned."""
    client = _client_from_context(ctx)
    return _list_accounts(client, include_status=include_status)


@mcp.tool
async def fio_test_connection(
    ctx: Context,
    account: str = "main",
    cache: CacheMode = "use",
) -> TestConnectionResult:
    """Verify that a configured Fio account token can read the API."""
    return await _test_connection(_client_from_context(ctx), account=account, cache=cache)


@mcp.tool
async def fio_find_transactions(
    query: Annotated[
        FindTransactionsQuery,
        Field(
            description="Object query for Fio period transactions. Pass an object, not a string."
        ),
    ],
    ctx: Context,
) -> FindTransactionsResult:
    """Find Fio transactions for a date range without advancing Fio's last-download marker."""
    return await _find_transactions(_client_from_context(ctx), query)


@mcp.tool
async def fio_get_new_transactions(
    request: Annotated[
        NewTransactionsRequest,
        Field(
            description=(
                "Object request for Fio's last endpoint. Requires "
                "confirm_advances_download_marker=true because this endpoint advances the "
                "bank-side download marker."
            ),
        ),
    ],
    ctx: Context,
) -> FindTransactionsResult:
    """Fetch new Fio transactions using the marker-advancing last endpoint."""
    return await _get_new_transactions(_client_from_context(ctx), request)


@mcp.tool
async def fio_get_metadata(ctx: Context) -> MetadataResult:
    """Return Fio transaction column, cache, error, and rate-limit metadata."""
    client = _client_from_context(ctx)
    return MetadataResult(
        columns=[
            MetadataEntry(code=0, name="posted_date", description="Datum"),
            MetadataEntry(code=1, name="amount", description="Objem"),
            MetadataEntry(code=2, name="counterparty_account", description="Protiucet"),
            MetadataEntry(code=3, name="counterparty_bank_code", description="Kod banky"),
            MetadataEntry(code=4, name="constant_symbol", description="KS"),
            MetadataEntry(code=5, name="variable_symbol", description="VS"),
            MetadataEntry(code=6, name="specific_symbol", description="SS"),
            MetadataEntry(code=8, name="transaction_type", description="Typ"),
            MetadataEntry(code=9, name="performer", description="Provedl"),
            MetadataEntry(code=10, name="counterparty_name", description="Nazev protiuctu"),
            MetadataEntry(code=12, name="counterparty_bank_name", description="Nazev banky"),
            MetadataEntry(code=14, name="currency", description="Mena"),
            MetadataEntry(code=16, name="message", description="Zprava pro prijemce"),
            MetadataEntry(code=18, name="specification", description="Upresneni"),
            MetadataEntry(code=22, name="transaction_id", description="ID pohybu"),
            MetadataEntry(code=25, name="comment", description="Komentar"),
            MetadataEntry(code=26, name="bic", description="BIC"),
            MetadataEntry(code=27, name="payer_reference", description="Reference platce"),
        ],
        error_codes=[
            MetadataEntry(code="invalid_token_or_url", name="Invalid token or URL"),
            MetadataEntry(code="rate_limited", name="Fio returned 409 Conflict"),
            MetadataEntry(code="too_many_transactions", name="Fio returned 413"),
            MetadataEntry(code="invalid_request", name="Fio returned 422"),
            MetadataEntry(code="cache_miss", name="cache=only but no valid cache entry exists"),
            MetadataEntry(code="cursor_expired", name="Cursor snapshot expired from memory"),
        ],
        cache_modes=[
            MetadataEntry(code="use", name="Use cache, call Fio on miss"),
            MetadataEntry(code="refresh", name="Bypass cache and call Fio"),
            MetadataEntry(code="only", name="Only use cache; never call Fio"),
        ],
        rate_limit_seconds=client.rate_limit_seconds(),
        max_period_days=client.max_period_days(),
    )


def _client_from_context(ctx: Context) -> FioClient:
    return ctx.lifespan_context["fio_client"]


def _list_accounts(client: FioClient, *, include_status: bool) -> ListAccountsResult:
    accounts = [
        AccountSummary(alias=account.alias, label=account.label, configured=True)
        for account in client.accounts()
    ]
    rate_limit = {}
    if include_status:
        for account in client.accounts():
            rate_limit[account.alias] = client.rate_limit_status(account.alias)
    return ListAccountsResult(accounts=accounts, rate_limit=rate_limit)


async def _test_connection(
    client: FioClient,
    *,
    account: str,
    cache: CacheMode,
) -> TestConnectionResult:
    try:
        result = await client.test_connection(account, cache_mode=cache)
        return TestConnectionResult(
            ok=True,
            account=result.statement.account,
            cache=result.cache,
            rate_limit=result.rate_limit,
        )
    except FioCacheMiss as exc:
        return TestConnectionResult(
            ok=False,
            cache=exc.cache_info,
            error=ErrorInfo(code="cache_miss", message=str(exc)),
        )
    except Exception as exc:
        return TestConnectionResult(
            ok=False,
            cache=CacheInfo(mode=cache, hit=False),
            error=_error_info(exc),
        )


async def _find_transactions(
    client: FioClient, query: FindTransactionsQuery
) -> FindTransactionsResult:
    if (query.date_to - query.date_from).days + 1 > client.max_period_days():
        return FindTransactionsResult(
            cache=CacheInfo(mode=query.cache, hit=False),
            error=ErrorInfo(
                code="period_too_large",
                message=f"Date range exceeds FIO_MAX_PERIOD_DAYS={client.max_period_days()}",
            ),
        )

    filter_hash = query.filter_hash()
    if query.cursor:
        return _page_from_cursor(
            client,
            cursor=query.cursor,
            expected_kind="period",
            expected_filter_hash=filter_hash,
            limit=query.limit,
            include_counterparty_details=query.include_counterparty_details,
            cache_mode=query.cache,
            query=query,
        )

    try:
        result = await client.period(
            query.account,
            query.date_from,
            query.date_to,
            cache_mode=query.cache,
            include_raw=query.include_raw,
        )
    except FioCacheMiss as exc:
        return FindTransactionsResult(
            cache=exc.cache_info,
            error=ErrorInfo(code="cache_miss", message=str(exc)),
        )
    except Exception as exc:
        return FindTransactionsResult(
            cache=CacheInfo(mode=query.cache, hit=False),
            error=_error_info(exc),
        )

    transactions = _filter_transactions(result.statement.transactions, query)
    return _page_result(
        account=result.statement.account,
        transactions=transactions,
        cache=result.cache,
        rate_limit=result.rate_limit,
        cache_key=period_cache_key(
            query.account,
            query.date_from,
            query.date_to,
            include_raw=query.include_raw,
        ),
        kind="period",
        filter_hash=filter_hash,
        offset=0,
        limit=query.limit,
        include_counterparty_details=query.include_counterparty_details,
    )


async def _get_new_transactions(
    client: FioClient,
    request: NewTransactionsRequest,
) -> FindTransactionsResult:
    if not request.confirm_advances_download_marker:
        return FindTransactionsResult(
            cache=CacheInfo(mode=request.cache, hit=False),
            error=ErrorInfo(
                code="confirmation_required",
                message=(
                    "Fio's last endpoint advances the bank-side download marker; pass "
                    "confirm_advances_download_marker=true to call it."
                ),
            ),
        )

    filter_hash = _filter_hash(
        {
            "account": request.account,
            "include_raw": request.include_raw,
        }
    )
    if request.cursor:
        return _page_from_cursor(
            client,
            cursor=request.cursor,
            expected_kind="last",
            expected_filter_hash=filter_hash,
            limit=request.limit,
            include_counterparty_details=True,
            cache_mode=request.cache,
        )

    try:
        result = await client.last(
            request.account,
            cache_mode=request.cache,
            include_raw=request.include_raw,
        )
    except FioCacheMiss as exc:
        return FindTransactionsResult(
            cache=exc.cache_info,
            error=ErrorInfo(code="cache_miss", message=str(exc)),
        )
    except Exception as exc:
        return FindTransactionsResult(
            cache=CacheInfo(mode=request.cache, hit=False),
            error=_error_info(exc),
        )

    return _page_result(
        account=result.statement.account,
        transactions=result.statement.transactions,
        cache=result.cache,
        rate_limit=result.rate_limit,
        cache_key=last_cache_key(request.account, include_raw=request.include_raw),
        kind="last",
        filter_hash=filter_hash,
        offset=0,
        limit=request.limit,
        include_counterparty_details=True,
    )


def _page_from_cursor(
    client: FioClient,
    *,
    cursor: str,
    expected_kind: Literal["period", "last"],
    expected_filter_hash: str,
    limit: int,
    include_counterparty_details: bool,
    cache_mode: CacheMode,
    query: FindTransactionsQuery | None = None,
) -> FindTransactionsResult:
    try:
        parsed = _decode_cursor(cursor)
    except ValueError as exc:
        return FindTransactionsResult(
            cache=CacheInfo(mode=cache_mode, hit=False),
            error=ErrorInfo(code="invalid_cursor", message=str(exc)),
        )
    if parsed.kind != expected_kind or parsed.filter_hash != expected_filter_hash:
        return FindTransactionsResult(
            cache=CacheInfo(mode=cache_mode, hit=False, key=parsed.cache_key),
            error=ErrorInfo(code="cursor_mismatch", message="Cursor does not match this query"),
        )

    snapshot = client.cache().get_snapshot(parsed.cache_key)
    if snapshot is None:
        return FindTransactionsResult(
            cache=CacheInfo(mode=cache_mode, hit=False, key=parsed.cache_key),
            error=ErrorInfo(code="cursor_expired", message="Cursor snapshot expired from memory"),
        )
    transactions = (
        _filter_transactions(snapshot.transactions, query) if query else snapshot.transactions
    )
    return _page_result(
        account=snapshot.account,
        transactions=transactions,
        cache=CacheInfo(mode=cache_mode, hit=True, key=parsed.cache_key),
        rate_limit=RateLimitInfo(),
        cache_key=parsed.cache_key,
        kind=parsed.kind,
        filter_hash=parsed.filter_hash,
        offset=parsed.offset,
        limit=limit,
        include_counterparty_details=include_counterparty_details,
    )


def _filter_transactions(
    transactions: list[Transaction], query: FindTransactionsQuery
) -> list[Transaction]:
    result = transactions
    if query.direction != "any":
        result = [transaction for transaction in result if transaction.direction == query.direction]
    if query.currency:
        currency = query.currency.upper()
        result = [
            transaction
            for transaction in result
            if (transaction.currency or "").upper() == currency
        ]
    if query.variable_symbol:
        result = [
            transaction
            for transaction in result
            if transaction.variable_symbol == query.variable_symbol
        ]
    if query.constant_symbol:
        result = [
            transaction
            for transaction in result
            if transaction.constant_symbol == query.constant_symbol
        ]
    if query.specific_symbol:
        result = [
            transaction
            for transaction in result
            if transaction.specific_symbol == query.specific_symbol
        ]
    if query.min_amount is not None:
        result = [
            transaction for transaction in result if abs(transaction.amount) >= query.min_amount
        ]
    if query.max_amount is not None:
        result = [
            transaction for transaction in result if abs(transaction.amount) <= query.max_amount
        ]
    if query.counterparty_search:
        needle = query.counterparty_search.casefold()
        result = [
            transaction
            for transaction in result
            if needle
            in " ".join(
                [
                    transaction.counterparty_name or "",
                    transaction.counterparty_account or "",
                    transaction.counterparty_bank_code or "",
                ]
            ).casefold()
        ]
    if query.message_search:
        needle = query.message_search.casefold()
        result = [
            transaction
            for transaction in result
            if needle in " ".join([transaction.message or "", transaction.comment or ""]).casefold()
        ]
    return result


def _page_result(
    *,
    account: Any,
    transactions: list[Transaction],
    cache: CacheInfo,
    rate_limit: RateLimitInfo,
    cache_key: str,
    kind: Literal["period", "last"],
    filter_hash: str,
    offset: int,
    limit: int,
    include_counterparty_details: bool,
) -> FindTransactionsResult:
    page = transactions[offset : offset + limit]
    if not include_counterparty_details:
        page = [_redact_counterparty(transaction) for transaction in page]
    next_offset = offset + limit
    next_cursor = None
    if next_offset < len(transactions):
        next_cursor = _encode_cursor(
            SearchCursor(
                kind=kind,
                cache_key=cache_key,
                offset=next_offset,
                limit=limit,
                filter_hash=filter_hash,
            )
        )
    return FindTransactionsResult(
        account=account,
        transactions=page,
        next_cursor=next_cursor,
        control_totals=_control_totals(transactions),
        cache=cache,
        rate_limit=rate_limit,
    )


def _redact_counterparty(transaction: Transaction) -> Transaction:
    data = transaction.model_dump()
    for key in (
        "counterparty_account",
        "counterparty_bank_code",
        "counterparty_bank_name",
        "counterparty_name",
        "bic",
    ):
        data[key] = None
    return Transaction.model_validate(data)


def _control_totals(transactions: list[Transaction]) -> ControlTotals:
    by_currency: dict[str, dict[str, str | int]] = {}
    for transaction in transactions:
        currency = transaction.currency or "unknown"
        bucket = by_currency.setdefault(currency, {"count": 0, "incoming": "0", "outgoing": "0"})
        bucket["count"] = int(bucket["count"]) + 1
        key = "incoming" if transaction.direction == "incoming" else "outgoing"
        bucket[key] = str(Decimal(str(bucket[key])) + abs(transaction.amount))
    return ControlTotals(
        count=len(transactions),
        incoming_count=sum(
            1 for transaction in transactions if transaction.direction == "incoming"
        ),
        outgoing_count=sum(
            1 for transaction in transactions if transaction.direction == "outgoing"
        ),
        by_currency=by_currency,
    )


def _encode_cursor(cursor: SearchCursor) -> str:
    payload = cursor.model_dump(mode="json")
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> SearchCursor:
    padding = "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
        return SearchCursor.model_validate(payload)
    except Exception as exc:
        raise ValueError("Cursor is not valid") from exc


def _filter_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _error_info(exc: Exception) -> ErrorInfo:
    if isinstance(exc, FioApiError):
        return ErrorInfo(code=exc.code, message=str(exc))
    if isinstance(exc, httpx.RequestError):
        return ErrorInfo(code="network_error", message="Fio API network request failed")
    return ErrorInfo(code="error", message=str(exc))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
