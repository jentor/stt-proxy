"""Provider abstractions and concrete implementations for STT backends."""

from .base import (
    Provider,
    ProviderError,
    ProviderNotConfiguredError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
    detect_routing,
    yandex_model_tag,
)
from .salute import SaluteProvider
from .yandex import YandexProvider

__all__ = [
    "Provider",
    "ProviderError",
    "ProviderNotConfiguredError",
    "TranscriptionRequest",
    "TranscriptionResult",
    "TranscriptionSegment",
    "detect_routing",
    "yandex_model_tag",
    "SaluteProvider",
    "YandexProvider",
]
