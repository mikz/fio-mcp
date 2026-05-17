from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

import httpx
from fastmcp import FastMCP
from fastmcp.apps import UI_EXTENSION_ID
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.lifespan import lifespan
from prefab_ui.actions import Fetch, SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import Button, Column, Form, Heading, Input, Muted, Text
from prefab_ui.rx import Rx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from client import FioApiError, FioCacheMiss, FioClient, last_cache_key, period_cache_key
from normalization import MESSAGE_PATTERNS, MessagePattern
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
    TokenPoolStatus,
    TokenSummary,
    Transaction,
)
from settings import Settings, load_settings

DetailLevel = Literal["summary", "counterparty", "full", "raw"]
MAX_PAGE_LIMIT = 500
_LIVE_CLIENT: FioClient | None = None


@lifespan
async def app_lifespan(_server: FastMCP):
    global _LIVE_CLIENT
    client = await _client_from_settings(load_settings())
    _LIVE_CLIENT = client
    try:
        yield {"fio_client": client}
    finally:
        _LIVE_CLIENT = None
        await client.aclose()


mcp = FastMCP("Fio Bank", lifespan=app_lifespan)
LoginMode = Literal["auto", "direct", "prefab", "web"]
ResolvedLoginMode = Literal["direct", "prefab", "web"]
_WEB_LOGIN_SERVERS: dict[str, ThreadingHTTPServer] = {}


async def _client_from_settings(settings: Settings) -> FioClient:
    client = FioClient(settings)
    try:
        for token in settings.startup_tokens():
            await client.add_token(token.get_secret_value())
    except Exception:
        await client.aclose()
        raise
    return client


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
                    "counterparty_bank_account": "2198370339/0800",
                    "limit": 100,
                    "cursor": None,
                    "detail_level": "summary",
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

    account: str | None = Field(
        default=None,
        description=(
            "Account selector — either the alias from fio_login/fio_alias_account or the "
            "bank_account string (e.g. '2603445200/2010'). Required when more than one "
            "account is configured; omit otherwise."
        ),
    )
    date_from: date = Field(
        description=(
            "Inclusive start of the period (ISO YYYY-MM-DD). The Fio API returns up to "
            "50000 transactions per response (HTTP 413 'too_many_transactions' if "
            "exceeded); for typical small organisations this is unreachable for years of "
            "history in one call."
        ),
    )
    date_to: date = Field(
        description="Inclusive end of the period (ISO YYYY-MM-DD). Must be on or after date_from.",
    )
    direction: Direction = Field(
        default="any",
        description=(
            "Local direction filter: 'incoming', 'outgoing', or 'any' (default). Applied "
            "after the Fio period read; does not reduce Fio API load."
        ),
    )
    currency: str | None = Field(
        default=None,
        description="Exact-match currency filter (case-insensitive), e.g. 'CZK' or 'EUR'.",
    )
    variable_symbol: str | None = Field(
        default=None,
        description=(
            "Exact-match filter on the Fio variable_symbol (VS) field. Use for "
            "reconciling a bank payment to an invoice / order / Darujme payout VS."
        ),
    )
    constant_symbol: str | None = Field(
        default=None,
        description="Exact-match filter on the Fio constant_symbol (KS).",
    )
    specific_symbol: str | None = Field(
        default=None,
        description="Exact-match filter on the Fio specific_symbol (SS).",
    )
    counterparty_bank_account: str | None = Field(
        default=None,
        description=(
            "Exact-match filter on the counterparty bank account in 'account/bank_code' "
            "form (e.g. '193181046/0300')."
        ),
    )
    counterparty_name: str | None = Field(
        default=None,
        description=(
            "Exact-match filter on counterparty_name. Note: counterparty_name is populated "
            "only at detail_level='full' or 'raw' (post-fetch local filter), so a request "
            "at detail_level='summary' or 'counterparty' may silently match nothing."
        ),
    )
    counterparty_search: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring search across counterparty_name and "
            "counterparty_bank_account. NOT an exact match. Same detail_level caveat as "
            "counterparty_name applies."
        ),
    )
    message_search: str | None = Field(
        default=None,
        description=(
            "Case-insensitive substring search across the `message` and `comment` fields. "
            "NOT an exact match. Example: 'SM PRODUCTION' matches 'Z920260035 "
            "SM PRODUCTION S.R.O.'. Note: message/comment populated only at "
            "detail_level='full' or 'raw' — use one of those when filtering on text."
        ),
    )
    min_amount: Decimal | None = Field(
        default=None,
        description=(
            "Filter to transactions whose absolute amount is at least this value. Decimal "
            "in the transaction currency."
        ),
    )
    max_amount: Decimal | None = Field(
        default=None,
        description=(
            "Filter to transactions whose absolute amount is at most this value. Decimal "
            "in the transaction currency."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum transactions returned per page (1–500, default 100).",
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor from a previous response's `next_cursor`. Pass back with "
            "the same filter set — the server rejects the cursor if filters drift "
            "between calls (audit safety)."
        ),
    )
    detail_level: DetailLevel | None = Field(
        default=None,
        description=(
            "Controls Transaction field visibility. Defaults to 'full'. For card payments "
            "and bank transfers with payee info in the message use 'full' or 'raw' — "
            "'summary' and 'counterparty' hide `user_identification`, `message`, `comment`, "
            "`specification`, and the derived `payee_hint`. Call fio_get_metadata for "
            "the complete `detail_level_visibility` matrix."
        ),
    )
    cache: CacheMode = Field(
        default="use",
        description=(
            "'use' (default) returns cached data if fresh, else calls Fio. 'refresh' "
            "ignores cache and always calls Fio. 'only' fails with cache_miss if no fresh "
            "snapshot exists. See fio_get_metadata for TTLs."
        ),
    )
    max_wait_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Maximum seconds this call may wait for a Fio token lease. If the local "
            "rate-limit wait would be longer, the tool returns rate_limit_wait_required."
        ),
    )

    @model_validator(mode="after")
    def validate_dates(self) -> FindTransactionsQuery:
        if self.date_to < self.date_from:
            raise ValueError("date_to must be on or after date_from")
        return self

    def filter_hash(self, account_key: str | None = None) -> str:
        return _filter_hash(
            {
                "account": account_key or self.account,
                "date_from": self.date_from.isoformat(),
                "date_to": self.date_to.isoformat(),
                "direction": self.direction,
                "currency": self.currency,
                "variable_symbol": self.variable_symbol,
                "constant_symbol": self.constant_symbol,
                "specific_symbol": self.specific_symbol,
                "counterparty_bank_account": self.counterparty_bank_account,
                "counterparty_name": self.counterparty_name,
                "counterparty_search": self.counterparty_search,
                "message_search": self.message_search,
                "min_amount": str(self.min_amount) if self.min_amount is not None else None,
                "max_amount": str(self.max_amount) if self.max_amount is not None else None,
                "detail_level": self.effective_detail_level(),
                "include_raw": self.effective_include_raw(),
            }
        )

    def effective_detail_level(self) -> DetailLevel:
        if self.detail_level is not None:
            return self.detail_level
        return "full"

    def effective_include_raw(self) -> bool:
        return self.detail_level == "raw"


class NewTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(
        default=None,
        description=(
            "Account selector (alias or bank_account). Required when more than one "
            "account is configured."
        ),
    )
    confirm_advances_download_marker: bool = Field(
        default=False,
        description=(
            "Required acknowledgement that this tool will advance Fio's bank-side "
            "last-download marker — subsequent calls skip transactions returned here. "
            "Pass `true` only when you genuinely want one-shot ingestion (e.g. a nightly "
            "sync). For repeatable queries on a date range, use fio_find_transactions."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum transactions returned per page (1–500, default 100).",
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor from a previous response's `next_cursor`. Pass back with "
            "the same request to fetch the next page; cursor invalidates if the snapshot "
            "expires (returns cursor_expired — repeat the original request)."
        ),
    )
    detail_level: DetailLevel | None = Field(
        default=None,
        description=(
            "Controls Transaction field visibility. Defaults to 'full'. See "
            "fio_find_transactions for the full explanation and fio_get_metadata for the "
            "`detail_level_visibility` matrix."
        ),
    )
    cache: CacheMode = Field(
        default="use",
        description=(
            "'use' (default), 'refresh', or 'only'. See fio_find_transactions for "
            "semantics."
        ),
    )
    max_wait_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Maximum seconds this call may wait for a Fio token lease. If the local "
            "rate-limit wait would be longer, the tool returns rate_limit_wait_required."
        ),
    )

    def effective_detail_level(self) -> DetailLevel:
        if self.detail_level is not None:
            return self.detail_level
        return "full"

    def effective_include_raw(self) -> bool:
        return self.detail_level == "raw"


class FioTokenSetup(BaseModel):
    """Fio API token setup. Stored locally and never returned by Fio MCP."""

    token: str = Field(description="Fio API token from Fio internet banking")
    alias: str | None = Field(
        default=None,
        description="Optional friendly account alias, for example main",
    )


class LoginResult(BaseModel):
    ok: bool
    mode: ResolvedLoginMode
    status: Literal["logged_in", "needs_input", "unsupported", "error"]
    message: str
    url: str | None = None
    transport: str | None = None
    ui_supported: bool = False


class SearchCursor(BaseModel):
    v: int = 1
    kind: Literal["period", "last"]
    cache_key: str
    offset: int
    limit: int
    filter_hash: str


class ListAccountsResult(BaseModel):
    accounts: list[AccountSummary]
    rate_limit: dict[str, TokenPoolStatus] = Field(default_factory=dict)


class AddTokenResult(BaseModel):
    ok: bool
    account: str
    bank_account: str | None = None
    alias: str | None = None
    token_key: str
    token_count: int
    added: bool


class AliasAccountResult(BaseModel):
    ok: bool
    account: str
    bank_account: str | None = None
    alias: str


