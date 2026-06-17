"""FastAPI application entry point.

Boots the HTTP server, wires up providers based on env configuration, and
refuses to start when no provider credentials are present.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .config import Settings, load_settings
from .providers import Provider, SaluteProvider, YandexProvider
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
    if settings.salute_enabled:
        providers["salute"] = SaluteProvider(
            credentials=settings.salute_credentials or ""
        )  # type: ignore[arg-type]
        logging.getLogger(__name__).info("SaluteSpeech provider enabled")
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
            "OpenAI Audio API compatible proxy for Yandex SpeechKit and Sber SaluteSpeech. "
            "POST /v1/audio/transcriptions accepts the OpenAI multipart shape and routes "
            "to the configured backend."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    return app


# Module-level app for `uvicorn app.main:app` and `task dev`.
app = create_app()


def run() -> None:
    """Entry point used by ``stt-proxy`` console script and Taskfile."""
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=settings.workers,
        reload=False,
    )


if __name__ == "__main__":
    run()
