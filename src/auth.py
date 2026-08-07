from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.firestore import FirestoreStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from settings import Settings

MCP_AUTH_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


def _required(value: str | None, name: str) -> str:
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable for HTTP OAuth mode: {name}")


def _oauth_storage(settings: Settings) -> FernetEncryptionWrapper:
    if settings.oauth_storage_backend == "firestore":
        store = FirestoreStore(
            project=settings.project_id,
            database=settings.firestore_database,
            default_collection=settings.firestore_collection,
        )
    elif settings.oauth_storage_backend == "filetree":
        oauth_dir = Path(settings.oauth_storage_dir)
        oauth_dir.mkdir(parents=True, exist_ok=True)
        store = FileTreeStore(
            data_directory=oauth_dir,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(oauth_dir),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(oauth_dir),
        )
    else:
        raise RuntimeError("OAUTH_STORAGE_BACKEND must be 'firestore' or 'filetree'.")
    return FernetEncryptionWrapper(
        key_value=store,
        fernet=Fernet(_required(settings.storage_encryption_key, "STORAGE_ENCRYPTION_KEY")),
    )


def auth_provider(settings: Settings) -> GoogleProvider:
    return GoogleProvider(
        client_id=_required(settings.google_client_id, "GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=_required(settings.google_client_secret, "GOOGLE_OAUTH_CLIENT_SECRET"),
        base_url=_required(settings.base_url, "BASE_URL"),
        required_scopes=MCP_AUTH_SCOPES,
        jwt_signing_key=_required(settings.jwt_signing_key, "JWT_SIGNING_KEY"),
        client_storage=_oauth_storage(settings),
    )


class HttpOAuthFastMCP(FastMCP):
    """FastMCP server that enables OAuth only when an HTTP transport starts."""

    def __init__(self, *args: Any, settings: Settings, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._oauth_settings = settings

    async def run_http_async(self, *args: Any, **kwargs: Any) -> None:
        if self.auth is None:
            self.auth = auth_provider(self._oauth_settings)
        await super().run_http_async(*args, **kwargs)
