"""Application configuration loaded from environment variables / config file.

Only providers whose required credentials are present are enabled. When no
provider has credentials, the application refuses to start (unless called
with ``validate=False``).

All environment variables are namespaced under ``STT_PROXY_`` (configured via
``Settings.model_config.env_prefix``):

  * ``STT_PROXY_HOST`` / ``STT_PROXY_PORT`` / ``STT_PROXY_LOG_LEVEL`` / ``STT_PROXY_WORKERS``
  * ``STT_PROXY_YANDEX_API_KEY`` / ``STT_PROXY_YANDEX_FOLDER_ID`` / ``STT_PROXY_YANDEX_MODEL``
  * ``STT_PROXY_SALUTESPEECH_KEY``
  * ``STT_PROXY_DEFAULT_PROVIDER``

Two non-env sources are also supported, depending on which "view" the caller
asks for (see :func:`load_settings`):

  * The ``.env`` file in the working directory — used by the foreground /
    development entry points (``task run`` / ``task dev`` / bare
    ``python -m app.main``). ``uv run --env-file .env`` in the Taskfile is a
    no-op duplication that makes ``.env`` visible to ``env | grep`` for
    debugging.
  * The TOML config file at :func:`_config_file_path` (typically
    ``~/.config/stt-proxy/config.toml``) — used by the detached daemon
    launched by ``stt-proxy start``. This matches user expectations for a
    system service and keeps secrets out of the shell history / process
    table.

The detached daemon launched by ``stt-proxy start`` deliberately disables
``.env`` loading so that the background process only sees variables exported
in the calling shell (plus the optional TOML config file). This is
implemented via the ``STT_PROXY_DAEMON`` environment variable (set by the
CLI when spawning the daemon) which makes :func:`load_settings` skip
``.env`` even on the implicit call made by the module-level
``app = create_app()`` during uvicorn's string import. See
:func:`load_settings` for the full resolution rules.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


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

    # Transient override used to thread the TOML config file path from
    # load_settings() into settings_customise_sources(). Set just before
    # instantiation and reset to None in a finally block.
    #
    # This ClassVar exists because pydantic-settings 2.14.x does NOT expose
    # ``_toml_file`` as a constructor kwarg (unlike ``_env_file``) — the only
    # way to pass a per-call TOML path is to customise the sources.
    _toml_file_override: ClassVar[str | Path | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add TomlConfigSettingsSource when load_settings() requests it.

        Source priority is the order of the returned tuple (earlier = higher
        priority): init kwargs > shell env > .env > TOML > secrets dir.
        In the daemon view ``_env_file=None`` so the .env source is a no-op,
        effectively giving: shell env > config.toml > defaults.
        """
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
            dotenv_settings,
        ]
        if cls._toml_file_override is not None:
            sources.append(
                TomlConfigSettingsSource(
                    settings_cls, toml_file=cls._toml_file_override
                )
            )
        sources.append(file_secret_settings)
        return tuple(sources)

    # ---- HTTP server -------------------------------------------------------
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the HTTP server. Default 127.0.0.1 (localhost only); "
            "set to 0.0.0.0 to accept connections from other hosts on the network."
        ),
    )
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


def _config_file_path() -> Path:
    """Return the location of the optional TOML config file.

    Honours ``XDG_CONFIG_HOME``; defaults to ``~/.config`` on every platform
    (macOS included — we deliberately do NOT use ``platformdirs`` here, which
    would point at ``~/Library/Application Support`` on Darwin).
    """
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return base / "stt-proxy" / "config.toml"


class ConfigFileError(Exception):
    """Raised when the TOML config file is unreadable or malformed.

    Carries a human-readable message that is safe to print to stderr.
    """


