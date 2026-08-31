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
    ModelInfo,
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

    # Model tags accepted by AsyncRecognizer.RecognizeFile (SpeechKit v3):
    #   https://yandex.cloud/ru-kz/docs/speechkit/stt-v3/api-ref/grpc/AsyncRecognizer/recognizeFile
    # ``id`` includes the routing prefix so it matches what users pass
    # in the request ``model`` field. ``yandex_model_tag()`` strips the
    # prefix before calling the SDK.
    _MODEL_TAGS: tuple[str, ...] = (
        "general",
        "general:rc",
        "general:deprecated",
        "deferred-general",
        "deferred-general:rc",
        "deferred-general:deprecated",
    )

    def __init__(self, api_key: str, folder_id: str, default_model: str = "general"):
        if not api_key:
            raise ProviderNotConfiguredError("Yandex API key is empty")
        if not folder_id:
            raise ProviderNotConfiguredError("Yandex folder id is empty")
        # Lazy import — the SDK pulls heavy gRPC deps.
        from yandex_ai_studio_sdk import AIStudio

        self._sdk = AIStudio(folder_id=folder_id, auth=api_key)
        self._default_model = default_model

    @classmethod
    def list_models(cls) -> list[ModelInfo]:
        """Advertise every Yandex model tag we know how to route to."""
        return [
            ModelInfo(id=f"yandex/{tag}", owned_by="yandex") for tag in cls._MODEL_TAGS
        ]

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        # Build the SpeechKit model descriptor. We have to peek inside the SDK
        # for the AudioFormat enum value, so we import lazily.
        from yandex_ai_studio_sdk._speechkit.enums import (
            AudioFormat as YandexAudioFormat,
        )
        from yandex_ai_studio_sdk._speechkit.speech_to_text.structures import (
            TextNormalization,
        )

        fmt_value, _ = _pick_yandex_audio_format(request.audio)
        sdk_audio_format = getattr(YandexAudioFormat, fmt_value)

        model_tag = yandex_model_tag(request.model, self._default_model)
        language = _coerce_language(request.language)

        stt = self._sdk.speechkit.speech_to_text(
            audio_format=sdk_audio_format,
            model=model_tag,
            language_codes=language,
            # Normalization alone formats numbers/dates. Literature mode adds
            # sentence capitalization and punctuation to the refined result.
            text_normalization=TextNormalization(literature_text=True),
        )

        def _run() -> TranscriptionResult:
            try:
                if request.deferred:
                    operation = stt.run_deferred(request.audio.data)
                    result = operation.wait(poll_interval=1.0)
                else:
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

    segments = _build_v3_segments(result)
    if not segments:
        # Compatibility with older SDK result objects which exposed a flat
        # ``segments`` collection instead of channels and utterances.
        segments = _build_legacy_segments(result)

    if duration is None and segments:
        duration = max(segment.end for segment in segments)

    return TranscriptionResult(
        text=text,
        language=language,
        duration=float(duration) if duration is not None else None,
        segments=segments,
    )


def _build_v3_segments(result: Any) -> list[TranscriptionSegment]:
    """Extract phrase and word timings from the current SpeechKit v3 SDK."""
    channels = getattr(result, "channels", None) or {}
    if isinstance(channels, dict):
        channel_values = (channels[key] for key in sorted(channels))
    else:
        channel_values = iter(channels)

    segments: list[TranscriptionSegment] = []
    for channel in channel_values:
        for utterance in getattr(channel, "utterances", None) or ():
            # Normalized refinements contain literature-mode capitalization and
            # punctuation. Fall back to raw finals when no refinement arrived.
            parts = getattr(utterance, "final_refinements", None) or ()
            if not parts:
                parts = getattr(utterance, "finals", None) or ()

            if parts:
                for part in parts:
                    segments.append(_segment_from_v3_part(len(segments), part))
                continue

            # Defensive fallback for SDK-compatible test doubles or future SDK
            # versions which expose only the aggregate utterance.
            segments.append(_segment_from_v3_part(len(segments), utterance))
    return segments


def _segment_from_v3_part(segment_id: int, part: Any) -> TranscriptionSegment:
    timespan = getattr(part, "timespan", None)
    start = (getattr(timespan, "start_time_ms", 0) or 0) / 1000.0
    end = (getattr(timespan, "end_time_ms", 0) or 0) / 1000.0
    words = [_word_with_timespan(word) for word in getattr(part, "words", None) or ()]
    return TranscriptionSegment(
        id=segment_id,
        start=start,
        end=end,
        text=getattr(part, "text", "") or "",
        words=words,
    )


def _word_with_timespan(word: Any) -> dict[str, Any]:
    timespan = getattr(word, "timespan", None)
    return {
        "word": getattr(word, "text", "") or "",
        "start": (getattr(timespan, "start_time_ms", 0) or 0) / 1000.0,
        "end": (getattr(timespan, "end_time_ms", 0) or 0) / 1000.0,
    }


def _build_legacy_segments(result: Any) -> list[TranscriptionSegment]:
    segments: list[TranscriptionSegment] = []
    for idx, segment in enumerate(getattr(result, "segments", None) or ()):
        words = []
        for word in getattr(segment, "words", None) or ():
            words.append(
                {
                    "word": getattr(word, "text", "") or "",
                    "start": (getattr(word, "start_time_ms", 0) or 0) / 1000.0,
                    "end": (getattr(word, "end_time_ms", 0) or 0) / 1000.0,
                }
            )
        segments.append(
            TranscriptionSegment(
                id=idx,
                start=(getattr(segment, "start_time_ms", 0) or 0) / 1000.0,
                end=(getattr(segment, "end_time_ms", 0) or 0) / 1000.0,
                text=getattr(segment, "text", "") or "",
                words=words,
            )
        )
    return segments
