"""Provider abstractions and concrete implementations for STT backends."""

from .base import (
    ModelInfo,
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
    "ModelInfo",
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
