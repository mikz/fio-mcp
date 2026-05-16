from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KEYRING_SERVICE = "fio-mcp"
KEYRING_ACCOUNTS_ACCOUNT = "accounts"
CREDENTIAL_SCOPE_ID_LENGTH = 16
ALIAS_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
TOKEN_KEY_RE = re.compile(r"[a-f0-9]{16}")


class FioAccountToken(BaseModel):
    token_key: str
    token: SecretStr
    added_at: datetime | None = None

    @field_validator("token_key")
    @classmethod
    def validate_token_key(cls, value: str) -> str:
        value = value.strip()
        if not TOKEN_KEY_RE.fullmatch(value):
            raise ValueError("token_key must be a 16-character lowercase hex prefix")
        return value


class FioAccount(BaseModel):
    account_key: str
    alias: str | None = None
    account_id: str
    bank_id: str | None = None
    currency: str | None = None
    iban: str | None = None
    bic: str | None = None
    tokens: list[FioAccountToken]
    marker_token_key: str

    @property
    def handle(self) -> str:
        return self.alias or self.account_key

    @field_validator("account_key", "account_id")
    @classmethod
    def validate_required_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("account identifiers cannot be blank")
        return value

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not ALIAS_RE.fullmatch(value):
            raise ValueError("alias must be 1-64 letters, numbers, underscores, or dashes")
        return value

    @field_validator("bank_id", "currency", "iban", "bic")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_tokens(self) -> FioAccount:
        if not self.tokens:
            raise ValueError("Fio accounts must have at least one token")
        token_keys = [token.token_key for token in self.tokens]
        if len(set(token_keys)) != len(token_keys):
            raise ValueError("Fio account token keys must be unique")
        if self.marker_token_key not in set(token_keys):
            raise ValueError("marker_token_key must reference an account token")
        return self


class StoredFioAccounts(BaseModel):
    accounts: list[FioAccount] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_accounts(self) -> StoredFioAccounts:
        account_keys = [account.account_key for account in self.accounts]
        if len(set(account_keys)) != len(account_keys):
            raise ValueError("Fio account keys must be unique")

        aliases = [account.alias for account in self.accounts if account.alias]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Fio account aliases must be unique")
        collisions = set(aliases) & set(account_keys)
        if collisions:
            raise ValueError("Fio account aliases must not collide with account keys")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.e2e"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fio_tokens_json: str | None = Field(default=None, alias="FIO_TOKENS_JSON")
    fio_base_url: AnyHttpUrl = Field(
        default="https://fioapi.fio.cz/v1/rest/",
        alias="FIO_BASE_URL",
    )
    fio_timeout_seconds: float = Field(default=30.0, alias="FIO_TIMEOUT_SECONDS", gt=0)
    fio_rate_limit_seconds: float = Field(default=31.0, alias="FIO_RATE_LIMIT_SECONDS", ge=0)
    fio_cache_ttl_active_seconds: int = Field(
        default=600,
        alias="FIO_CACHE_TTL_ACTIVE_SECONDS",
        ge=0,
    )
    fio_cache_ttl_historical_seconds: int = Field(
        default=86_400,
        alias="FIO_CACHE_TTL_HISTORICAL_SECONDS",
        ge=0,
    )
    fio_cache_ttl_last_seconds: int = Field(default=600, alias="FIO_CACHE_TTL_LAST_SECONDS", ge=0)
    fio_max_period_days: int = Field(default=31, alias="FIO_MAX_PERIOD_DAYS", ge=1)

    def accounts(self) -> list[FioAccount]:
        stored = load_stored_accounts()
        return stored.accounts if stored is not None else []

    def startup_tokens(self) -> list[SecretStr]:
        if not self.fio_tokens_json:
            return []
        try:
            raw = json.loads(self.fio_tokens_json)
        except json.JSONDecodeError as exc:
            raise ValueError("FIO_TOKENS_JSON must be valid JSON") from exc
        if not isinstance(raw, list):
            raise ValueError("FIO_TOKENS_JSON must be a JSON array of token strings")
        tokens: list[SecretStr] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("FIO_TOKENS_JSON must contain only non-empty token strings")
            tokens.append(SecretStr(item.strip()))
        return tokens


def credentials_file_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fio-mcp" / "scopes" / credential_scope_id() / "accounts.json"


def credential_scope_cwd() -> Path:
    return Path.cwd().resolve()


def credential_scope_id() -> str:
    scope = str(credential_scope_cwd()).encode("utf-8")
    return sha256(scope).hexdigest()[:CREDENTIAL_SCOPE_ID_LENGTH]


def keyring_service_name() -> str:
    return f"{KEYRING_SERVICE}:{credential_scope_id()}"


def _load_from_keyring() -> StoredFioAccounts | None:
    try:
        import keyring
    except Exception:
        return None
    try:
        payload = keyring.get_password(keyring_service_name(), KEYRING_ACCOUNTS_ACCOUNT)
    except Exception:
        return None
    if not payload:
        return None
    try:
        return _parse_accounts_json(payload)
    except ValueError:
        return None


def _load_from_file() -> StoredFioAccounts | None:
    cfg = credentials_file_path()
    if not cfg.is_file():
        return None
    try:
        payload = cfg.read_text(encoding="utf-8")
        return _parse_accounts_json(payload)
    except (OSError, ValueError):
        return None


def load_stored_accounts() -> StoredFioAccounts | None:
    return _load_from_keyring() or _load_from_file()


def store_accounts(accounts: list[FioAccount]) -> None:
    payload = _accounts_payload_json(accounts)
    try:
        import keyring

        keyring.set_password(keyring_service_name(), KEYRING_ACCOUNTS_ACCOUNT, payload)
    except Exception:
        pass

    cfg = credentials_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(payload + "\n", encoding="utf-8")
    with suppress(OSError):
        cfg.chmod(0o600)


def _parse_accounts_json(value: str) -> StoredFioAccounts:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Stored Fio accounts must be valid JSON") from exc
    if isinstance(raw, list):
        raw = {"accounts": raw}
    if not isinstance(raw, dict):
        raise ValueError("Stored Fio accounts must be a JSON object or array")
    return StoredFioAccounts.model_validate(raw)


def _accounts_payload_json(accounts: list[FioAccount]) -> str:
    payload: dict[str, Any] = {
        "accounts": [
            {
                "account_key": account.account_key,
                "alias": account.alias,
                "account_id": account.account_id,
                "bank_id": account.bank_id,
                "currency": account.currency,
                "iban": account.iban,
                "bic": account.bic,
                "tokens": [
                    {
                        "token_key": token.token_key,
                        "token": token.token.get_secret_value(),
                        "added_at": (
                            token.added_at.astimezone(UTC).isoformat()
                            if token.added_at is not None
                            else None
                        ),
                    }
                    for token in account.tokens
                ],
                "marker_token_key": account.marker_token_key,
            }
            for account in accounts
        ]
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_settings() -> Settings:
    settings = Settings()
    settings.accounts()
    settings.startup_tokens()
    return settings
