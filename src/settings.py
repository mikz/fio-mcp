from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ID = "zsb-gwscli"
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
        return self.alias or self.bank_account or self.account_key

    @property
    def bank_account(self) -> str | None:
        if self.bank_id is None:
            return None
        return f"{self.account_id}/{self.bank_id}"

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

    fio_accounts_json: str | None = Field(default=None, alias="FIO_ACCOUNTS_JSON")
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
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    project_id: str = Field(default=PROJECT_ID, alias="PROJECT_ID")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_SECRET")
    base_url: str | None = Field(default=None, alias="BASE_URL")
    jwt_signing_key: str | None = Field(default=None, alias="JWT_SIGNING_KEY")
    storage_encryption_key: str | None = Field(default=None, alias="STORAGE_ENCRYPTION_KEY")
    oauth_storage_backend: Literal["firestore", "filetree"] = Field(
        default="firestore",
        alias="OAUTH_STORAGE_BACKEND",
    )
    oauth_storage_dir: str = Field(default="/tmp/fio-mcp/oauth", alias="OAUTH_STORAGE_DIR")
    firestore_database: str = Field(default="(default)", alias="FIRESTORE_DATABASE")
    firestore_collection: str = Field(default="fio-mcp-oauth", alias="FIRESTORE_COLLECTION")

    def accounts(self) -> list[FioAccount]:
        if self.fio_accounts_json:
            return _parse_accounts_json(self.fio_accounts_json).accounts
        return []

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
