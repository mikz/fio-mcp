from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from cache import ResponseCache
from models import AccountStatement, CacheInfo, CacheMode, RateLimitInfo
from normalization import normalize_statement
from rate_limit import TokenRateLimiter
from settings import FioAccount, Settings


class FioApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload


class FioCacheMiss(RuntimeError):
    def __init__(self, cache_info: CacheInfo) -> None:
        super().__init__("No valid cached Fio response is available")
        self.cache_info = cache_info


@dataclass(frozen=True)
class FioReadResult:
    statement: AccountStatement
    cache: CacheInfo
    rate_limit: RateLimitInfo


class FioClient:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: ResponseCache | None = None,
        limiter: TokenRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._accounts = {account.alias: account for account in settings.accounts()}
        self._cache = cache or ResponseCache()
        self._limiter = limiter or TokenRateLimiter(
            cooldown_seconds=settings.fio_rate_limit_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=str(settings.fio_base_url).rstrip("/") + "/",
            timeout=httpx.Timeout(settings.fio_timeout_seconds),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def accounts(self) -> list[FioAccount]:
        return list(self._accounts.values())

    def account(self, alias: str) -> FioAccount:
        try:
            return self._accounts[alias]
        except KeyError as exc:
            raise FioApiError(
                f"Unknown Fio account alias: {alias}",
                code="unknown_account",
            ) from exc

    def cache(self) -> ResponseCache:
        return self._cache

    def max_period_days(self) -> int:
        return self._settings.fio_max_period_days

    def rate_limit_seconds(self) -> float:
        return self._settings.fio_rate_limit_seconds

    def rate_limit_status(self, alias: str) -> RateLimitInfo:
        return self._limiter.status(_token_key(self.account(alias)))

    async def test_connection(self, alias: str, *, cache_mode: CacheMode) -> FioReadResult:
        today = date.today()
        return await self.period(alias, today, today, cache_mode=cache_mode, include_raw=False)

    async def period(
        self,
        alias: str,
        date_from: date,
        date_to: date,
        *,
        cache_mode: CacheMode,
        include_raw: bool,
    ) -> FioReadResult:
        account = self.account(alias)
        key = period_cache_key(alias, date_from, date_to, include_raw=include_raw)
        ttl = (
            self._settings.fio_cache_ttl_historical_seconds
            if date_to < date.today()
            else self._settings.fio_cache_ttl_active_seconds
        )
        return await self._read(
            account=account,
            path=f"periods/{account.token.get_secret_value()}/{date_from}/{date_to}/transactions.json",
            cache_key=key,
            cache_mode=cache_mode,
            ttl_seconds=ttl,
            include_raw=include_raw,
        )

    async def last(
        self,
        alias: str,
        *,
        cache_mode: CacheMode,
        include_raw: bool,
    ) -> FioReadResult:
        account = self.account(alias)
        key = last_cache_key(alias, include_raw=include_raw)
        return await self._read(
            account=account,
            path=f"last/{account.token.get_secret_value()}/transactions.json",
            cache_key=key,
            cache_mode=cache_mode,
            ttl_seconds=self._settings.fio_cache_ttl_last_seconds,
            include_raw=include_raw,
        )

    async def _read(
        self,
        *,
        account: FioAccount,
        path: str,
        cache_key: str,
        cache_mode: CacheMode,
        ttl_seconds: int,
        include_raw: bool,
    ) -> FioReadResult:
        if cache_mode != "refresh":
            cached, cache_info = self._cache.get(cache_key, mode=cache_mode)
            if cached is not None:
                return FioReadResult(
                    statement=cached,
                    cache=cache_info,
                    rate_limit=self._limiter.status(_token_key(account)),
                )
            if cache_mode == "only":
                raise FioCacheMiss(cache_info)

        rate_info = await self._limiter.acquire(_token_key(account))
        try:
            statement = await self._request_statement(account, path, include_raw=include_raw)
        except FioApiError as exc:
            if exc.status_code == 409:
                next_available = self._limiter.mark_conflict(_token_key(account))
                retry_rate = await self._limiter.acquire(_token_key(account))
                try:
                    statement = await self._request_statement(
                        account, path, include_raw=include_raw
                    )
                except FioApiError as retry_exc:
                    raise exc from retry_exc
                rate_info.waited_seconds += retry_rate.waited_seconds
                rate_info.next_available_at = retry_rate.next_available_at or next_available
            else:
                raise

        cache_info = self._cache.set(
            cache_key,
            statement,
            ttl_seconds=ttl_seconds,
            mode=cache_mode,
        )
        return FioReadResult(statement=statement, cache=cache_info, rate_limit=rate_info)

    async def _request_statement(
        self,
        account: FioAccount,
        path: str,
        *,
        include_raw: bool,
    ) -> AccountStatement:
        response = await self._client.get(path)
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if response.status_code >= 400:
            raise _fio_error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise FioApiError("Fio returned a non-object JSON payload", code="invalid_response")
        return normalize_statement(payload, account=account, include_raw=include_raw)


def period_cache_key(alias: str, date_from: date, date_to: date, *, include_raw: bool) -> str:
    return f"periods:{alias}:{date_from}:{date_to}:raw={int(include_raw)}"


def last_cache_key(alias: str, *, include_raw: bool) -> str:
    return f"last:{alias}:raw={int(include_raw)}"


def _token_key(account: FioAccount) -> str:
    digest = hashlib.sha256(account.token.get_secret_value().encode("utf-8")).hexdigest()
    return digest[:16]


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
