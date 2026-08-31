import asyncio
from types import SimpleNamespace

from app.audio import AudioFormat, NormalizedAudio
from app.providers.base import TranscriptionRequest
from app.providers.yandex import YandexProvider, _build_result
from app.response import render_srt, render_verbose_json, render_vtt


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


def _timespan(start: int, end: int) -> SimpleNamespace:
    return SimpleNamespace(start_time_ms=start, end_time_ms=end)


def _part(text: str, start: int, end: int, *words: tuple[str, int, int]):
    return SimpleNamespace(
        text=text,
        timespan=_timespan(start, end),
        words=tuple(
            SimpleNamespace(text=word, timespan=_timespan(word_start, word_end))
            for word, word_start, word_end in words
        ),
    )


def test_yandex_v3_timings_are_exposed_in_verbose_json_and_subtitles() -> None:
    first = _part(
        "Привет, мир!",
        120,
        1540,
        ("Привет", 120, 780),
        ("мир", 900, 1450),
    )
    second = _part("Как дела?", 1800, 2760, ("Как", 1800, 2100), ("дела", 2160, 2700))
    result = SimpleNamespace(
        text="Привет, мир! Как дела?",
        channels={
            "0": SimpleNamespace(
                utterances=(
                    SimpleNamespace(final_refinements=(first,), finals=()),
                    SimpleNamespace(final_refinements=(second,), finals=()),
                )
            )
        },
    )

    transcription = _build_result(result, "ru-RU")
    verbose = render_verbose_json(transcription)

    assert transcription.duration == 2.76
    assert verbose["segments"] == [
        {
            "id": 0,
            "seek": 0,
            "start": 0.12,
            "end": 1.54,
            "text": "Привет, мир!",
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": -0.0,
            "compression_ratio": 0.0,
            "no_speech_prob": 0.0,
            "words": [
                {"word": "Привет", "start": 0.12, "end": 0.78},
                {"word": "мир", "start": 0.9, "end": 1.45},
            ],
        },
        {
            "id": 1,
            "seek": 0,
            "start": 1.8,
            "end": 2.76,
            "text": "Как дела?",
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": -0.0,
            "compression_ratio": 0.0,
            "no_speech_prob": 0.0,
            "words": [
                {"word": "Как", "start": 1.8, "end": 2.1},
                {"word": "дела", "start": 2.16, "end": 2.7},
            ],
        },
    ]
    assert "00:00:00,120 --> 00:00:01,540" in render_srt(transcription)
    assert "00:00:01.800 --> 00:00:02.760" in render_vtt(transcription)


def test_yandex_v3_uses_raw_final_when_refinement_is_missing() -> None:
    final = _part("raw text", 500, 1250, ("raw", 500, 800), ("text", 850, 1250))
    result = SimpleNamespace(
        text="raw text",
        channels={
            "0": SimpleNamespace(
                utterances=(SimpleNamespace(final_refinements=(), finals=(final,)),)
            )
        },
    )

    transcription = _build_result(result, "en-US")

    assert transcription.segments[0].text == "raw text"
    assert transcription.segments[0].start == 0.5
    assert transcription.segments[0].words[1] == {
        "word": "text",
        "start": 0.85,
        "end": 1.25,
    }
