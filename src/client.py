from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from cache import ResponseCache
from models import AccountStatement, CacheInfo, CacheMode, RateLimitInfo, TokenPoolStatus
from normalization import normalize_statement
from rate_limit import RateLimitWaitRequired, TokenRateLimiter
from settings import FioAccount, FioAccountToken, Settings, store_accounts


class FioApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        payload: Any = None,
        retry_after_seconds: float | None = None,
        next_available_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload
        self.retry_after_seconds = retry_after_seconds
        self.next_available_at = next_available_at


class FioCacheMiss(RuntimeError):
    def __init__(self, cache_info: CacheInfo) -> None:
        super().__init__("No valid cached Fio response is available")
        self.cache_info = cache_info


@dataclass(frozen=True)
class FioReadResult:
    statement: AccountStatement
    cache: CacheInfo
    rate_limit: RateLimitInfo


@dataclass(frozen=True)
class AddTokenResult:
    ok: bool
    account: str
    account_key: str
    bank_account: str | None
    alias: str | None
    token_key: str
    token_count: int
    added: bool


@dataclass(frozen=True)
class AliasAccountResult:
    ok: bool
    account: str
    account_key: str
    bank_account: str | None
    alias: str


@dataclass(frozen=True)
class RemoveTokenResult:
    ok: bool
    account: str
    account_key: str
    bank_account: str | None
    token_count: int
    removed: bool


