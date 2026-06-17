"""Sber SaluteSpeech provider.

Wraps the asynchronous ``salute_speech`` SDK (``SaluteSpeechClient`` +
``Audio.Transcriptions.create``). The SDK auto-detects the audio codec from
the bytes via pydub, so we just hand it the normalized payload.
"""

from __future__ import annotations

import io
import logging

from .base import (
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
)


logger = logging.getLogger(__name__)


# The Salute REST API supports these language codes for STT.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "ru-RU",
        "en-US",
        "de-DE",
        "es-ES",
        "fr-FR",
        "it-IT",
        "kk-KZ",
        "pt-BR",
        "pt-PT",
        "tr-TR",
    }
)


def _coerce_language(language: str | None) -> str:
    """Pick a sensible Salute language code from the OpenAI request.

    OpenAI uses ISO-639-1 codes like ``en``, ``ru``. Salute uses BCP-47 like
    ``ru-RU``. We map a small set and fall back to ``ru-RU`` (the SDK's default
    and the only fully-tested code).
    """
    if not language:
        return "ru-RU"
    lang = language.strip()
    if "-" in lang or "_" in lang:
        lang = lang.replace("_", "-")
        if lang in SUPPORTED_LANGUAGES:
            return lang
    short = lang.lower()
    mapping = {
        "ru": "ru-RU",
        "en": "en-US",
        "de": "de-DE",
        "es": "es-ES",
        "fr": "fr-FR",
        "it": "it-IT",
        "kk": "kk-KZ",
        "pt": "pt-PT",
        "tr": "tr-TR",
    }
    return mapping.get(short, "ru-RU")


class SaluteProvider(Provider):
    """SaluteSpeech provider backed by the ``salute_speech`` SDK."""

    name = "salute"

    def __init__(self, credentials: str):
        if not credentials:
            raise ProviderNotConfiguredError("SaluteSpeech credentials are empty")
        # Import lazily so the package is optional at module-import time.
        from salute_speech.speech_recognition import SaluteSpeechClient

        self._client = SaluteSpeechClient(client_credentials=credentials)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        from salute_speech.speech_recognition import SpeechRecognitionConfig

        language = _coerce_language(request.language)

        # The SDK consumes a binary file object. Pass a BytesIO so the request
        # is fully self-contained (no temp file on disk).
        file_obj = io.BytesIO(request.audio.data)
        # The SDK uses pydub to introspect the stream; giving it a name helps
        # debugging if anyone enables debug_dump.
        if request.audio.original_filename:
            file_obj.name = request.audio.original_filename  # type: ignore[attr-defined]

        config = SpeechRecognitionConfig(
            hypotheses_count=1,
            enable_profanity_filter=False,
        )
        if request.prompt:
            # Salute has no first-class prompt slot; encode it as a hint phrase
            # via the ``hints`` map so it still biases vocabulary.
            config = SpeechRecognitionConfig(
                hypotheses_count=1,
                enable_profanity_filter=False,
                hints={"words": [request.prompt]},
            )

        try:
            response = await self._client.audio.transcriptions.create(
                file=file_obj,
                language=language,
                config=config,
            )
        except Exception as exc:  # SDK raises a mix of HTTPError / domain errors
            logger.exception("Salute transcription failed")
            raise ProviderError(f"Salute transcription failed: {exc}") from exc

        # Map the SDK's Whisper-shaped response to our normalized result.
        segments: list[TranscriptionSegment] = []
        for idx, seg in enumerate(response.segments or []):
            text = getattr(seg, "text", "") or ""
            segments.append(
                TranscriptionSegment(
                    id=idx,
                    start=float(getattr(seg, "start", 0.0) or 0.0),
                    end=float(getattr(seg, "end", 0.0) or 0.0),
                    text=text,
                    words=[
                        w.model_dump() if hasattr(w, "model_dump") else dict(w)
                        for w in (getattr(seg, "words", None) or [])
                    ],
                )
            )

        return TranscriptionResult(
            text=response.text,
            language=response.language or language,
            duration=float(response.duration)
            if response.duration is not None
            else None,
            segments=segments,
        )