class RemoveTokenResult(BaseModel):
    ok: bool
    account: str
    bank_account: str | None = None
    token_count: int
    removed: bool


class MetadataEntry(BaseModel):
    code: str | int
    name: str
    description: str | None = None


class MetadataLimits(BaseModel):
    max_page_limit: int
    rate_limit_seconds: float
    max_wait_seconds_option: str = "Pass max_wait_seconds on read tools to avoid blocking."


class MetadataSideEffect(BaseModel):
    tool: str
    side_effect: str
    guard: str | None = None
    description: str


class MetadataResult(BaseModel):
    source_documents: list[str] = Field(default_factory=list)
    comparison_fields: dict[str, Any] = Field(default_factory=dict)
    setup_tools: list[str] = Field(default_factory=list)
    account_selection: dict[str, Any] = Field(default_factory=dict)
    columns: list[MetadataEntry] = Field(default_factory=list)
    error_codes: list[MetadataEntry] = Field(default_factory=list)
    cache_modes: list[MetadataEntry] = Field(default_factory=list)
    detail_levels: list[MetadataEntry] = Field(default_factory=list)
    detail_level_visibility: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Maps each `detail_level` to the Transaction fields populated at that level. "
            "Use to decide which detail_level satisfies your downstream task without "
            "fetching a sample. Field-membership is inclusive: lower levels are subsets "
            "of higher ones."
        ),
    )
    directions: list[MetadataEntry] = Field(default_factory=list)
    message_patterns: list[MessagePattern] = Field(
        default_factory=list,
        description=(
            "Documented Fio `message` prefixes (QR Objednavka, PLATBA DARUJMECZ, Nákup card "
            "purchases, Z-reference transfers, ...). Apply these patterns to a transaction's "
            "`message` to classify the transaction or extract a counterparty name when "
            "`counterparty_name` is empty. The same patterns power the derived `payee_hint`."
        ),
    )
    limits: MetadataLimits
    side_effects: list[MetadataSideEffect] = Field(default_factory=list)
    rate_limit_seconds: float


