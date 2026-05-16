from __future__ import annotations

import json
import re

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FioAccount(BaseModel):
    alias: str
    label: str | None = None
    token: SecretStr

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
            raise ValueError("alias must be 1-64 letters, numbers, underscores, or dashes")
        return value

    @field_validator("label")
    @classmethod
    def blank_label_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.e2e"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fio_accounts_json: str | None = Field(default=None, alias="FIO_ACCOUNTS_JSON")
    fio_api_token: SecretStr | None = Field(default=None, alias="FIO_API_TOKEN")
    fio_account_alias: str = Field(default="main", alias="FIO_ACCOUNT_ALIAS")
    fio_account_label: str | None = Field(default=None, alias="FIO_ACCOUNT_LABEL")
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
        if self.fio_accounts_json:
            try:
                raw_accounts = json.loads(self.fio_accounts_json)
            except json.JSONDecodeError as exc:
                raise ValueError("FIO_ACCOUNTS_JSON must be valid JSON") from exc
            if not isinstance(raw_accounts, list):
                raise ValueError("FIO_ACCOUNTS_JSON must be a JSON array")
            accounts = [FioAccount.model_validate(raw) for raw in raw_accounts]
        elif self.fio_api_token is not None:
            accounts = [
                FioAccount(
                    alias=self.fio_account_alias,
                    label=self.fio_account_label,
                    token=self.fio_api_token,
                )
            ]
        else:
            raise ValueError("Configure FIO_API_TOKEN or FIO_ACCOUNTS_JSON")

        aliases = [account.alias for account in accounts]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Fio account aliases must be unique")
        return accounts


def load_settings() -> Settings:
    settings = Settings()
    settings.accounts()
    return settings
