from __future__ import annotations

from datetime import date

import pytest
import respx
from fastmcp import Client
from httpx import Response

import client as client_module
import server as server_module
import settings as settings_module
from client import FioClient
from normalization import normalize_statement
from server import (
    FindTransactionsQuery,
    FioTokenSetup,
    NewTransactionsRequest,
    SearchCursor,
    _add_token_on_submit,
    _client_from_settings,
    _encode_cursor,
    _find_transactions,
    _get_new_transactions,
    _list_accounts,
    _metadata_result,
    _shape_transaction,
)
from settings import Settings
from tests.fixtures import sample_account, sample_fio_response, sample_transaction


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    account = sample_account()
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[account]),
    )
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setenv("FIO_MAX_PERIOD_DAYS", "31")
    return Settings(_env_file=None)


def test_query_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        FindTransactionsQuery.model_validate(
            {
                "account": "main",
                "date_from": "2026-05-01",
                "date_to": "2026-05-16",
                "token": "must-not-be-accepted",
            }
        )


async def test_exposed_login_is_unified_login_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "load_settings", lambda: settings(monkeypatch))

    async with Client(server_module.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert "fio_login" in tools
    properties = tools["fio_login"].inputSchema["properties"]
    assert properties["mode"]["enum"] == ["auto", "direct", "prefab", "web"]
    assert set(properties) == {"mode", "credentials"}
    credentials_schema = properties["credentials"]["anyOf"][0]
    assert credentials_schema["required"] == ["token"]
    assert "token" in credentials_schema["properties"]


async def test_add_token_form_submit_validates_stores_alias_and_updates_live_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_accounts = []
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[]),
    )
    monkeypatch.setattr(
        client_module, "store_accounts", lambda accounts: stored_accounts.append(accounts)
    )
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")

    live_client = FioClient(Settings(_env_file=None))

    def validate_token(token: str, token_key: str) -> dict[str, str | None]:
        assert token == "secret-token"
        assert token_key
        return {
            "account_id": "2603445200",
            "bank_id": "2010",
            "currency": "CZK",
            "iban": "CZ6508000000192000145399",
            "bic": "FIOBCZPPXXX",
        }

    monkeypatch.setattr(live_client, "_validate_token_sync", validate_token)
    monkeypatch.setattr(server_module, "_LIVE_CLIENT", live_client)

    try:
        result = _add_token_on_submit(FioTokenSetup(token="secret-token", alias="main"))
    finally:
        monkeypatch.setattr(server_module, "_LIVE_CLIENT", None)
        await live_client.aclose()

    assert "Added Fio token" in result
    assert "secret-token" not in result
    account = live_client.accounts()[0]
    assert account.alias == "main"
    assert account.handle == "main"
    assert stored_accounts


def test_detail_level_compatibility_mapping() -> None:
    assert (
        FindTransactionsQuery(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
        ).effective_detail_level()
        == "full"
    )
    assert (
        FindTransactionsQuery(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            include_counterparty_details=False,
        ).effective_detail_level()
        == "matching"
    )
    assert (
        FindTransactionsQuery(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            include_raw=True,
        ).effective_detail_level()
        == "raw"
    )
    assert (
        FindTransactionsQuery(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            detail_level="counterparty",
        ).effective_detail_level()
        == "counterparty"
    )
    explicit_detail = FindTransactionsQuery(
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 16),
        detail_level="matching",
        include_raw=True,
    )
    assert explicit_detail.effective_detail_level() == "matching"
    assert explicit_detail.effective_include_raw() is False


def test_detail_level_shapes_transaction_privacy() -> None:
    raw = sample_transaction(
        message="QR Objednavka 420260001 Jana Novakova",
        comment="Jana Novakova internal note",
        payer_reference="Jana Novakova reference",
    )
    transaction = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(),
        include_raw=True,
    ).transactions[0]

    matching = _shape_transaction(transaction, "matching")
    assert matching.transaction_id == transaction.transaction_id
    assert matching.amount == transaction.amount
    assert matching.variable_symbol == transaction.variable_symbol
    assert matching.order_id == "39825209552"
    assert matching.counterparty_account is None
    assert matching.counterparty_name is None
    assert matching.user_identification is None
    assert matching.message is None
    assert matching.comment is None
    assert matching.payer_reference is None
    assert matching.raw is None

    counterparty = _shape_transaction(transaction, "counterparty")
    assert counterparty.counterparty_account == "123456789"
    assert counterparty.counterparty_name == "Jana Novakova"
    assert counterparty.payer_reference == "Jana Novakova reference"
    assert counterparty.message is None
    assert counterparty.comment is None
    assert counterparty.raw is None

    full = _shape_transaction(transaction, "full")
    assert full.message == "QR Objednavka 420260001 Jana Novakova"
    assert full.user_identification == "Jana Novakova"
    assert full.comment == "Jana Novakova internal note"
    assert full.raw is None

    shaped_raw = _shape_transaction(transaction, "raw")
    assert shaped_raw.raw == raw


async def test_metadata_exposes_tool_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))
    metadata = _metadata_result(client)

    assert {entry.code for entry in metadata.cache_modes} == {"use", "refresh", "only"}
    assert {entry.code for entry in metadata.detail_levels} == {
        "matching",
        "counterparty",
        "full",
        "raw",
    }
    assert {entry.code for entry in metadata.directions} == {"incoming", "outgoing", "any"}
    assert metadata.limits.max_page_limit == 500
    assert metadata.limits.max_period_days == 31
    assert metadata.limits.rate_limit_seconds == 0
    assert metadata.side_effects[0].tool == "fio_get_new_transactions"
    assert metadata.side_effects[0].guard == "confirm_advances_download_marker=true"
    await client.aclose()