COLUMN_METADATA = [
    MetadataEntry(code=0, name="posted_date", description="Datum"),
    MetadataEntry(code=1, name="amount", description="Objem"),
    MetadataEntry(code="2/3", name="counterparty_bank_account", description="Protiucet/Kod banky"),
    MetadataEntry(code=4, name="constant_symbol", description="KS"),
    MetadataEntry(code=5, name="variable_symbol", description="VS"),
    MetadataEntry(code=6, name="specific_symbol", description="SS"),
    MetadataEntry(
        code=7,
        name="user_identification",
        description=(
            "Uzivatelska identifikace. For card purchases this carries merchant info "
            "(matches the `Nákup:` message_pattern)."
        ),
    ),
    MetadataEntry(
        code=8,
        name="transaction_type",
        description=(
            "Typ operace (e.g. `Platba kartou`, `Bezhotovostni prijem`). Classifies the "
            "transaction kind. NOT a payee identifier."
        ),
    ),
    MetadataEntry(
        code=9,
        name="initiated_by_name",
        description=(
            "Provedl — name of the person who entered the payment order at your end (your "
            "operator). NOT the counterparty. Do not use for payee matching; for that see "
            "`counterparty_name` (column 10) or the derived `payee_hint`."
        ),
    ),
    MetadataEntry(
        code=10,
        name="counterparty_name",
        description=(
            "Nazev protiuctu — counterparty name (payee for outgoing, payer for incoming). "
            "May be empty, especially for card purchases. When empty, recover the party "
            "from `message` (column 16) via the documented `message_patterns`, or rely on "
            "the derived `payee_hint` which already does this."
        ),
    ),
    MetadataEntry(code=14, name="currency", description="Mena"),
    MetadataEntry(code=16, name="message", description="Zprava pro prijemce"),
    MetadataEntry(code=18, name="specification", description="Upresneni"),
    MetadataEntry(code=22, name="transaction_id", description="ID pohybu"),
    MetadataEntry(code=25, name="comment", description="Komentar"),
]
ERROR_CODE_METADATA = [
    MetadataEntry(
        code="invalid_token_or_url",
        name="Invalid token or URL",
        description="Check the Fio API token and base URL.",
    ),
    MetadataEntry(
        code="rate_limited",
        name="Fio returned 409 Conflict",
        description="Retry after the local cooldown or use cache where possible.",
    ),
    MetadataEntry(
        code="too_many_transactions",
        name="Fio returned 413",
        description="Reduce the date range or add narrower filters.",
    ),
    MetadataEntry(
        code="invalid_request",
        name="Fio returned 422",
        description="Check dates, account configuration, and request parameters.",
    ),
    MetadataEntry(
        code="cache_miss",
        name="cache=only but no valid cache entry exists",
        description="Retry with cache=use or refresh if calling Fio is acceptable.",
    ),
    MetadataEntry(
        code="cursor_expired",
        name="Cursor snapshot expired from memory",
        description="Repeat the original query to create a fresh cursor snapshot.",
    ),
    MetadataEntry(
        code="confirmation_required",
        name="last endpoint confirmation missing",
        description=(
            "Pass confirm_advances_download_marker=true only when advancing the marker is intended."
        ),
    ),
    MetadataEntry(
        code="rate_limit_wait_required",
        name="Local Fio rate-limit wait would exceed max_wait_seconds",
        description="Retry after error.retry_after_seconds or use an existing cache entry.",
    ),
    MetadataEntry(
        code="invalid_cursor",
        name="Cursor cannot be decoded",
        description="Discard the cursor and repeat the original query.",
    ),
    MetadataEntry(
        code="cursor_mismatch",
        name="Cursor does not match the request",
        description=(
            "Repeat the same filters and response-shaping options used to create the cursor."
        ),
    ),
    MetadataEntry(
        code="unknown_account",
        name="Unknown configured account",
        description="Call fio_list_accounts and retry with a listed alias or bank_account.",
    ),
    MetadataEntry(
        code="not_configured",
        name="No Fio account tokens are configured",
        description="Call fio_login first.",
    ),
    MetadataEntry(
        code="ambiguous_account",
        name="Account parameter is required",
        description="More than one Fio account is configured; pass account explicitly.",
    ),
    MetadataEntry(
        code="unknown_token",
        name="Unknown token key",
        description="Call fio_list_accounts with include_tokens=true and retry.",
    ),
    MetadataEntry(
        code="last_token",
        name="Cannot remove last account token",
        description="Add another token before removing this token.",
    ),
    MetadataEntry(
        code="network_error",
        name="Fio API network request failed",
        description="Retry after checking connectivity and Fio API availability.",
    ),
    MetadataEntry(
        code="error",
        name="Unexpected server error",
        description="Generic fallback for unexpected local failures.",
    ),
]
CACHE_MODE_METADATA = [
    MetadataEntry(code="use", name="Use cache, call Fio on miss"),
    MetadataEntry(code="refresh", name="Bypass cache and call Fio"),
    MetadataEntry(code="only", name="Only use cache; never call Fio"),
]
DETAIL_LEVEL_METADATA = [
    MetadataEntry(
        code="summary",
        name="Payment summary fields only",
        description=(
            "Identifiers, amount, dates, symbols, and canonical counterparty bank "
            "account. Hides counterparty_name, message, comment, user_identification, "
            "specification, and the derived payee_hint. Use for narrow reconciliation."
        ),
    ),
    MetadataEntry(
        code="counterparty",
        name="Summary plus counterparty name",
        description=(
            "Adds `counterparty_name`. Still hides message, comment, user_identification, "
            "specification, and payee_hint. Insufficient for card transactions whose "
            "payee lives in user_identification or message."
        ),
    ),
    MetadataEntry(
        code="full",
        name="All normalized non-raw fields",
        description=(
            "Adds message, comment, user_identification, specification, and the derived "
            "`payee_hint`. Required for accounting / payee identification of card payments "
            "and message-only transfers. Excludes only the original Fio JSON payload."
        ),
    ),
    MetadataEntry(
        code="raw",
        name="Full plus raw Fio payload",
        description="Requires the raw Fio response and exposes it in transactions[].raw",
    ),
]
DIRECTION_METADATA = [
    MetadataEntry(code="incoming", name="Incoming payments"),
    MetadataEntry(code="outgoing", name="Outgoing payments"),
    MetadataEntry(code="any", name="Incoming and outgoing payments"),
]
SIDE_EFFECT_METADATA = [
    MetadataSideEffect(
        tool="fio_get_new_transactions",
        side_effect="advances_bank_download_marker",
        guard="confirm_advances_download_marker=true",
        description=(
            "Calls Fio's last endpoint, which advances the bank-side last-download marker."
        ),
    ),
]


def _add_token_on_submit(setup: FioTokenSetup) -> str:
    try:
        client = _LIVE_CLIENT or FioClient(load_settings())
        close_client = _LIVE_CLIENT is None
        try:
            result = client.add_token_sync(setup.token)
            alias = setup.alias.strip() if setup.alias else None
            if alias:
                alias_result = client.alias_account(result.account_key, alias)
                result = AddTokenResult(
                    ok=True,
                    account=alias_result.account,
                    bank_account=alias_result.bank_account,
                    alias=alias_result.alias,
                    token_key=result.token_key,
                    token_count=result.token_count,
                    added=result.added,
                )
        finally:
            if close_client:
                asyncio.run(client.aclose())

        action = "Added" if result.added else "Already configured"
        alias_part = f" Alias: {result.alias}." if result.alias else ""
        return (
            f"{action} Fio token and paired it to account {result.account}. "
            f"Token key: {result.token_key}. Token count: {result.token_count}."
            f"{alias_part}"
        )
    except Exception as exc:
        info = _error_info(exc)
        return f"{info.code}: {info.message}"


