from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

import settings as settings_module
from client import FioApiError, FioCacheMiss, FioClient
from settings import Settings
from tests.fixtures import sample_account, sample_fio_response


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    account = sample_account()
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[account]),
    )
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    return Settings(_env_file=None)


@respx.mock
async def test_fetches_period_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-for-test/2026-05-01/2026-05-16/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response()))
    client = FioClient(settings(monkeypatch))

    result = await client.period(
        "main",
        date(2026, 5, 1),
        date(2026, 5, 16),
        cache_mode="use",
        include_raw=False,
    )
    await client.aclose()

    assert route.called
    assert result.statement.transactions[0].transaction_id == "27573053171"
    assert result.cache.hit is False
    assert result.rate_limit.consumed_lease is True


@respx.mock
async def test_repeated_period_call_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-for-test/2026-05-01/2026-05-16/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response()))
    client = FioClient(settings(monkeypatch))

    first = await client.period(
        "main",
        date(2026, 5, 1),
        date(2026, 5, 16),
        cache_mode="use",
        include_raw=False,
    )
    second = await client.period(
        "main",
        date(2026, 5, 1),
        date(2026, 5, 16),
        cache_mode="use",
        include_raw=False,
    )
    await client.aclose()

    assert route.call_count == 1
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.rate_limit.consumed_lease is False


async def test_cache_only_miss_never_calls_fio(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    with pytest.raises(FioCacheMiss):
        await client.period(
            "main",
            date(2026, 5, 1),
            date(2026, 5, 16),
            cache_mode="only",
            include_raw=False,
        )
    await client.aclose()


@respx.mock
async def test_maps_fio_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-for-test/2026-05-01/2026-05-16/transactions.json"
    ).mock(return_value=Response(413, text="too much"))
    client = FioClient(settings(monkeypatch))

    with pytest.raises(FioApiError) as error:
        await client.period(
            "main",
            date(2026, 5, 1),
            date(2026, 5, 16),
            cache_mode="refresh",
            include_raw=False,
        )
    await client.aclose()

    assert error.value.code == "too_many_transactions"
    assert "token-for-test" not in str(error.value)


@respx.mock
async def test_add_token_validates_persists_and_pairs_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setattr(settings_module, "load_stored_accounts", lambda: None)
    stored = []
    monkeypatch.setattr("client.store_accounts", lambda accounts: stored.append(accounts))
    today = date.today()
    respx.get(
        f"https://fioapi.fio.cz/v1/rest/periods/new-token/{today}/{today}/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response([])))
    client = FioClient(Settings(_env_file=None))

    result = await client.add_token("new-token")
    await client.aclose()

    assert result.added is True
    assert result.account_key == "2603445200-2010-CZK"
    assert result.token_count == 1
    assert stored

    client = FioClient(Settings(_env_file=None))
    client.replace_accounts(stored[-1], persist=False)
    duplicate = await client.add_token("new-token")
    await client.aclose()

    assert duplicate.added is False
    assert duplicate.token_count == 1


@respx.mock
async def test_period_load_balances_across_account_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    account = settings_module.FioAccount(
        account_key="2603445200-2010-CZK",
        account_id="2603445200",
        bank_id="2010",
        currency="CZK",
        tokens=[
            settings_module.FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a"),
            settings_module.FioAccountToken(token_key="1111222233334444", token="token-b"),
        ],
        marker_token_key="aaaabbbbccccdddd",
    )
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[account]),
    )
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "31")
    first = respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-a/2026-05-01/2026-05-16/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response([])))
    second = respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-b/2026-05-02/2026-05-16/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response([])))
    client = FioClient(Settings(_env_file=None))

    await client.period(
        None,
        date(2026, 5, 1),
        date(2026, 5, 16),
        cache_mode="refresh",
        include_raw=False,
    )
    await client.period(
        None,
        date(2026, 5, 2),
        date(2026, 5, 16),
        cache_mode="refresh",
        include_raw=False,
    )
    await client.aclose()

    assert first.called
    assert second.called


@respx.mock
async def test_last_uses_marker_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    account = settings_module.FioAccount(
        account_key="2603445200-2010-CZK",
        account_id="2603445200",
        bank_id="2010",
        currency="CZK",
        tokens=[
            settings_module.FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a"),
            settings_module.FioAccountToken(token_key="1111222233334444", token="token-b"),
        ],
        marker_token_key="1111222233334444",
    )
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[account]),
    )
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    wrong = respx.get("https://fioapi.fio.cz/v1/rest/last/token-a/transactions.json").mock(
        return_value=Response(500, text="wrong token")
    )
    right = respx.get("https://fioapi.fio.cz/v1/rest/last/token-b/transactions.json").mock(
        return_value=Response(200, json=sample_fio_response([]))
    )
    client = FioClient(Settings(_env_file=None))

    await client.last(None, cache_mode="refresh", include_raw=False)
    await client.aclose()

    assert wrong.called is False
    assert right.called is True
