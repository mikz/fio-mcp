from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from client import FioApiError, FioCacheMiss, FioClient
from settings import Settings
from tests.fixtures import sample_fio_response


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("FIO_API_TOKEN", "token-for-test")
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