async def fio_login(
    ctx: Context,
    mode: LoginMode = "auto",
    credentials: Annotated[
        FioTokenSetup | None,
        Field(
            description=(
                "Token setup for mode=direct. Omit for auto, prefab, or web. "
                "Direct mode sends the token through the MCP tool call."
            )
        ),
    ] = None,
) -> LoginResult | PrefabApp:
    """Add a Fio API token using auto, direct, Prefab UI, or localhost web login."""
    selected = _resolve_login_mode(ctx, mode)
    if selected == "prefab":
        if not ctx.client_supports_extension(UI_EXTENSION_ID):
            return _login_result(
                ctx,
                mode="prefab",
                status="unsupported",
                message="This MCP client does not advertise the Apps UI extension.",
                ok=False,
            )
        return _fio_login_prefab_app()
    if selected == "web":
        url = _start_fio_web_login()
        return _login_result(
            ctx,
            mode="web",
            status="needs_input",
            message=f"Open this local URL in a browser to add a Fio token: {url}",
            ok=True,
            url=url,
        )
    if credentials is None:
        return _login_result(
            ctx,
            mode="direct",
            status="error",
            message=(
                "mode=direct accepts credentials in the tool call and requires "
                "credentials.token; credentials.alias is optional."
            ),
            ok=False,
        )
    return _login_result_from_submit(ctx, "direct", _add_token_on_submit(credentials))


def _resolve_login_mode(ctx: Context, mode: LoginMode) -> ResolvedLoginMode:
    if mode != "auto":
        return mode
    if ctx.client_supports_extension(UI_EXTENSION_ID):
        return "prefab"
    return "web"


def _login_result(
    ctx: Context,
    *,
    mode: ResolvedLoginMode,
    status: Literal["logged_in", "needs_input", "unsupported", "error"],
    message: str,
    ok: bool,
    url: str | None = None,
) -> LoginResult:
    return LoginResult(
        ok=ok,
        mode=mode,
        status=status,
        message=message,
        url=url,
        transport=ctx.transport,
        ui_supported=ctx.client_supports_extension(UI_EXTENSION_ID),
    )


def _login_result_from_submit(ctx: Context, mode: ResolvedLoginMode, message: str) -> LoginResult:
    ok = message.startswith(("Added Fio token", "Already configured Fio token"))
    return _login_result(
        ctx,
        mode=mode,
        status="logged_in" if ok else "error",
        message=message,
        ok=ok,
    )


def _fio_login_prefab_app(web_submit_url: str | None = None) -> PrefabApp:
    credentials = {"token": Rx("token"), "alias": Rx("alias")}
    if web_submit_url:
        submit_action = Fetch(
            web_submit_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=credentials,
            onSuccess=[
                SetState("message", "{{ $result.message }}"),
                ShowToast("{{ $result.message }}", variant="success"),
            ],
            onError=ShowToast("Fio token setup failed.", variant="error"),
        )
    else:
        submit_action = CallTool(
            "fio_login",
            arguments={"mode": "direct", "credentials": credentials},
            onSuccess=[
                SetState("message", "{{ $result.message }}"),
                ShowToast("{{ $result.message }}", variant="success"),
            ],
            onError=ShowToast("Fio token setup failed.", variant="error"),
        )

    with Column(gap=4, css_class="p-6 max-w-md") as view:
        Heading("Add Fio API token", level=2)
        Muted("The token is validated with Fio and stored locally for this MCP scope.")
        with Form(onSubmit=submit_action):
            Input(name="token", inputType="password", placeholder="Fio API token", required=True)
            Input(name="alias", placeholder="Optional account alias")
            Button("Add token", buttonType="submit")
        Text(content=Rx("message"))
    return PrefabApp(title="Fio Login", view=view, state={"message": ""})


class _FioLoginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        token = getattr(server, "login_token", "")
        if self.path.rstrip("/") != f"/{token}":
            self.send_error(404)
            return
        submit_url = f"http://127.0.0.1:{server.server_port}/{token}/submit"
        html = _fio_login_prefab_app(submit_url).html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        server = self.server
        token = getattr(server, "login_token", "")
        if self.path != f"/{token}/submit":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            if "application/json" in self.headers.get("Content-Type", ""):
                payload = json.loads(raw or "{}")
            else:
                parsed = parse_qs(raw)
                payload = {key: values[-1] for key, values in parsed.items()}
            message = _add_token_on_submit(FioTokenSetup.model_validate(payload))
            ok = message.startswith(("Added Fio token", "Already configured Fio token"))
            self._send_json({"ok": ok, "message": message})
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=400)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _start_fio_web_login() -> str:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FioLoginHandler)
    server.login_token = token  # type: ignore[attr-defined]
    _WEB_LOGIN_SERVERS[token] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/{token}"


