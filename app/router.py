"""FastAPI router exposing OpenAI-compatible ``/v1/audio/transcriptions``."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from .audio import normalize
from .providers import (
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
    detect_routing,
)
from .response import normalize_format, render


logger = logging.getLogger(__name__)

router = APIRouter()


_TEXT_BASED_FORMATS = {"text", "srt", "vtt"}


@router.post("/v1/audio/transcriptions")
async def create_transcription(
    request: Request,
    file: Annotated[UploadFile, File(description="Audio file to transcribe")],
    model: Annotated[
        str,
        Form(
            description="Model id used for routing; must be prefixed with 'yandex-' or 'salute-' when both providers are configured"
        ),
    ],
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[str | None, Form()] = None,
    temperature: Annotated[float | None, Form()] = None,
    timestamp_granularities: Annotated[list[str] | None, Form()] = None,
):
    """Transcribe audio using the configured Yandex / Salute backend."""
    settings = request.app.state.settings
    providers: dict[str, Provider] = request.app.state.providers

    # 1. Validate response_format up-front so we can fail fast with a clean 400.
    try:
        fmt = normalize_format(response_format)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": str(exc), "type": "invalid_request_error"}},
        ) from exc

    # 2. Pick a provider. Routing precedence:
    #    a) explicit prefix in `model`
    #    b) STT_PROXY_DEFAULT_PROVIDER
    #    c) sole configured provider (if exactly one)
    routed = detect_routing(model)
    if routed:
        if routed not in providers:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": (
                            f"Model '{model}' requests provider '{routed}', but it is not configured. "
                            f"Available providers: {sorted(providers.keys())}."
                        ),
                        "type": "invalid_request_error",
                    }
                },
            )
        provider_name = routed
    else:
        try:
            provider_name = settings.resolve_default_provider_or_fail()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": (
                            f"{exc} Prefix the `model` field with 'yandex-' or 'salute-' to disambiguate."
                        ),
                        "type": "invalid_request_error",
                    }
                },
            ) from exc

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
            {"name": "salute", "enabled": settings.salute_enabled, "model": None},
        ],
        "default_provider": settings.effective_default_provider(),
    }
