import asyncio
from types import SimpleNamespace

from app.audio import AudioFormat, NormalizedAudio
from app.providers.base import TranscriptionRequest
from app.providers.yandex import YandexProvider


def test_yandex_enables_literature_text_normalization() -> None:
    captured: dict = {}

    class FakeSTT:
        @staticmethod
        def run(data: bytes):
            return SimpleNamespace(text="Hello, world.")

    class FakeSpeechKit:
        @staticmethod
        def speech_to_text(**kwargs):
            captured.update(kwargs)
            return FakeSTT()

    provider = object.__new__(YandexProvider)
    provider._sdk = SimpleNamespace(speechkit=FakeSpeechKit())
    provider._default_model = "general"
    request = TranscriptionRequest(
        audio=NormalizedAudio(
            data=b"RIFF\x00\x00\x00\x00WAVE",
            format=AudioFormat.WAV,
            original_filename="audio.wav",
            original_content_type="audio/wav",
        ),
        model="yandex/general",
    )

    result = asyncio.run(provider.transcribe(request))

    assert result.text == "Hello, world."
    assert captured["text_normalization"].literature_text is True