class FioClient:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: ResponseCache | None = None,
        limiter: TokenRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache or ResponseCache()
        self._limiter = limiter or TokenRateLimiter(
            cooldown_seconds=settings.fio_rate_limit_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=str(settings.fio_base_url).rstrip("/") + "/",
            timeout=httpx.Timeout(settings.fio_timeout_seconds),
            follow_redirects=False,
        )
        self._accounts_by_key: dict[str, FioAccount] = {}
        self._aliases: dict[str, str] = {}
        self.replace_accounts(settings.accounts(), persist=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    def is_configured(self) -> bool:
        return bool(self._accounts_by_key)

    def replace_accounts(self, accounts: list[FioAccount], *, persist: bool = True) -> None:
        self._accounts_by_key = {account.account_key: account for account in accounts}
        self._aliases = {
            account.alias: account.account_key for account in accounts if account.alias is not None
        }
        if persist:
            self._persist()

    def accounts(self) -> list[FioAccount]:
        return list(self._accounts_by_key.values())

    def account(self, handle: str | None = None) -> FioAccount:
        if handle is None or not handle.strip():
            accounts = self.accounts()
            if not accounts:
                raise FioApiError(
                    "No Fio accounts configured. Call fio_login first.",
                    code="not_configured",
                )
            if len(accounts) > 1:
                choices = ", ".join(account.handle for account in accounts)
                raise FioApiError(
                    f"Multiple Fio accounts are configured; pass account. Available: {choices}",
                    code="ambiguous_account",
                )
            return accounts[0]

        handle = handle.strip()
        account_key = self._aliases.get(handle, handle)
        if account_key in self._accounts_by_key:
            return self._accounts_by_key[account_key]
        matches = [account for account in self.accounts() if account.bank_account == handle]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FioApiError(
                f"Multiple Fio accounts use bank account {handle}; pass alias.",
                code="ambiguous_account",
            )
        raise FioApiError(
            f"Unknown Fio account: {handle}",
            code="unknown_account",
        )

    def cache(self) -> ResponseCache:
        return self._cache

    def rate_limit_seconds(self) -> float:
        return self._settings.fio_rate_limit_seconds

    def rate_limit_status(self, handle: str | None = None) -> TokenPoolStatus:
        account = self.account(handle)
        return self._limiter.status_many(_token_keys(account))

    def token_rate_limit_status(self, token_key: str) -> RateLimitInfo:
        return self._limiter.status(token_key)

    async def add_token(self, token: str) -> AddTokenResult:
        token = token.strip()
        if not token:
            raise FioApiError("Fio token cannot be blank", code="invalid_request")
        token_key = token_key_from_secret(token)
        existing = self._find_account_by_token_key(token_key)
        if existing is not None:
            return AddTokenResult(
                ok=True,
                account=existing.handle,
                account_key=existing.account_key,
                bank_account=existing.bank_account,
                alias=existing.alias,
                token_key=token_key,
                token_count=len(existing.tokens),
                added=False,
            )

        account_info = await self._validate_token(token, token_key)
        return self._store_validated_token(token, token_key, account_info)

    def add_token_sync(self, token: str) -> AddTokenResult:
        token = token.strip()
        if not token:
            raise FioApiError("Fio token cannot be blank", code="invalid_request")
        token_key = token_key_from_secret(token)
        existing = self._find_account_by_token_key(token_key)
        if existing is not None:
            return AddTokenResult(
                ok=True,
                account=existing.handle,
                account_key=existing.account_key,
                bank_account=existing.bank_account,
                alias=existing.alias,
                token_key=token_key,
                token_count=len(existing.tokens),
                added=False,
            )

        account_info = self._validate_token_sync(token, token_key)
        return self._store_validated_token(token, token_key, account_info)

    def _store_validated_token(
        self,
        token: str,
        token_key: str,
        account_info: dict[str, str | None],
    ) -> AddTokenResult:
        account_key = _account_key_from_info(account_info)
        existing_account = self._accounts_by_key.get(account_key)
        new_token = FioAccountToken(
            token_key=token_key,
            token=SecretStr(token),
            added_at=datetime.now(UTC),
        )

        if existing_account is None:
            updated = FioAccount(
                account_key=account_key,
                alias=None,
                account_id=account_info["account_id"],
                bank_id=account_info.get("bank_id"),
                currency=account_info.get("currency"),
                iban=account_info.get("iban"),
                bic=account_info.get("bic"),
                tokens=[new_token],
                marker_token_key=token_key,
            )
        else:
            updated = existing_account.model_copy(
                update={
                    "tokens": [*existing_account.tokens, new_token],
                    "account_id": account_info["account_id"],
                    "bank_id": account_info.get("bank_id") or existing_account.bank_id,
                    "currency": account_info.get("currency") or existing_account.currency,
                    "iban": account_info.get("iban") or existing_account.iban,
                    "bic": account_info.get("bic") or existing_account.bic,
                }
            )

        self._accounts_by_key[account_key] = updated
        self._rebuild_aliases()
        self._persist()
        return AddTokenResult(
            ok=True,
            account=updated.handle,
            account_key=updated.account_key,
            bank_account=updated.bank_account,
            alias=updated.alias,
            token_key=token_key,
            token_count=len(updated.tokens),
            added=True,
        )

    def alias_account(self, account: str, alias: str) -> AliasAccountResult:
        target = self.account(account)
        alias = alias.strip()
        if not alias:
            raise FioApiError("Fio account alias cannot be blank", code="invalid_request")
        if alias in self._accounts_by_key and alias != target.account_key:
            raise FioApiError(
                "Fio account alias collides with an account key", code="invalid_request"
            )
        existing = self._aliases.get(alias)
        if existing is not None and existing != target.account_key:
            raise FioApiError("Fio account alias is already in use", code="invalid_request")

        updated = FioAccount(
            account_key=target.account_key,
            alias=alias,
            account_id=target.account_id,
            bank_id=target.bank_id,
            currency=target.currency,
            iban=target.iban,
            bic=target.bic,
            tokens=target.tokens,
            marker_token_key=target.marker_token_key,
        )
        self._accounts_by_key[target.account_key] = updated
        self._rebuild_aliases()
        self._persist()
        return AliasAccountResult(
            ok=True,
            account=updated.handle,
            account_key=updated.account_key,
            bank_account=updated.bank_account,
            alias=alias,
        )

    def remove_token(self, account: str, token_key: str) -> RemoveTokenResult:
        target = self.account(account)
        token_key = token_key.strip()
        remaining = [token for token in target.tokens if token.token_key != token_key]
        if len(remaining) == len(target.tokens):
            raise FioApiError("Unknown Fio token key for account", code="unknown_token")
        if not remaining:
            raise FioApiError(
                "Cannot remove the last token from an account",
                code="last_token",
            )
        marker_token_key = target.marker_token_key
        if marker_token_key == token_key:
            marker_token_key = remaining[0].token_key
        updated = target.model_copy(
            update={"tokens": remaining, "marker_token_key": marker_token_key}
        )
        self._accounts_by_key[target.account_key] = updated
        self._persist()
        return RemoveTokenResult(
            ok=True,
            account=updated.handle,
            account_key=updated.account_key,
            bank_account=updated.bank_account,
            token_count=len(updated.tokens),
            removed=True,
        )

    async def test_connection(
        self,
        handle: str | None = None,
        *,
        cache_mode: CacheMode,
        max_wait_seconds: float | None = None,
    ) -> FioReadResult:
        today = date.today()
        return await self.period(
            handle,
            today,
            today,
            cache_mode=cache_mode,
            include_raw=False,
            max_wait_seconds=max_wait_seconds,
        )

    async def period(
        self,
        handle: str | None,
        date_from: date,
        date_to: date,
        *,
        cache_mode: CacheMode,
        include_raw: bool,
        max_wait_seconds: float | None = None,
    ) -> FioReadResult:
        account = self.account(handle)
        key = period_cache_key(account.account_key, date_from, date_to, include_raw=include_raw)
        ttl = (
            self._settings.fio_cache_ttl_historical_seconds
            if date_to < date.today()
            else self._settings.fio_cache_ttl_active_seconds
        )
        return await self._read(
            account=account,
            tokens=account.tokens,
            path_template=lambda token: (
                f"periods/{token.token.get_secret_value()}/{date_from}/{date_to}/transactions.json"
            ),
            cache_key=key,
            cache_mode=cache_mode,
            ttl_seconds=ttl,
            include_raw=include_raw,
            retry_with_pool=True,
            max_wait_seconds=max_wait_seconds,
        )

    async def last(
        self,
        handle: str | None,
        *,
        cache_mode: CacheMode,
        include_raw: bool,
        max_wait_seconds: float | None = None,
    ) -> FioReadResult:
        account = self.account(handle)
        token = self._marker_token(account)
        key = last_cache_key(account.account_key, include_raw=include_raw)
        return await self._read(
            account=account,
            tokens=[token],
            path_template=lambda selected: (
                f"last/{selected.token.get_secret_value()}/transactions.json"
            ),
            cache_key=key,
            cache_mode=cache_mode,
            ttl_seconds=self._settings.fio_cache_ttl_last_seconds,
            include_raw=include_raw,
            retry_with_pool=False,
            max_wait_seconds=max_wait_seconds,
        )

    async def _read(
        self,
        *,
        account: FioAccount,
        tokens: list[FioAccountToken],
        path_template: Any,
        cache_key: str,
        cache_mode: CacheMode,
        ttl_seconds: int,
        include_raw: bool,
        retry_with_pool: bool,
        max_wait_seconds: float | None,
    ) -> FioReadResult:
        if cache_mode != "refresh":
            cached, cache_info = self._cache.get(cache_key, mode=cache_mode)
            if cached is None and not include_raw:
                raw_cache_key = _raw_cache_key(cache_key)
                if raw_cache_key != cache_key:
                    cached, cache_info = self._cache.get(raw_cache_key, mode=cache_mode)
            if cached is not None:
                status = self._limiter.status_many([token.token_key for token in tokens])
                return FioReadResult(
                    statement=cached,
                    cache=cache_info,
                    rate_limit=RateLimitInfo(
                        consumed_lease=False,
                        waited_seconds=0,
                        next_available_at=status.next_available_at,
                    ),
                )
            if cache_mode == "only":
                raise FioCacheMiss(cache_info)

        token_by_key = {token.token_key: token for token in tokens}
        attempts = max(1, len(tokens)) if retry_with_pool else 1
        rate_info: RateLimitInfo | None = None
        last_error: FioApiError | None = None
        used_token_keys: set[str] = set()

        for _ in range(attempts):
            available = [
                token_key for token_key in token_by_key if token_key not in used_token_keys
            ] or list(token_by_key)
            try:
                lease = await self._limiter.acquire_any(
                    available,
                    max_wait_seconds=max_wait_seconds,
                )
            except RateLimitWaitRequired as exc:
                raise FioApiError(
                    (
                        "Fio rate limit wait exceeds max_wait_seconds; retry later "
                        "or use cache=only/use with an existing cached response."
                    ),
                    code="rate_limit_wait_required",
                    retry_after_seconds=exc.wait_seconds,
                    next_available_at=exc.next_available_at,
                ) from exc
            selected = token_by_key[lease.token_key]
            used_token_keys.add(lease.token_key)
            rate_info = _merge_rate_info(rate_info, lease.rate_limit)
            try:
                statement = await self._request_statement(
                    account,
                    path_template(selected),
                    include_raw=include_raw,
                )
                cache_info = self._cache.set(
                    cache_key,
                    statement,
                    ttl_seconds=ttl_seconds,
                    mode=cache_mode,
                )
                return FioReadResult(
                    statement=statement,
                    cache=cache_info,
                    rate_limit=rate_info,
                )
            except FioApiError as exc:
                if exc.status_code != 409:
                    raise
                last_error = exc
                self._limiter.mark_conflict(lease.token_key)

        assert last_error is not None
        raise last_error

    async def _request_statement(
        self,
        account: FioAccount,
        path: str,
        *,
        include_raw: bool,
    ) -> AccountStatement:
        response = await self._client.get(path)
        payload = _response_payload(response)

        if response.status_code >= 400:
            raise _fio_error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise FioApiError("Fio returned a non-object JSON payload", code="invalid_response")
        return normalize_statement(payload, account=account, include_raw=include_raw)

    async def _validate_token(self, token: str, token_key: str) -> dict[str, str | None]:
        today = date.today()
        await self._limiter.acquire(token_key)
        response = await self._client.get(
            f"periods/{token}/{today}/{today}/transactions.json",
        )
        payload = _response_payload(response)
        if response.status_code >= 400:
            raise _fio_error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise FioApiError("Fio returned a non-object JSON payload", code="invalid_response")
        info = _statement_info(payload)
        account_id = _string_or_none(info.get("accountId"))
        if account_id is None:
            raise FioApiError("Fio response did not include accountId", code="invalid_response")
        return {
            "account_id": account_id,
            "bank_id": _string_or_none(info.get("bankId")),
            "currency": _string_or_none(info.get("currency")),
            "iban": _string_or_none(info.get("iban")),
            "bic": _string_or_none(info.get("bic")),
        }

    def _validate_token_sync(self, token: str, token_key: str) -> dict[str, str | None]:
        today = date.today()
        with httpx.Client(
            base_url=str(self._settings.fio_base_url).rstrip("/") + "/",
            timeout=httpx.Timeout(self._settings.fio_timeout_seconds),
            follow_redirects=False,
        ) as client:
            response = client.get(f"periods/{token}/{today}/{today}/transactions.json")
        payload = _response_payload(response)
        if response.status_code >= 400:
            raise _fio_error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise FioApiError("Fio returned a non-object JSON payload", code="invalid_response")
        info = _statement_info(payload)
        account_id = _string_or_none(info.get("accountId"))
        if account_id is None:
            raise FioApiError("Fio response did not include accountId", code="invalid_response")
        return {
            "account_id": account_id,
            "bank_id": _string_or_none(info.get("bankId")),
            "currency": _string_or_none(info.get("currency")),
            "iban": _string_or_none(info.get("iban")),
            "bic": _string_or_none(info.get("bic")),
        }

    def _find_account_by_token_key(self, token_key: str) -> FioAccount | None:
        for account in self.accounts():
            if any(token.token_key == token_key for token in account.tokens):
                return account
        return None

    def _marker_token(self, account: FioAccount) -> FioAccountToken:
        for token in account.tokens:
            if token.token_key == account.marker_token_key:
                return token
        raise FioApiError("Account marker token is not configured", code="invalid_request")

    def _rebuild_aliases(self) -> None:
        self._aliases = {
            account.alias: account.account_key
            for account in self._accounts_by_key.values()
            if account.alias is not None
        }

    def _persist(self) -> None:
        store_accounts(self.accounts())


def period_cache_key(account: str, date_from: date, date_to: date, *, include_raw: bool) -> str:
    return f"periods:{account}:{date_from}:{date_to}:raw={int(include_raw)}"


def last_cache_key(account: str, *, include_raw: bool) -> str:
    return f"last:{account}:raw={int(include_raw)}"


def _raw_cache_key(cache_key: str) -> str:
    return cache_key[:-1] + "1" if cache_key.endswith(":raw=0") else cache_key


def token_key_from_secret(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:16]


def _token_keys(account: FioAccount) -> list[str]:
    return [token.token_key for token in account.tokens]


def _merge_rate_info(current: RateLimitInfo | None, new: RateLimitInfo) -> RateLimitInfo:
    if current is None:
        return new
    return RateLimitInfo(
        consumed_lease=current.consumed_lease or new.consumed_lease,
        waited_seconds=current.waited_seconds + new.waited_seconds,
        next_available_at=new.next_available_at or current.next_available_at,
    )


def _account_key_from_info(info: dict[str, str | None]) -> str:
    account_id = info["account_id"]
    bank_id = info.get("bank_id") or "unknown-bank"
    currency = info.get("currency") or "unknown-currency"
    return f"{account_id}-{bank_id}-{currency}"


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _statement_info(payload: dict[str, Any]) -> dict[str, Any]:
    statement = payload.get("accountStatement")
    if not isinstance(statement, dict):
        raise FioApiError("Fio response does not contain accountStatement", code="invalid_response")
    info = statement.get("info")
    if not isinstance(info, dict):
        raise FioApiError(
            "Fio response does not contain accountStatement.info",
            code="invalid_response",
        )
    return info


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    string = str(value).strip()
    return string or None


def _fio_error(status_code: int, payload: Any) -> FioApiError:
    messages: dict[int, tuple[str, str]] = {
        404: ("invalid_token_or_url", "Invalid Fio URL or API token"),
        409: ("rate_limited", "Fio rate limit exceeded"),
        413: ("too_many_transactions", "Fio returned too many transactions for this request"),
        422: ("invalid_request", "Fio rejected the request data"),
        500: ("fio_internal_error", "Fio returned an internal server error"),
    }
    code, message = messages.get(
        status_code, ("fio_error", f"Fio API request failed: {status_code}")
    )
    return FioApiError(message, code=code, status_code=status_code, payload=payload)
