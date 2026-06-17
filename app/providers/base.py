"""Abstract base class for STT providers plus shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from ..audio import NormalizedAudio


class ProviderError(RuntimeError):
    """Base class for provider-level errors that should surface as HTTP responses."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider was selected but has no credentials configured."""


@dataclass(slots=True)
class TranscriptionSegment:
    """OpenAI-style transcription segment with optional word timings."""

    id: int
    start: float
    end: float
    text: str
    # Optional word-level timings (OpenAI verbose_json format).
    words: list[dict] = field(default_factory=list)
    # Provider-specific confidence / quality metrics.
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None


@dataclass(slots=True)
class TranscriptionResult:
    """Normalized transcription result, independent of the backend."""

    text: str
    language: str | None = None
    duration: float | None = None
    segments: list[TranscriptionSegment] = field(default_factory=list)


@dataclass(slots=True)
class TranscriptionRequest:
    """Per-request payload passed to a provider."""

    audio: NormalizedAudio
    model: str
    language: str | None = None
    prompt: str | None = None
    temperature: float | None = None
    response_format: str = "json"
    timestamp_granularities: Sequence[str] = ()


class Provider(ABC):
    """Abstract STT provider."""

    name: str = "abstract"

    @abstractmethod
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Run a transcription and return a normalized result."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


YANDEX_PREFIXES: tuple[str, ...] = ("yandex-", "yandex_", "yc-", "speechkit-")
SALUTE_PREFIXES: tuple[str, ...] = ("salute-", "salute_", "sber-")


def detect_routing(model: str | None) -> str | None:
    """Return provider name implied by the request ``model`` field, or None.

    Routing is purely by prefix so we don't hardcode a model catalogue:
      * ``yandex-...`` / ``yc-...`` / ``speechkit-...`` -> ``yandex``
      * ``salute-...`` / ``sber-...``                  -> ``salute``
    """
    if not model:
        return None
    m = model.lower()
    for prefix in YANDEX_PREFIXES:
        if m.startswith(prefix):
            return "yandex"
    for prefix in SALUTE_PREFIXES:
        if m.startswith(prefix):
            return "salute"
    return None


def yandex_model_tag(model: str, default: str) -> str:
    """Extract the Yandex model tag from a routed model name.

    ``yandex-general`` -> ``general``, ``yandex-general:rc`` -> ``general:rc``.
    If the model doesn't carry a prefix and doesn't look like a Yandex tag,
    return the configured default.
    """
    if not model:
        return default
    m = model
    for prefix in YANDEX_PREFIXES:
        if m.lower().startswith(prefix):
            return m[len(prefix) :] or default
    # Already a tag form (e.g. "general", "general:rc")?
    if (
        model
        and model.replace("-", "").replace(":", "").replace("_", "").isalnum()
        and "_" not in model.replace("-", "_")
    ):
        return model
    return default
