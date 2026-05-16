from __future__ import annotations

import pytest

from settings import Settings


def test_single_token_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIO_API_TOKEN", "single-token")
    monkeypatch.setenv("FIO_ACCOUNT_ALIAS", "main")

    accounts = Settings(_env_file=None).accounts()

    assert len(accounts) == 1
    assert accounts[0].alias == "main"
    assert accounts[0].token.get_secret_value() == "single-token"


def test_multi_token_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FIO_ACCOUNTS_JSON",
        '[{"alias":"main","label":"Main","token":"token-a"},'
        '{"alias":"savings","label":"Savings","token":"token-b"}]',
    )

    accounts = Settings(_env_file=None).accounts()

    assert [account.alias for account in accounts] == ["main", "savings"]
    assert [account.label for account in accounts] == ["Main", "Savings"]


def test_duplicate_aliases_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FIO_ACCOUNTS_JSON",
        '[{"alias":"main","token":"token-a"},{"alias":"main","token":"token-b"}]',
    )

    with pytest.raises(ValueError, match="aliases must be unique"):
        Settings(_env_file=None).accounts()


def test_missing_configuration_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIO_API_TOKEN", raising=False)
    monkeypatch.delenv("FIO_ACCOUNTS_JSON", raising=False)

    with pytest.raises(ValueError, match="Configure FIO_API_TOKEN"):
        Settings(_env_file=None).accounts()
