from __future__ import annotations

import pytest

import settings as settings_module
from settings import FioAccount, FioAccountToken, Settings, load_settings


def _accounts_json() -> str:
    return """
    {
      "accounts": [
        {
          "account_key": "2603445200-2010-CZK",
          "alias": "main",
          "account_id": "2603445200",
          "bank_id": "2010",
          "currency": "CZK",
          "tokens": [
            {"token_key": "aaaabbbbccccdddd", "token": "token-a"}
          ],
          "marker_token_key": "aaaabbbbccccdddd"
        }
      ]
    }
    """


def test_full_account_registry_env_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_ACCOUNTS_JSON", _accounts_json())

    settings = Settings(_env_file=None)

    assert settings.accounts()[0].alias == "main"
    assert settings.startup_tokens() == []


def test_startup_tokens_configuration_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_TOKENS_JSON", '["token-a", " token-b "]')

    tokens = Settings(_env_file=None).startup_tokens()

    assert [token.get_secret_value() for token in tokens] == ["token-a", "token-b"]


def test_startup_tokens_env_rejects_full_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_TOKENS_JSON", _accounts_json())

    with pytest.raises(ValueError, match="JSON array of token strings"):
        Settings(_env_file=None).startup_tokens()


def test_duplicate_aliases_are_rejected() -> None:
    accounts = [
        FioAccount(
            account_key="a-2010-CZK",
            alias="main",
            account_id="a",
            tokens=[FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
            marker_token_key="aaaabbbbccccdddd",
        ),
        FioAccount(
            account_key="b-2010-CZK",
            alias="main",
            account_id="b",
            tokens=[FioAccountToken(token_key="1111222233334444", token="token-b")],
            marker_token_key="1111222233334444",
        ),
    ]

    with pytest.raises(ValueError, match="aliases must be unique"):
        settings_module.StoredFioAccounts(accounts=accounts)


def test_missing_configuration_starts_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIO_ACCOUNTS_JSON", raising=False)
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)

    assert Settings(_env_file=None).accounts() == []
    assert load_settings().accounts() == []


def test_load_settings_uses_account_registry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setenv("FIO_ACCOUNTS_JSON", _accounts_json())

    accounts = Settings(_env_file=None).accounts()

    assert accounts[0].alias == "main"