@mcp.tool
async def fio_alias_account(
    ctx: Context,
    account: Annotated[
        str,
        Field(
            description=(
                "Account identifier — the current alias or the bank_account string "
                "(e.g. '2603445200/2010'). Use fio_list_accounts to see available accounts."
            ),
        ),
    ],
    alias: Annotated[
        str,
        Field(
            description=(
                "New friendly alias to assign (e.g. 'main', 'donation_account'). After this "
                "tool succeeds, other fio_* tools accept `account=<alias>` in place of the "
                "full bank_account string."
            ),
        ),
    ],
) -> AliasAccountResult:
    """Assign or update the friendly alias for a configured Fio account.

    Aliases simplify account selection across the other fio_* tools. Renaming
    is idempotent and does not affect the underlying token or registry entry.
    """
    try:
        result = _client_from_context(ctx).alias_account(account, alias)
        return AliasAccountResult(**result.__dict__)
    except Exception as exc:
        raise _tool_error(exc) from exc


@mcp.tool
async def fio_remove_token(
    ctx: Context,
    account: Annotated[
        str,
        Field(description="Account identifier (alias or bank_account) that holds the token."),
    ],
    token_key: Annotated[
        str,
        Field(
            description=(
                "Safe token_key from fio_list_accounts (an opaque reference, NOT the raw "
                "Fio API token). Each account can hold multiple tokens for rotation."
            ),
        ),
    ],
) -> RemoveTokenResult:
    """Revoke a Fio API token by its safe token_key.

    Destructive: the token_key cannot be reused after removal — to add another
    token to the same account, call fio_login again. Removing the last token
    blocks further queries on that account until a new one is added.
    """
    try:
        result = _client_from_context(ctx).remove_token(account, token_key)
        return RemoveTokenResult(**result.__dict__)
    except Exception as exc:
        raise _tool_error(exc) from exc


@mcp.tool
async def fio_list_accounts(
    ctx: Context,
    include_tokens: Annotated[
        bool,
        Field(
            description=(
                "Include the per-account token list with safe token_keys and rate-limit "
                "availability. Default true."
            ),
        ),
    ] = True,
    include_status: Annotated[
        bool,
        Field(
            description=(
                "Compute and include each token's rate-limit clock (available / "
                "next_available_at). Cheap; default true."
            ),
        ),
    ] = True,
) -> ListAccountsResult:
    """List configured Fio accounts with bank_account, alias, currency, token count,
    and per-token rate-limit availability.

    Safe — never calls Fio's API and never returns raw token strings. Use to
    discover which accounts exist before fio_find_transactions, or to check
    whether a token is currently rate-limited.
    """
    client = _client_from_context(ctx)
    return _list_accounts(client, include_tokens=include_tokens, include_status=include_status)


@mcp.tool
async def fio_test_connection(
    ctx: Context,
    account: Annotated[
        str | None,
        Field(
            description=(
                "Account identifier (alias or bank_account). Omit if exactly one account "
                "is configured."
            ),
        ),
    ] = None,
    cache: Annotated[
        CacheMode,
        Field(
            description=(
                "'use' (default), 'refresh', or 'only'. See fio_find_transactions for "
                "semantics; fio_get_metadata exposes cache TTLs."
            ),
        ),
    ] = "use",
    max_wait_seconds: Annotated[
        float | None,
        Field(
            ge=0,
            description=(
                "Maximum seconds to wait for a Fio token lease before returning "
                "rate_limit_wait_required."
            ),
        ),
    ] = None,
) -> TestConnectionResult:
    """Verify Fio API connectivity for a configured account without advancing the
    bank-side download marker.

    Returns an account header plus a minimal sample. Use to validate setup,
    diagnose auth failures, or check rate-limit warmup before bulk queries.
    """
    return await _test_connection(
        _client_from_context(ctx),
        account=account,
        cache=cache,
        max_wait_seconds=max_wait_seconds,
    )


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
    """Search Fio transactions for a date range without advancing the bank-side
    last-download marker. Use fio_get_new_transactions for marker-advancing
    ingestion.

    Filters: symbols, counterparty, message text, amount, currency, direction.

    CRITICAL: detail_level controls which Transaction fields are populated. For
    accounting / payee identification ALWAYS use 'full' (the default) — 'summary'
    and 'counterparty' hide message, user_identification, comment, specification,
    and the derived `payee_hint`. Without those, card merchants and message-only
    transfer payees cannot be identified. Local filters like `message_search` /
    `counterparty_search` rely on the same hidden fields and silently match
    nothing at lower levels.

    Example: date_from='2026-05-01', date_to='2026-05-31',
    message_search='SM PRODUCTION', detail_level='full' returns May payments
    that mention SM Production with the vendor name visible in `message`.
    """
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
    """⚠ SIDE EFFECT — advances Fio's bank-side last-download marker.

    Use this only for one-shot ingestion of new transactions (e.g. a nightly
    sync). For repeatable queries over a known date range, use
    fio_find_transactions instead. Requires `confirm_advances_download_marker=true`
    in the request — otherwise the tool returns `confirmation_required`.

    detail_level semantics match fio_find_transactions; the same caveat about
    hidden fields below detail_level='full' applies.
    """
    return await _get_new_transactions(_client_from_context(ctx), request)


