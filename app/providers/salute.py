"""Sber SaluteSpeech provider using the asynchronous REST workflow."""

from __future__ import annotations

import io
import logging
from typing import Any

from .base import (
    ModelInfo,
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
    salute_model_tag,
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

    @classmethod
    def list_models(cls) -> list[ModelInfo]:
        """Advertise the model names documented by the asynchronous REST API."""
        return [
            ModelInfo(id="salutespeech/general", owned_by="salutespeech"),
            ModelInfo(id="salutespeech/callcenter", owned_by="salutespeech"),
        ]

    def __init__(self, credentials: str):
        if not credentials:
            raise ProviderNotConfiguredError("SaluteSpeech credentials are empty")
        # Import lazily so the package is optional at module-import time.
        from salute_speech.speech_recognition import SaluteSpeechClient

        self._client = SaluteSpeechClient(client_credentials=credentials)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        import asyncio

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

        model = salute_model_tag(request.model)

        try:
            response = await asyncio.to_thread(
                self._transcribe_sync,
                file_obj,
                language,
                model,
                config,
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

    def _transcribe_sync(
        self,
        file_obj: io.BytesIO,
        language: str,
        model: str,
        config: Any,
    ) -> Any:
        """Run Salute's upload/task/poll/download sequence with a model field.

        The public high-level SDK performs the same asynchronous REST sequence,
        but currently omits ``options.model``. This small adapter keeps using
        its authentication, certificate handling, validation and response
        types while adding the missing upstream parameter.
        """
        from salute_speech.speech_recognition import (
            TaskPoller,
            TranscriptionResponse,
            TranscriptionSegment as SDKTranscriptionSegment,
            _convert_to_whisper,
        )
        from salute_speech.utils.audio import AudioValidator
        from salute_speech.utils.russian_certs import russian_secure_post

        audio_encoding, sample_rate, channels_count = (
            AudioValidator.detect_and_validate(file_obj)
        )
        sr = self._client.sr

        upload_headers = sr._get_headers(raw=True)  # noqa: SLF001
        upload_headers["Content-Type"] = _salute_content_type(
            audio_encoding, sample_rate
        )
        file_obj.seek(0)
        upload_response = russian_secure_post(
            sr.base_url + "data:upload",
            headers=upload_headers,
            data=file_obj,
        )
        upload_json = sr.response_parser.parse_response(upload_response)
        request_file_id = sr.response_parser.extract_result(
            upload_json, ["request_file_id"]
        )["request_file_id"]

        options = {
            "model": model,
            "language": language,
            "audio_encoding": audio_encoding,
            "sample_rate": sample_rate,
            "channels_count": channels_count,
            **config.to_dict(),
        }
        task_response = russian_secure_post(
            sr.base_url + "speech:async_recognize",
            headers=sr._get_headers(),  # noqa: SLF001
            json={"options": options, "request_file_id": request_file_id},
        )
        task_json = sr.response_parser.parse_response(task_response)
        task_id = sr.response_parser.extract_result(task_json, ["id"])["id"]

        response_file_id = TaskPoller(sr, poll_interval=1.0).poll_for_result(task_id)
        raw_result = sr.download_result(response_file_id)
        text, raw_segments, lang_code, duration = _convert_to_whisper(
            raw_result, language=language
        )
        segments = [
            SDKTranscriptionSegment(
                id=segment.id,
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
            for segment in raw_segments
        ]
        return TranscriptionResponse(
            duration=duration,
            language=lang_code,
            text=text,
            segments=segments,
            status="DONE",
            task_id=task_id,
        )


def _salute_content_type(audio_encoding: str, sample_rate: int) -> str:
    """Return the documented upload Content-Type for detected audio."""
    if audio_encoding == "MP3":
        return "audio/mpeg"
    if audio_encoding == "OPUS":
        return "audio/ogg;codecs=opus"
    if audio_encoding == "FLAC":
        return "audio/flac"
    if audio_encoding == "ALAW":
        return f"audio/pcma;rate={sample_rate}"
    if audio_encoding == "MULAW":
        return f"audio/pcmu;rate={sample_rate}"
    return f"audio/x-pcm;bit=16;rate={sample_rate}"
