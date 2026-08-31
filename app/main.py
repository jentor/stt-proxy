"""FastAPI application entry point.

Boots the HTTP server, wires up providers based on env configuration, and
refuses to start when no provider credentials are present.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI

from uvicorn.config import LOGGING_CONFIG

from .config import Settings, load_settings
from .providers import Provider, YandexProvider
from .router import router


def _configure_logging(level: str) -> None:
    """Install a single root logger that talks to stderr."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def _build_providers(settings: Settings) -> dict[str, Provider]:
    """Instantiate every provider whose credentials are configured."""
    providers: dict[str, Provider] = {}
    if settings.yandex_enabled:
        providers["yandex"] = YandexProvider(
            api_key=settings.yandex_api_key,  # type: ignore[arg-type]
            folder_id=settings.yandex_folder_id,  # type: ignore[arg-type]
            default_model=settings.yandex_model,
        )
        logging.getLogger(__name__).info(
            "Yandex provider enabled (folder_id=%s, model=%s)",
            settings.yandex_folder_id,
            settings.yandex_model,
        )
    return providers


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    app.state.providers = _build_providers(settings)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. ``settings`` is injected for tests."""
    if settings is None:
        settings = load_settings()
    _configure_logging(settings.log_level)

    app = FastAPI(
        title="stt-proxy",
        description=(
            "OpenAI Audio API compatible proxy for Yandex SpeechKit. "
            "POST /v1/audio/transcriptions accepts the OpenAI multipart shape."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    return app


# Module-level app for `uvicorn app.main:app` and `task dev`.
app = create_app()


def run_server(
    settings: Settings,
    *,
    reload: bool = False,
    log_config: dict[str, Any] | None = LOGGING_CONFIG,
) -> None:
    """Launch uvicorn against the module-level ``app`` object.

    This is the shared "start serving HTTP" routine used by both the
    foreground entry point (:func:`run`, via ``task run`` / ``task dev``) and
    the detached daemon launched by ``stt-proxy start`` (see
    :mod:`app.daemon`). ``reload`` is always ``False`` in the daemon — live
    reload is a development convenience and meaningless for a production
    background process.

    ``log_config`` is forwarded to uvicorn. The default
    (:data:`uvicorn.config.LOGGING_CONFIG`) preserves the pretty coloured
    stderr output users expect from ``task run`` / ``task dev``. The daemon
    passes ``None`` so uvicorn leaves logging alone — letting the daemon's
    own :class:`~logging.handlers.RotatingFileHandler` capture uvicorn's
    startup banner and access logs instead of losing them to ``/dev/null``.
    """
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=settings.workers,
        reload=reload,
        log_config=log_config,
    )


def run() -> None:
    """Entry point used by ``task run`` / ``task dev``.

    Honours the ``STT_PROXY_RELOAD`` env var (set to ``1``/``true`` by the
    dev task) to enable uvicorn's auto-reload. All other configuration
    (host, port, workers, log level) comes from ``.env`` / shell env via
    :func:`load_settings`.

    The detached ``stt-proxy`` CLI (``stt-proxy start``) does NOT go through
    this function — see :mod:`app.cli` and :mod:`app.daemon`.
    """
    settings = load_settings()
    reload_enabled = os.getenv("STT_PROXY_RELOAD", "").lower() in {"1", "true", "yes"}
    run_server(settings, reload=reload_enabled)


if __name__ == "__main__":
    run()
