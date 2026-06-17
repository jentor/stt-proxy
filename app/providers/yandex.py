"""Yandex Cloud SpeechKit (STT v3) provider.

Wraps the ``yandex_ai_studio_sdk`` (``AIStudio.speechkit.speech_to_text``).
The SDK exposes a synchronous ``run(bytes)`` method that blocks internally on
gRPC, so we run it in a worker thread to keep the FastAPI event loop free.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..audio import NormalizedAudio, YANDEX_AUDIO_FORMAT_MAP
from .base import (
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
    yandex_model_tag,
)


logger = logging.getLogger(__name__)


# Yandex language code aliases (ISO-639-1 -> BCP-47).
_LANGUAGE_ALIASES: dict[str, str] = {
    "ru": "ru-RU",
    "en": "en-US",
    "de": "de-DE",
    "es": "es-ES",
    "fi": "fi-FI",
    "fr": "fr-FR",
    "he": "he-IL",
    "it": "it-IT",
    "kk": "kk-KZ",
    "nl": "nl-NL",
    "pl": "pl-PL",
    "pt": "pt-PT",
    "sv": "sv-SE",
    "tr": "tr-TR",
    "uz": "uz-UZ",
}


def _coerce_language(language: str | None) -> str:
    """Convert an OpenAI-style language code to the Yandex SDK's BCP-47 form."""
    if not language:
        return "ru-RU"
    lang = language.strip()
    if "-" in lang or "_" in lang:
        return lang.replace("_", "-")
    return _LANGUAGE_ALIASES.get(lang.lower(), "ru-RU")


def _pick_yandex_audio_format(audio: NormalizedAudio) -> tuple[Any, bool]:
    """Return the (AudioFormat, is_pcm16) tuple for the SDK call.

    Raises ProviderError if the audio format is not supported by Yandex.
    """
    fmt_value = YANDEX_AUDIO_FORMAT_MAP.get(audio.format)
    if fmt_value is None:
        # Caller should have transcoded to WAV; this is a defensive check.
        raise ProviderError(
            f"Yandex provider cannot consume audio container {audio.format.value!r}; "
            "expected MP3, WAV or OGG/OPUS"
        )
    return fmt_value, False


class YandexProvider(Provider):
    """Yandex SpeechKit STT v3 provider."""

    name = "yandex"

    def __init__(self, api_key: str, folder_id: str, default_model: str = "general"):
        if not api_key:
            raise ProviderNotConfiguredError("Yandex API key is empty")
        if not folder_id:
            raise ProviderNotConfiguredError("Yandex folder id is empty")
        # Lazy import — the SDK pulls heavy gRPC deps.
        from yandex_ai_studio_sdk import AIStudio

        self._sdk = AIStudio(folder_id=folder_id, auth=api_key)
        self._default_model = default_model

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        # Build the SpeechKit model descriptor. We have to peek inside the SDK
        # for the AudioFormat enum value, so we import lazily.
        from yandex_ai_studio_sdk._speechkit.enums import (
            AudioFormat as YandexAudioFormat,
        )

        fmt_value, _ = _pick_yandex_audio_format(request.audio)
        sdk_audio_format = getattr(YandexAudioFormat, fmt_value)

        model_tag = yandex_model_tag(request.model, self._default_model)
        language = _coerce_language(request.language)

        stt = self._sdk.speechkit.speech_to_text(
            audio_format=sdk_audio_format,
            model=model_tag,
            language_codes=language,
        )

        def _run() -> TranscriptionResult:
            try:
                result = stt.run(request.audio.data)
            except Exception as exc:
                logger.exception("Yandex transcription failed")
                raise ProviderError(f"Yandex transcription failed: {exc}") from exc
            return _build_result(result, language)

        return await asyncio.to_thread(_run)


def _build_result(result: Any, fallback_language: str) -> TranscriptionResult:
    """Translate a Yandex ``SpeechToTextResult`` into our normalized shape."""
    text = getattr(result, "text", "") or ""
    language = getattr(result, "language_code", None) or fallback_language
    duration = getattr(result, "duration", None)

    segments: list[TranscriptionSegment] = []
    raw_segments = getattr(result, "segments", None) or []
    for idx, seg in enumerate(raw_segments):
        seg_text = getattr(seg, "text", "") or ""
        seg_start = getattr(seg, "start_time_ms", None)
        seg_end = getattr(seg, "end_time_ms", None)
        words_raw = getattr(seg, "words", None) or []
        words: list[dict] = []
        for w in words_raw:
            words.append(
                {
                    "word": getattr(w, "text", "") or "",
                    "start": (getattr(w, "start_time_ms", 0) or 0) / 1000.0,
                    "end": (getattr(w, "end_time_ms", 0) or 0) / 1000.0,
                }
            )
        segments.append(
            TranscriptionSegment(
                id=idx,
                start=(seg_start or 0) / 1000.0,
                end=(seg_end or 0) / 1000.0,
                text=seg_text,
                words=words,
            )
        )

    return TranscriptionResult(
        text=text,
        language=language,
        duration=float(duration) if duration is not None else None,
        segments=segments,
    )