@mcp.tool
async def fio_get_metadata(ctx: Context) -> MetadataResult:
    """Return the Rosetta Stone for interpreting Fio responses.

    Covers Fio column mappings, the `detail_level` field-visibility matrix
    (`detail_level_visibility`), documented Fio `message` prefixes (`message_patterns`
    — QR Objednavka, PLATBA DARUJMECZ, Nákup card purchases, Z-reference transfers),
    cache modes, error codes, operational limits, side effects, and rate-limit info.

    Call once at session start and cache the result. The payload is small.
    """
    return _metadata_result(_client_from_context(ctx))


def _metadata_result(client: FioClient) -> MetadataResult:
    return MetadataResult(
        source_documents=["https://www.fio.cz/docs/cz/API_Bankovnictvi.pdf"],
        comparison_fields={
            "money_format": "fixed_two_decimal_string",
            "bank_account_format": "account/bank_code",
            "date_filters": ["date_from", "date_to"],
            "payment_fields": ["variable_symbol", "currency", "amount"],
            "bank_account_fields": [
                "account.bank_account",
                "transactions.counterparty_bank_account",
            ],
            "local_filters": [
                "variable_symbol",
                "constant_symbol",
                "specific_symbol",
                "currency",
                "direction",
                "min_amount",
                "max_amount",
                "counterparty_bank_account",
                "counterparty_name",
                "counterparty_search",
                "message_search",
            ],
            "detail_level_for_pairing": "summary",
            "safe_endpoint": "periods",
            "marker_advancing_endpoint": "last",
        },
        setup_tools=["fio_login", "fio_alias_account", "fio_remove_token"],
        account_selection={
            "omitted_account_allowed_when": "exactly one account is configured",
            "accepted_account_values": ["alias", "bank_account"],
        },
        columns=COLUMN_METADATA,
        error_codes=ERROR_CODE_METADATA,
        cache_modes=CACHE_MODE_METADATA,
        detail_levels=DETAIL_LEVEL_METADATA,
        detail_level_visibility=_detail_level_visibility(),
        directions=DIRECTION_METADATA,
        message_patterns=MESSAGE_PATTERNS,
        limits=MetadataLimits(
            max_page_limit=MAX_PAGE_LIMIT,
            rate_limit_seconds=client.rate_limit_seconds(),
        ),
        side_effects=SIDE_EFFECT_METADATA,
        rate_limit_seconds=client.rate_limit_seconds(),
    )


def _client_from_context(ctx: Context) -> FioClient:
    return ctx.lifespan_context["fio_client"]


def _tool_error(exc: Exception) -> ToolError:
    info = _error_info(exc)
    return ToolError(f"{info.code}: {info.message}")


def _token_available(client: FioClient, token_key: str) -> bool:
    next_available = client.token_rate_limit_status(token_key).next_available_at
    return next_available is None or next_available <= datetime.now(UTC)


def _list_accounts(
    client: FioClient,
    *,
    include_tokens: bool,
    include_status: bool,
) -> ListAccountsResult:
    accounts = [
        AccountSummary(
            account=account.handle,
            alias=account.alias,
            bank_account=account.bank_account,
            currency=account.currency,
            iban=account.iban,
            token_count=len(account.tokens),
            tokens=[
                TokenSummary(
                    token_key=token.token_key,
                    available=_token_available(client, token.token_key) if include_status else None,
                    next_available_at=client.token_rate_limit_status(
                        token.token_key
                    ).next_available_at
                    if include_status
                    else None,
                )
                for token in account.tokens
            ]
            if include_tokens
            else [],
        )
        for account in client.accounts()
    ]
    rate_limit: dict[str, TokenPoolStatus] = {}
    if include_status:
        for account in client.accounts():
            rate_limit[account.handle] = client.rate_limit_status(account.account_key)
    return ListAccountsResult(accounts=accounts, rate_limit=rate_limit)