async def test_metadata_contains_emitted_error_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))
    metadata_codes = {entry.code for entry in _metadata_result(client).error_codes}

    emitted_codes = set()

    confirmation = await _get_new_transactions(
        client,
        NewTransactionsRequest(account="main", confirm_advances_download_marker=False),
    )
    assert confirmation.error is not None
    emitted_codes.add(confirmation.error.code)

    too_large = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 2, 15),
        ),
    )
    assert too_large.error is not None
    emitted_codes.add(too_large.error.code)

    invalid_cursor = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            cursor="not-a-cursor",
        ),
    )
    assert invalid_cursor.error is not None
    emitted_codes.add(invalid_cursor.error.code)

    mismatched_cursor = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            cursor=_encode_cursor(
                SearchCursor(
                    kind="period",
                    cache_key="periods:main:2026-05-01:2026-05-16:raw=0",
                    offset=1,
                    limit=1,
                    filter_hash="different-filter",
                )
            ),
        ),
    )
    assert mismatched_cursor.error is not None
    emitted_codes.add(mismatched_cursor.error.code)

    cache_miss = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            cache="only",
        ),
    )
    assert cache_miss.error is not None
    emitted_codes.add(cache_miss.error.code)

    unknown_account = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="missing",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
        ),
    )
    assert unknown_account.error is not None
    emitted_codes.add(unknown_account.error.code)

    await client.aclose()

    assert emitted_codes <= metadata_codes
    assert "error" in metadata_codes


async def test_list_accounts_exposes_safe_token_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    result = _list_accounts(client, include_tokens=True, include_status=True)
    await client.aclose()

    assert result.accounts[0].account == "main"
    assert result.accounts[0].account_key == "2603445200-2010-CZK"
    assert result.accounts[0].token_count == 1
    assert result.accounts[0].tokens[0].token_key == "aaaabbbbccccdddd"
    assert result.accounts[0].tokens[0].role == "marker"
    assert "token-for-test" not in result.model_dump_json()


@respx.mock
async def test_startup_tokens_are_validated_into_runtime_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIO_TOKENS_JSON", '["new-token"]')
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setattr(settings_module, "load_stored_accounts", lambda: None)
    monkeypatch.setattr("client.store_accounts", lambda accounts: None)
    today = date.today()
    respx.get(
        f"https://fioapi.fio.cz/v1/rest/periods/new-token/{today}/{today}/transactions.json"
    ).mock(return_value=Response(200, json=sample_fio_response([])))

    client = await _client_from_settings(Settings(_env_file=None))
    listed = _list_accounts(client, include_tokens=True, include_status=False)
    await client.aclose()

    assert listed.accounts[0].account_key == "2603445200-2010-CZK"
    assert listed.accounts[0].token_count == 1


async def test_alias_account_and_remove_token_update_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("client.store_accounts", lambda accounts: None)
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
    client = FioClient(Settings(_env_file=None))

    alias = client.alias_account("2603445200-2010-CZK", "main")
    removed = client.remove_token("main", "aaaabbbbccccdddd")
    listed = _list_accounts(client, include_tokens=True, include_status=False)
    await client.aclose()

    assert alias.account == "main"
    assert removed.token_count == 1
    assert listed.accounts[0].marker_token_key == "1111222233334444"
    assert listed.accounts[0].tokens[0].token_key == "1111222233334444"


async def test_omitted_account_is_ambiguous_with_multiple_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [
        settings_module.FioAccount(
            account_key="a-2010-CZK",
            alias="main",
            account_id="a",
            tokens=[settings_module.FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
            marker_token_key="aaaabbbbccccdddd",
        ),
        settings_module.FioAccount(
            account_key="b-2010-CZK",
            alias="savings",
            account_id="b",
            tokens=[settings_module.FioAccountToken(token_key="1111222233334444", token="token-b")],
            marker_token_key="1111222233334444",
        ),
    ]
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=accounts),
    )
    client = FioClient(Settings(_env_file=None))

    result = await _find_transactions(
        client,
        FindTransactionsQuery(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
        ),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "ambiguous_account"


async def test_last_endpoint_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    result = await _get_new_transactions(
        client,
        NewTransactionsRequest(account="main", confirm_advances_download_marker=False),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "confirmation_required"


@respx.mock
async def test_find_transactions_filters_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-for-test/2026-05-01/2026-05-16/transactions.json"
    ).mock(
        return_value=Response(
            200,
            json=sample_fio_response(
                [
                    sample_transaction(
                        transaction_id="1", amount="490.00", variable_symbol="2026000001"
                    ),
                    sample_transaction(
                        transaction_id="2", amount="-100.00", variable_symbol="2026000002"
                    ),
                    sample_transaction(
                        transaction_id="3", amount="990.00", variable_symbol="2026000001"
                    ),
                ]
            ),
        )
    )
    client = FioClient(settings(monkeypatch))

    first = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            direction="incoming",
            variable_symbol="2026000001",
            limit=1,
        ),
    )
    second = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            direction="incoming",
            variable_symbol="2026000001",
            limit=1,
            cursor=first.next_cursor,
        ),
    )
    await client.aclose()

    assert first.error is None
    assert first.transactions[0].transaction_id == "1"
    assert first.next_cursor is not None
    assert second.error is None
    assert second.transactions[0].transaction_id == "3"
    assert second.rate_limit.consumed_lease is False


async def test_large_period_is_rejected_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    result = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 2, 15),
        ),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "period_too_large"
