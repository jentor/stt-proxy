"""Application configuration loaded from environment variables.

Only providers whose required credentials are present are enabled. When no
provider has credentials, the application refuses to start.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


ProviderName = Literal["yandex", "salute"]


class Settings(BaseSettings):
    """Runtime configuration for the proxy server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- HTTP server -------------------------------------------------------
    host: str = Field(default="0.0.0.0", description="Bind address for the HTTP server")
    port: int = Field(
        default=8000, ge=1, le=65535, description="Bind port for the HTTP server"
    )
    log_level: str = Field(
        default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR"
    )
    workers: int = Field(
        default=1,
        ge=1,
        description="Number of uvicorn workers (1 recommended; provider SDKs may not be fork-safe)",
    )

    # ---- Provider credentials ---------------------------------------------
    # Yandex SpeechKit: API key + folder ID are both required.
    yandex_api_key: str | None = Field(
        default=None, description="Yandex Cloud API key (also YANDEX_API_KEY)"
    )
    yandex_folder_id: str | None = Field(
        default=None, description="Yandex Cloud folder ID (also YANDEX_FOLDER_ID)"
    )
    yandex_model: str = Field(
        default="general",
        description="Default Yandex STT model tag (general, general:rc, general:deprecated, deferred-general, ...)",
    )

    # Salute Speech: single base64-encoded "client_id:client_secret" token,
    # as documented at https://developers.sber.ru/docs/ru/salutespeech/rest/post-token
    sber_salute_speech_api_key: str | None = Field(
        default=None,
        description="Salute Speech authorization key: base64 of client_id:client_secret",
    )

    # ---- Routing -----------------------------------------------------------
    # Default provider when the request `model` doesn't carry a routing prefix.
    # If only one provider is configured, it is used regardless of this value.
    stt_default_provider: ProviderName | None = Field(
        default=None,
        description="Default provider: 'yandex' or 'salute'. If unset and both providers are configured, requests must use a prefixed model name.",
    )

    @model_validator(mode="after")
    def _validate_providers(self) -> "Settings":
        # Yandex is enabled only when both the API key AND folder id are present.
        self._yandex_enabled = bool(self.yandex_api_key and self.yandex_folder_id)
        if (self.yandex_api_key and not self.yandex_folder_id) or (
            self.yandex_folder_id and not self.yandex_api_key
        ):
            logger.warning(
                "YANDEX_API_KEY and YANDEX_FOLDER_ID must be set together; Yandex provider will be disabled"
            )
            self._yandex_enabled = False

        # Salute is enabled when its single credential is present.
        self._salute_enabled = bool(self.sber_salute_speech_api_key)
        self._salute_credentials = self.sber_salute_speech_api_key

        return self

    # ---- Computed properties ----------------------------------------------
    @property
    def yandex_enabled(self) -> bool:
        return getattr(self, "_yandex_enabled", False)

    @property
    def salute_enabled(self) -> bool:
        return getattr(self, "_salute_enabled", False)

    @property
    def salute_credentials(self) -> str | None:
        return getattr(self, "_salute_credentials", None)

    @property
    def enabled_providers(self) -> list[ProviderName]:
        providers: list[ProviderName] = []
        if self.yandex_enabled:
            providers.append("yandex")
        if self.salute_enabled:
            providers.append("salute")
        return providers

    def effective_default_provider(self) -> ProviderName | None:
        """Resolve the provider used when a request's `model` has no routing prefix."""
        if self.stt_default_provider:
            return self.stt_default_provider
        if len(self.enabled_providers) == 1:
            return self.enabled_providers[0]
        return None

    def resolve_default_provider_or_fail(self) -> ProviderName:
        """Same as effective_default_provider() but raises if no provider is configured."""
        providers = self.enabled_providers
        if not providers:
            raise RuntimeError(
                "No STT provider configured. Set YANDEX_API_KEY + YANDEX_FOLDER_ID for Yandex, "
                "or SBER_SALUTE_SPEECH_API_KEY for Salute Speech."
            )
        default = self.effective_default_provider()
        if default is None:
            raise RuntimeError(
                "Multiple STT providers configured but STT_DEFAULT_PROVIDER is unset. "
                "Either set STT_DEFAULT_PROVIDER=yandex|salute, or use a prefixed model name "
                "in requests (yandex-* / salute-*)."
            )
        return default


def load_settings() -> Settings:
    """Build Settings and exit with a clear error if nothing is configured."""
    settings = Settings()
    if not settings.enabled_providers:
        msg = (
            "No STT provider configured.\n"
            "Set YANDEX_API_KEY + YANDEX_FOLDER_ID for Yandex SpeechKit, and/or\n"
            "SBER_SALUTE_SPEECH_API_KEY for Salute Speech.\n"
            "Exiting."
        )
        print(msg, file=sys.stderr)
        raise SystemExit(2)
    return settings