async def _test_connection(
    client: FioClient,
    *,
    account: str | None,
    cache: CacheMode,
    max_wait_seconds: float | None = None,
) -> TestConnectionResult:
    try:
        result = await client.test_connection(
            account,
            cache_mode=cache,
            max_wait_seconds=max_wait_seconds,
        )
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
    try:
        resolved_account = client.account(query.account)
    except Exception as exc:
        return FindTransactionsResult(
            cache=CacheInfo(mode=query.cache, hit=False),
            error=_error_info(exc),
        )

    filter_hash = query.filter_hash(resolved_account.account_key)
    if query.cursor:
        return _page_from_cursor(
            client,
            cursor=query.cursor,
            expected_kind="period",
            expected_filter_hash=filter_hash,
            limit=query.limit,
            detail_level=query.effective_detail_level(),
            cache_mode=query.cache,
            query=query,
        )

    try:
        include_raw = query.effective_include_raw()
        result = await client.period(
            resolved_account.account_key,
            query.date_from,
            query.date_to,
            cache_mode=query.cache,
            include_raw=include_raw,
            max_wait_seconds=query.max_wait_seconds,
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
        cache_key=result.cache.key
        or period_cache_key(
            resolved_account.account_key,
            query.date_from,
            query.date_to,
            include_raw=query.effective_include_raw(),
        ),
        kind="period",
        filter_hash=filter_hash,
        offset=0,
        limit=query.limit,
        detail_level=query.effective_detail_level(),
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

    try:
        resolved_account = client.account(request.account)
    except Exception as exc:
        return FindTransactionsResult(
            cache=CacheInfo(mode=request.cache, hit=False),
            error=_error_info(exc),
        )

    filter_hash = _filter_hash(
        {
            "account": resolved_account.account_key,
            "detail_level": request.effective_detail_level(),
            "include_raw": request.effective_include_raw(),
        }
    )
    if request.cursor:
        return _page_from_cursor(
            client,
            cursor=request.cursor,
            expected_kind="last",
            expected_filter_hash=filter_hash,
            limit=request.limit,
            detail_level=request.effective_detail_level(),
            cache_mode=request.cache,
        )

    try:
        include_raw = request.effective_include_raw()
        result = await client.last(
            resolved_account.account_key,
            cache_mode=request.cache,
            include_raw=include_raw,
            max_wait_seconds=request.max_wait_seconds,
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
        cache_key=result.cache.key
        or last_cache_key(
            resolved_account.account_key,
            include_raw=request.effective_include_raw(),
        ),
        kind="last",
        filter_hash=filter_hash,
        offset=0,
        limit=request.limit,
        detail_level=request.effective_detail_level(),
    )


def _page_from_cursor(
    client: FioClient,
    *,
    cursor: str,
    expected_kind: Literal["period", "last"],
    expected_filter_hash: str,
    limit: int,
    detail_level: DetailLevel,
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
        detail_level=detail_level,
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
    if query.counterparty_bank_account:
        result = [
            transaction
            for transaction in result
            if transaction.counterparty_bank_account == query.counterparty_bank_account
        ]
    if query.counterparty_name:
        result = [
            transaction
            for transaction in result
            if transaction.counterparty_name == query.counterparty_name
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
                    transaction.counterparty_bank_account or "",
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
    detail_level: DetailLevel,
) -> FindTransactionsResult:
    page = transactions[offset : offset + limit]
    page = [_shape_transaction(transaction, detail_level) for transaction in page]
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


SUMMARY_FIELDS = {
    "transaction_id",
    "posted_date",
    "amount",
    "currency",
    "direction",
    "counterparty_bank_account",
    "constant_symbol",
    "variable_symbol",
    "specific_symbol",
    "transaction_type",
    "order_id",
}
COUNTERPARTY_FIELDS = SUMMARY_FIELDS | {
    "counterparty_name",
}
FULL_FIELDS = COUNTERPARTY_FIELDS | {
    "user_identification",
    "message",
    "specification",
    "comment",
    "payee_hint",
}


def _detail_level_visibility() -> dict[str, list[str]]:
    """Field-visibility matrix exposed via fio_get_metadata so callers can pick
    the right detail_level without sampling responses."""
    return {
        "summary": sorted(SUMMARY_FIELDS),
        "counterparty": sorted(COUNTERPARTY_FIELDS),
        "full": sorted(FULL_FIELDS),
        "raw": sorted(FULL_FIELDS | {"raw"}),
    }


def _shape_transaction(transaction: Transaction, detail_level: DetailLevel) -> Transaction:
    data = transaction.model_dump()
    if detail_level == "raw":
        return Transaction.model_validate(data)
    if detail_level == "full":
        data["raw"] = None
        return Transaction.model_validate(data)

    allowed = COUNTERPARTY_FIELDS if detail_level == "counterparty" else SUMMARY_FIELDS
    for key in data:
        if key not in allowed:
            data[key] = None
    return Transaction.model_validate(data)


def _control_totals(transactions: list[Transaction]) -> ControlTotals:
    by_currency: dict[str, dict[str, str | int]] = {}
    for transaction in transactions:
        currency = transaction.currency or "unknown"
        bucket = by_currency.setdefault(
            currency, {"count": 0, "incoming": "0.00", "outgoing": "0.00"}
        )
        bucket["count"] = int(bucket["count"]) + 1
        key = "incoming" if transaction.direction == "incoming" else "outgoing"
        bucket[key] = _format_money(Decimal(str(bucket[key])) + abs(transaction.amount))
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


def _format_money(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01")))


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
        return ErrorInfo(
            code=exc.code,
            message=str(exc),
            retry_after_seconds=exc.retry_after_seconds,
            next_available_at=exc.next_available_at,
        )
    if isinstance(exc, httpx.RequestError):
        return ErrorInfo(code="network_error", message="Fio API network request failed")
    return ErrorInfo(code="error", message=str(exc))


def main() -> None:
    mcp.run()


mcp.tool(fio_login, app=True)


if __name__ == "__main__":
    main()
