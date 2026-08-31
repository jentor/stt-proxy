"""FastAPI router exposing OpenAI-compatible ``/v1/audio/transcriptions``."""

from __future__ import annotations

import logging
from secrets import compare_digest
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, PlainTextResponse

from .audio import normalize
from .providers import (
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
)
from .response import normalize_format, render


logger = logging.getLogger(__name__)


def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    configured = request.app.state.settings.api_key
    if configured is None:
        return

    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not compare_digest(
        credential.encode(), configured.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {"message": "Invalid API key", "type": "authentication_error"}
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(dependencies=[Depends(require_api_key)])


_TEXT_BASED_FORMATS = {"text", "srt", "vtt"}


@router.post("/v1/audio/transcriptions")
async def create_transcription(
    request: Request,
    file: Annotated[UploadFile, File(description="Audio file to transcribe")],
    model: Annotated[
        str,
        Form(description="Yandex model id, for example yandex/general"),
    ],
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[str | None, Form()] = None,
    temperature: Annotated[float | None, Form()] = None,
    timestamp_granularities: Annotated[list[str] | None, Form()] = None,
):
    """Transcribe audio using Yandex SpeechKit."""
    providers: dict[str, Provider] = request.app.state.providers

    # 1. Validate response_format up-front so we can fail fast with a clean 400.
    try:
        fmt = normalize_format(response_format)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": str(exc), "type": "invalid_request_error"}},
        ) from exc

    # 2. Yandex is the only backend.
    provider_name = "yandex"
    provider = providers[provider_name]

    # 3. Read the upload into memory. The OpenAI API supports up to 25 MB
    #    by default for whisper-1; we cap at the same to keep things sane.
    MAX_FILE_BYTES = 25 * 1024 * 1024
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "Uploaded file is empty",
                    "type": "invalid_request_error",
                }
            },
        )
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": {
                    "message": f"Uploaded file exceeds {MAX_FILE_BYTES} bytes",
                    "type": "invalid_request_error",
                }
            },
        )

    # 4. Normalize the audio container (transcode webm/mp4/m4a/flac to WAV).
    try:
        audio = await normalize(data, file.filename, file.content_type)
    except RuntimeError as exc:
        # Transcoding failed (corrupt container, missing ffmpeg, ...).
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": (
                        f"Could not process audio: {exc}. Make sure the upload is a valid "
                        "WAV/MP3/OGG file, or install ffmpeg to enable automatic transcoding "
                        "for mp4/webm/m4a/flac."
                    ),
                    "type": "invalid_request_error",
                }
            },
        ) from exc

    # 5. Build the request and call the provider.
    transcription_req = TranscriptionRequest(
        audio=audio,
        model=model,
        language=language,
        prompt=prompt,
        temperature=temperature,
        response_format=fmt,
        timestamp_granularities=tuple(timestamp_granularities or ()),
    )

    try:
        result = await provider.transcribe(transcription_req)
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": f"Provider '{provider_name}' not configured: {exc}",
                    "type": "server_error",
                }
            },
        ) from exc
    except ProviderError as exc:
        # Surface backend errors as OpenAI-style 502 Bad Gateway.
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": str(exc), "type": "upstream_error"}},
        ) from exc

    # 6. Render the response in the requested shape.
    payload = render(result, fmt)
    if fmt in _TEXT_BASED_FORMATS:
        # text / srt / vtt: return as raw text with the right content-type.
        media_type = {
            "text": "text/plain; charset=utf-8",
            "srt": "application/x-subrip; charset=utf-8",
            "vtt": "text/vtt; charset=utf-8",
        }[fmt]
        return PlainTextResponse(payload, media_type=media_type)

    # json / verbose_json
    return JSONResponse(payload, media_type="application/json")


@router.get("/v1/audio/providers")
async def list_providers(request: Request) -> dict:
    """Health/inspection endpoint listing enabled providers (no secrets)."""
    settings = request.app.state.settings
    return {
        "providers": [
            {
                "name": "yandex",
                "enabled": settings.yandex_enabled,
                "model": settings.yandex_model if settings.yandex_enabled else None,
            },
        ],
        "default_provider": "yandex" if settings.yandex_enabled else None,
    }


# OpenAI's ``/v1/models`` endpoint returns the list of models a client can
# use. Yandex takes a model tag rather than exposing a catalogue API, so the
# response is a curated static list.
_MODEL_CREATED_PLACEHOLDER: int = 0


def _render_models(providers: dict) -> dict:
    data: list[dict] = []
    for provider in providers.values():
        for model in provider.list_models():
            data.append(
                {
                    "id": model.id,
                    "object": "model",
                    "created": _MODEL_CREATED_PLACEHOLDER,
                    "owned_by": model.owned_by,
                }
            )
    return {"object": "list", "data": data}


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    """OpenAI-compatible Yandex model catalogue."""
    return _render_models(request.app.state.providers)


@router.get("/models")
async def list_models_no_prefix(request: Request) -> dict:
    """Alias for ``/v1/models`` — some clients hit the un-prefixed path."""
    return _render_models(request.app.state.providers)
