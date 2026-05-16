from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import settings as settings_module
from settings import (
    KEYRING_SERVICE,
    FioAccount,
    FioAccountToken,
    Settings,
    credential_scope_id,
    credentials_file_path,
    keyring_service_name,
    load_settings,
    store_accounts,
)


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


def test_full_account_registry_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_ACCOUNTS_JSON", _accounts_json())
    monkeypatch.setattr(settings_module, "load_stored_accounts", lambda: None)

    settings = Settings(_env_file=None)

    assert settings.accounts() == []
    assert settings.startup_tokens() == []


def test_startup_tokens_configuration_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_TOKENS_JSON", '["token-a", " token-b "]')
    monkeypatch.setattr(
        settings_module, "load_stored_accounts", lambda: settings_module.StoredFioAccounts()
    )

    tokens = Settings(_env_file=None).startup_tokens()

    assert [token.get_secret_value() for token in tokens] == ["token-a", "token-b"]


def test_startup_tokens_env_rejects_full_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_TOKENS_JSON", _accounts_json())

    with pytest.raises(ValueError, match="JSON array of token strings"):
        Settings(_env_file=None).startup_tokens()


def test_duplicate_aliases_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    monkeypatch.setattr(settings_module, "load_stored_accounts", lambda: None)

    assert Settings(_env_file=None).accounts() == []
    assert load_settings().accounts() == []


def test_load_settings_uses_stored_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIO_TOKENS_JSON", raising=False)
    account = FioAccount(
        account_key="2603445200-2010-CZK",
        alias="main",
        account_id="2603445200",
        bank_id="2010",
        currency="CZK",
        tokens=[FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
        marker_token_key="aaaabbbbccccdddd",
    )
    monkeypatch.setattr(
        settings_module,
        "load_stored_accounts",
        lambda: settings_module.StoredFioAccounts(accounts=[account]),
    )

    accounts = Settings(_env_file=None).accounts()

    assert accounts == [account]


def test_store_accounts_writes_keyring_and_private_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            set_password=lambda service, account, password: stored.__setitem__(
                (service, account), password
            )
        ),
    )
    account = FioAccount(
        account_key="2603445200-2010-CZK",
        alias="main",
        account_id="2603445200",
        bank_id="2010",
        currency="CZK",
        tokens=[FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
        marker_token_key="aaaabbbbccccdddd",
    )

    store_accounts([account])

    assert (keyring_service_name(), "accounts") in stored
    assert ("fio-mcp", "accounts") not in stored
    cfg = credentials_file_path()
    assert '"token":"token-a"' in cfg.read_text(encoding="utf-8")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_scope_id_uses_canonical_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    first_scope = credential_scope_id()
    monkeypatch.chdir(second)
    second_scope = credential_scope_id()
    monkeypatch.chdir(first / ".")

    assert first_scope != second_scope
    assert credential_scope_id() == first_scope


def test_keyring_service_is_scoped_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            set_password=lambda service, account, password: stored.__setitem__(
                (service, account), password
            )
        ),
    )
    account = FioAccount(
        account_key="2603445200-2010-CZK",
        alias="main",
        account_id="2603445200",
        bank_id="2010",
        currency="CZK",
        tokens=[FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
        marker_token_key="aaaabbbbccccdddd",
    )

    store_accounts([account])

    services = {service for service, _ in stored}
    assert services == {keyring_service_name()}
    assert KEYRING_SERVICE not in services


def test_accounts_file_is_scoped_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(settings_module, "_load_from_keyring", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(set_password=lambda service, account, password: None),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    first_account = FioAccount(
        account_key="first-2010-CZK",
        alias="first",
        account_id="first",
        tokens=[FioAccountToken(token_key="aaaabbbbccccdddd", token="token-a")],
        marker_token_key="aaaabbbbccccdddd",
    )
    store_accounts([first_account])
    first_cfg = credentials_file_path()

    monkeypatch.chdir(second)
    second_account = FioAccount(
        account_key="second-2010-CZK",
        alias="second",
        account_id="second",
        tokens=[FioAccountToken(token_key="1111222233334444", token="token-b")],
        marker_token_key="1111222233334444",
    )
    store_accounts([second_account])
    second_cfg = credentials_file_path()
    second_loaded = Settings(_env_file=None).accounts()

    monkeypatch.chdir(first)
    first_loaded = Settings(_env_file=None).accounts()

    assert first_cfg != second_cfg
    assert first_cfg.parent.parent.name == "scopes"
    assert first_loaded == [first_account]
    assert second_loaded == [second_account]
    assert stat.S_IMODE(first_cfg.stat().st_mode) == 0o600
