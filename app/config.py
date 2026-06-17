"""Application configuration loaded from environment variables.

Only providers whose required credentials are present are enabled. When no
provider has credentials, the application refuses to start.

All environment variables are namespaced under ``STT_PROXY_`` (configured via
``Settings.model_config.env_prefix``):

  * ``STT_PROXY_HOST`` / ``STT_PROXY_PORT`` / ``STT_PROXY_LOG_LEVEL`` / ``STT_PROXY_WORKERS``
  * ``STT_PROXY_YANDEX_API_KEY`` / ``STT_PROXY_YANDEX_FOLDER_ID`` / ``STT_PROXY_YANDEX_MODEL``
  * ``STT_PROXY_SALUTESPEECH_KEY``
  * ``STT_PROXY_DEFAULT_PROVIDER``

The ``.env`` file in the working directory is loaded automatically by
pydantic-settings; ``uv run --env-file .env`` in the Taskfile is a no-op
duplication that makes ``.env`` visible to ``env | grep`` for debugging.

The detached daemon launched by ``stt-proxy start`` deliberately disables
``.env`` loading so that the background process only sees variables exported
in the calling shell — this matches user expectations for a system service
and avoids surprises when ``.env`` is edited later. This is implemented via
the ``STT_PROXY_DAEMON`` environment variable (set by the CLI when spawning
the daemon) which makes :func:`load_settings` skip ``.env`` even on the
implicit call made by the module-level ``app = create_app()`` during
uvicorn's string import. See :func:`load_settings` for the full resolution
rules.
"""

from __future__ import annotations

import logging
import os
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
        env_prefix="STT_PROXY_",
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
    yandex_api_key: str | None = Field(default=None, description="Yandex Cloud API key")
    yandex_folder_id: str | None = Field(
        default=None, description="Yandex Cloud folder ID"
    )
    yandex_model: str = Field(
        default="general",
        description="Default Yandex STT model tag (general, general:rc, general:deprecated, deferred-general, ...)",
    )

    # SaluteSpeech: single base64-encoded "client_id:client_secret" token.
    # See https://developers.sber.ru/docs/ru/salutespeech/rest/post-token
    salutespeech_key: str | None = Field(
        default=None,
        description="Salute Speech authorization key: base64 of client_id:client_secret",
    )

    # ---- Routing -----------------------------------------------------------
    # Default provider when the request `model` doesn't carry a routing prefix.
    # If only one provider is configured, it is used regardless of this value.
    default_provider: ProviderName | None = Field(
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
                "STT_PROXY_YANDEX_API_KEY and STT_PROXY_YANDEX_FOLDER_ID must be set together; "
                "Yandex provider will be disabled"
            )
            self._yandex_enabled = False

        # Salute is enabled when its single credential is present.
        self._salute_enabled = bool(self.salutespeech_key)
        self._salute_credentials = self.salutespeech_key

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
        if self.default_provider:
            return self.default_provider
        if len(self.enabled_providers) == 1:
            return self.enabled_providers[0]
        return None

    def resolve_default_provider_or_fail(self) -> ProviderName:
        """Same as effective_default_provider() but raises if no provider is configured."""
        providers = self.enabled_providers
        if not providers:
            raise RuntimeError(
                "No STT provider configured. Set STT_PROXY_YANDEX_API_KEY + STT_PROXY_YANDEX_FOLDER_ID for Yandex, "
                "or STT_PROXY_SALUTESPEECH_KEY for Salute Speech."
            )
        default = self.effective_default_provider()
        if default is None:
            raise RuntimeError(
                "Multiple STT providers configured but STT_PROXY_DEFAULT_PROVIDER is unset. "
                "Either set STT_PROXY_DEFAULT_PROVIDER=yandex|salute, or use a prefixed model name "
                "in requests (yandex-* / salute-*)."
            )
        return default


# Sentinel used to distinguish "caller passed nothing" from "caller passed None".
# Without this, ``load_settings(env_file=None)`` (explicit "no .env") would be
# indistinguishable from ``load_settings()`` (default) once we make the
# default context-sensitive (see the ``STT_PROXY_DAEMON`` flag below).
_DEFAULT_ENV_FILE: object = object()


def load_settings(
    *, env_file: str | os.PathLike[str] | None | object = _DEFAULT_ENV_FILE
) -> Settings:
    """Build Settings and exit with a clear error if nothing is configured.

    ``env_file`` is forwarded to pydantic-settings as ``_env_file`` (note the
    leading underscore — pydantic-settings reserves ``env_file`` for the model
    config dict and exposes the per-call override through ``_env_file``).

    Resolution rules:

    * ``load_settings(env_file=".env")`` (or any explicit path) — that path
      only.
    * ``load_settings(env_file=None)`` — never reads ``.env`` (used by the
      detached daemon launched via ``stt-proxy start``).
    * ``load_settings()`` (no argument) — reads ``.env`` *unless* the
      ``STT_PROXY_DAEMON`` environment variable is set, in which case ``.env``
      is skipped. This matters because :func:`uvicorn.run` is called with the
      string import ``"app.main:app"``, which re-executes the module-level
      ``app = create_app()`` → ``load_settings()`` inside the worker; setting
      ``STT_PROXY_DAEMON=1`` in the daemon's environment (done by
      :func:`app.cli._cmd_start`) makes that implicit call honor the
      "shell-env-only" contract.
    """
    if env_file is _DEFAULT_ENV_FILE:
        env_file = None if os.environ.get("STT_PROXY_DAEMON") else ".env"
    settings = Settings(_env_file=env_file)  # type: ignore[arg-type]
    if not settings.enabled_providers:
        msg = (
            "No STT provider configured.\n"
            "Set STT_PROXY_YANDEX_API_KEY + STT_PROXY_YANDEX_FOLDER_ID for Yandex SpeechKit, and/or\n"
            "STT_PROXY_SALUTESPEECH_KEY for Salute Speech.\n"
            "Exiting."
        )
        print(msg, file=sys.stderr)
        raise SystemExit(2)
    return settings