# Deliberate duplication of pydantic-settings' own TOML parse: we want to
# validate the file *before* pydantic-settings touches it, so a malformed
# file produces a clear CLI error instead of an opaque failure later.
def _parse_config_file(path: Path) -> dict[str, Any]:
    """Parse the TOML config file. Returns ``{}`` if the file doesn't exist.

    Raises :class:`ConfigFileError` if the file exists but cannot be read
    (permission denied, I/O error) or parsed (invalid TOML).
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(
            f"Invalid TOML in {path}: {type(exc).__name__}: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigFileError(f"Cannot read config file {path}: {exc}") from exc


def load_settings(
    *,
    env_file: str | os.PathLike[str] | None | object = _DEFAULT_ENV_FILE,
    daemon: bool | None = None,
    validate: bool = True,
) -> Settings:
    """Build Settings, optionally exiting with a clear error if nothing is configured.

    Parameters
    ----------
    env_file:
        Forwarded to pydantic-settings as ``_env_file`` (note the leading
        underscore — pydantic-settings reserves ``env_file`` for the model
        config dict and exposes the per-call override through ``_env_file``).
        Passing the ``_DEFAULT_ENV_FILE`` sentinel (the default) makes the
        value context-sensitive (see "Resolution rules" below).
    daemon:
        Selects which "view" of the configuration to load.

        * ``None`` (default) — auto-detect from the ``STT_PROXY_DAEMON``
          environment variable. This preserves today's implicit behaviour
          for all existing call sites (``app.main.create_app()``,
          ``app.main.run()``, ``app.daemon.main()``).
        * ``True`` — force the daemon view: skip ``.env``, optionally load
          the TOML config file at :func:`_config_file_path`.
        * ``False`` — force the foreground view: load ``.env`` (or whatever
          ``env_file`` was passed), ignore the TOML config file.
    validate:
        * ``True`` (default) — exit with ``SystemExit(2)`` and a clear
          message when no provider is configured.
        * ``False`` — skip that check; return whatever Settings (possibly
          with zero providers enabled) pydantic-settings produced. Used by
          ``stt-proxy config`` so it can render a partial view when nothing
          is configured yet.

    Resolution rules:

    * ``daemon`` resolves to ``os.environ.get("STT_PROXY_DAEMON") is not
      None`` when called as ``daemon=None``.
    * ``env_file`` (when still the ``_DEFAULT_ENV_FILE`` sentinel) resolves
      to ``None`` in the daemon view and ``".env"`` in the foreground view.
      An explicitly-passed value (including ``None``) is used as-is — this
      preserves ``app.daemon.main()``'s explicit ``env_file=None``.
    * TOML config file: only consulted in the daemon view, and only when
      the file actually exists. Threaded into :class:`Settings` via the
      ``_toml_file_override`` ClassVar + ``settings_customise_sources``
      override (pydantic-settings 2.14.x doesn't expose ``_toml_file`` as
      a constructor kwarg).

    File-loading precedence in the daemon view (highest first):

    1. shell environment variables (``STT_PROXY_*``)
    2. ``~/.config/stt-proxy/config.toml`` (if present)
    3. built-in defaults

    In the foreground view, the precedence is shell env → ``.env`` →
    defaults (the TOML file is never read).
    """
    if daemon is None:
        daemon = os.environ.get("STT_PROXY_DAEMON") is not None
    if env_file is _DEFAULT_ENV_FILE:
        env_file = None if daemon else ".env"
    config_path = _config_file_path()
    toml_path = str(config_path) if daemon and config_path.is_file() else None
    Settings._toml_file_override = toml_path
    try:
        settings = Settings(_env_file=env_file)  # type: ignore[arg-type]
    finally:
        Settings._toml_file_override = None
    if validate and not settings.enabled_providers:
        msg = (
            "No STT provider configured.\n"
            "Set STT_PROXY_YANDEX_API_KEY + STT_PROXY_YANDEX_FOLDER_ID for Yandex SpeechKit, and/or\n"
            "STT_PROXY_SALUTESPEECH_KEY for Salute Speech.\n"
            "Exiting."
        )
        print(msg, file=sys.stderr)
        raise SystemExit(2)
    return settings
