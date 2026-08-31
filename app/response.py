"""Format transcription results the way OpenAI's Audio API does it.

OpenAI's `/v1/audio/transcriptions` accepts a ``response_format`` parameter with
these values:

  * ``json``         -> ``{"text": "..."}``
  * ``text``         -> raw string
  * ``verbose_json`` -> dict with language / duration / segments
  * ``srt``          -> SRT subtitle block
  * ``vtt``          -> WebVTT block
  * ``diarized_json``-> not supported by either backend; raise 400

This module renders our normalized ``TranscriptionResult`` into any of those
shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from .providers import TranscriptionResult


ResponseFormat = Literal["json", "text", "verbose_json", "srt", "vtt", "diarized_json"]


SUPPORTED_FORMATS: tuple[ResponseFormat, ...] = (
    "json",
    "text",
    "verbose_json",
    "srt",
    "vtt",
)


class UnsupportedResponseFormat(ValueError):
    """Raised when the client requests an unsupported response_format."""


def normalize_format(value: str | None) -> ResponseFormat:
    """Validate and normalize the response_format value; default is ``json``."""
    if not value:
        return "json"
    value = value.strip().lower()
    if value == "diarized_json":
        raise UnsupportedResponseFormat(
            "diarized_json is not supported: Yandex does not return speaker labels"
        )
    if value not in SUPPORTED_FORMATS:
        raise UnsupportedResponseFormat(
            f"Unsupported response_format {value!r}; expected one of {', '.join(SUPPORTED_FORMATS)}"
        )
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_json(result: TranscriptionResult) -> dict[str, Any]:
    return {"text": result.text}


def render_text(result: TranscriptionResult) -> str:
    return result.text


def render_verbose_json(result: TranscriptionResult) -> dict[str, Any]:
    segments_payload: list[dict[str, Any]] = []
    for seg in result.segments:
        item: dict[str, Any] = {
            "id": seg.id,
            "seek": 0,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
            "tokens": [],
            "temperature": seg.temperature if seg.temperature is not None else 0.0,
            "avg_logprob": seg.avg_logprob if seg.avg_logprob is not None else -0.0,
            "compression_ratio": seg.compression_ratio
            if seg.compression_ratio is not None
            else 0.0,
            "no_speech_prob": seg.no_speech_prob
            if seg.no_speech_prob is not None
            else 0.0,
        }
        if seg.words:
            item["words"] = seg.words
        segments_payload.append(item)
    payload: dict[str, Any] = {
        "task": "transcribe",
        "language": result.language or "unknown",
        "duration": result.duration if result.duration is not None else 0.0,
        "text": result.text,
        "segments": segments_payload,
    }
    return payload


def _format_timestamp(seconds: float, *, sep: str) -> str:
    """Format a timestamp the way SRT/VTT want it."""
    if seconds < 0:
        seconds = 0.0
    millis_total = int(round(seconds * 1000))
    hours, remainder = divmod(millis_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def render_srt(result: TranscriptionResult) -> str:
    lines: list[str] = []
    if not result.segments:
        # Fallback: dump the whole transcript as a single block.
        end = result.duration if result.duration is not None else 5.0
        lines.append("1")
        lines.append(f"00:00:00,000 --> {_format_timestamp(end, sep=',')}")
        lines.append(result.text)
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for seg in result.segments:
        lines.append(str(seg.id + 1))
        lines.append(
            f"{_format_timestamp(seg.start, sep=',')} --> {_format_timestamp(seg.end, sep=',')}"
        )
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_vtt(result: TranscriptionResult) -> str:
    lines: list[str] = ["WEBVTT", ""]
    if not result.segments:
        end = result.duration if result.duration is not None else 5.0
        lines.append(f"00:00:00.000 --> {_format_timestamp(end, sep='.')}")
        lines.append(result.text)
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for seg in result.segments:
        lines.append(
            f"{_format_timestamp(seg.start, sep='.')} --> {_format_timestamp(seg.end, sep='.')}"
        )
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_RENDERERS = {
    "json": render_json,
    "text": render_text,
    "verbose_json": render_verbose_json,
    "srt": render_srt,
    "vtt": render_vtt,
}


def render(result: TranscriptionResult, fmt: str) -> Any:
    """Dispatch to the right renderer.

    Returns either a dict (for JSON-based formats) or a string (for text-based
    formats). The caller picks the matching ``Content-Type``.
    """
    fmt = normalize_format(fmt)
    renderer = _RENDERERS[fmt]
    return renderer(result)
