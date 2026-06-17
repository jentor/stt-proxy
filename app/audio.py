"""Audio format detection and normalization.

OpenAI's audio API accepts many container/codec combinations (flac, mp3, mp4,
mpeg, mpga, m4a, ogg, wav, webm). The SaluteSpeech SDK auto-detects the codec
from the bytes via pydub, but the Yandex SDK needs an explicit
``AudioFormat`` hint (MP3, WAV, OGG_OPUS, PCM16).

This module:
  * detects the input format from filename / content-type
  * passes the bytes through when they match a natively supported container
  * transcodes to 16 kHz mono WAV PCM16 with ffmpeg as a fallback for
    containers the providers cannot consume directly (mp4, webm, m4a, flac).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Final


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


class AudioFormat(str, Enum):
    """Containers we either pass through or transcode."""

    MP3 = "mp3"
    WAV = "wav"
    OGG = "ogg"
    M4A = "m4a"
    MP4 = "mp4"
    WEBM = "webm"
    FLAC = "flac"
    UNKNOWN = "unknown"


# Map file extension and content-type to AudioFormat.
_EXT_MAP: Final[dict[str, AudioFormat]] = {
    ".mp3": AudioFormat.MP3,
    ".wav": AudioFormat.WAV,
    ".wave": AudioFormat.WAV,
    ".ogg": AudioFormat.OGG,
    ".oga": AudioFormat.OGG,
    ".opus": AudioFormat.OGG,
    ".m4a": AudioFormat.M4A,
    ".mp4": AudioFormat.MP4,
    ".mpeg": AudioFormat.MP3,
    ".mpga": AudioFormat.MP3,
    ".webm": AudioFormat.WEBM,
    ".flac": AudioFormat.FLAC,
}

_MIME_MAP: Final[dict[str, AudioFormat]] = {
    "audio/mpeg": AudioFormat.MP3,
    "audio/mp3": AudioFormat.MP3,
    "audio/wav": AudioFormat.WAV,
    "audio/x-wav": AudioFormat.WAV,
    "audio/wave": AudioFormat.WAV,
    "audio/ogg": AudioFormat.OGG,
    "audio/opus": AudioFormat.OGG,
    "audio/mp4": AudioFormat.M4A,
    "audio/x-m4a": AudioFormat.M4A,
    "video/mp4": AudioFormat.MP4,
    "audio/webm": AudioFormat.WEBM,
    "audio/flac": AudioFormat.FLAC,
    "audio/x-flac": AudioFormat.FLAC,
}


def detect_format(filename: str | None, content_type: str | None) -> AudioFormat:
    """Return the audio container based on filename and Content-Type header."""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in _EXT_MAP:
            return _EXT_MAP[ext]
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in _MIME_MAP:
            return _MIME_MAP[ct]
    return AudioFormat.UNKNOWN


# Containers both Salute and Yandex accept without transcoding.
NATIVE_FORMATS: Final[frozenset[AudioFormat]] = frozenset(
    {AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OGG}
)


# Map AudioFormat -> Yandex SDK AudioFormat enum value name.
YANDEX_AUDIO_FORMAT_MAP: Final[dict[AudioFormat, str]] = {
    AudioFormat.MP3: "MP3",
    AudioFormat.WAV: "WAV",
    AudioFormat.OGG: "OGG_OPUS",
}


# ---------------------------------------------------------------------------
# Normalized audio payload
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NormalizedAudio:
    """Audio bytes ready to feed into a provider, plus the detected format."""

    data: bytes
    format: AudioFormat
    original_filename: str | None
    original_content_type: str | None
    # If we transcoded, this is the ffmpeg target spec we used (e.g. "wav-16k-mono").
    transcoded: bool = False


# ---------------------------------------------------------------------------
# ffmpeg-based transcoding
# ---------------------------------------------------------------------------

_FFMPEG_BIN: Final[str | None] = shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return _FFMPEG_BIN is not None


def _transcode_sync(input_bytes: bytes, input_format: AudioFormat | None) -> bytes:
    """Run ffmpeg to convert arbitrary input to 16 kHz mono WAV PCM16."""
    if not _FFMPEG_BIN:
        raise RuntimeError(
            "ffmpeg is not available on PATH and the input audio is in a "
            f"non-native format ({input_format or 'unknown'}). Install ffmpeg "
            "or upload a WAV/MP3/OGG file."
        )

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as src:
        src.write(input_bytes)
        src_path = src.name
    out_path = src_path + ".wav"
    try:
        cmd = [
            _FFMPEG_BIN,
            "-y",  # overwrite
            "-loglevel",
            "error",
            "-i",
            src_path,
            "-ac",
            "1",  # mono
            "-ar",
            "16000",  # 16 kHz
            "-f",
            "wav",
            "-acodec",
            "pcm_s16le",
            out_path,
        ]
        if input_format and input_format is not AudioFormat.UNKNOWN:
            # Insert `-f <format>` before `-i` to help ffmpeg detect the container.
            cmd.insert(-3, "-f")
            cmd.insert(-3, input_format.value)
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg transcode failed: {stderr}")
        with open(out_path, "rb") as fh:
            return fh.read()
    finally:
        for path in (src_path, out_path):
            try:
                os.unlink(path)
            except OSError:
                pass


async def transcode_to_wav_pcm16(
    input_bytes: bytes, input_format: AudioFormat | None
) -> bytes:
    """Async wrapper around ffmpeg that runs the blocking subprocess in a thread."""
    return await asyncio.to_thread(_transcode_sync, input_bytes, input_format)


# ---------------------------------------------------------------------------
# Top-level normalization
# ---------------------------------------------------------------------------


async def normalize(
    data: bytes,
    filename: str | None,
    content_type: str | None,
    *,
    always_transcode: bool = False,
) -> NormalizedAudio:
    """Return audio bytes in a provider-friendly container.

    Strategy:
      * If ``always_transcode`` is True, send everything through ffmpeg.
      * Otherwise, if the detected format is in NATIVE_FORMATS, return the bytes
        untouched so each provider can use its preferred path.
      * Otherwise, transcode to 16 kHz mono WAV PCM16.
    """
    fmt = detect_format(filename, content_type)

    if always_transcode:
        wav = await transcode_to_wav_pcm16(
            data, fmt if fmt is not AudioFormat.UNKNOWN else None
        )
        return NormalizedAudio(
            data=wav,
            format=AudioFormat.WAV,
            original_filename=filename,
            original_content_type=content_type,
            transcoded=True,
        )

    if fmt in NATIVE_FORMATS:
        return NormalizedAudio(
            data=data,
            format=fmt,
            original_filename=filename,
            original_content_type=content_type,
            transcoded=False,
        )

    # Unsupported container (mp4, webm, m4a, flac, unknown).
    logger.info(
        "Transcoding input %s (format=%s) to WAV PCM16 16 kHz mono", filename, fmt.value
    )
    wav = await transcode_to_wav_pcm16(
        data, fmt if fmt is not AudioFormat.UNKNOWN else None
    )
    return NormalizedAudio(
        data=wav,
        format=AudioFormat.WAV,
        original_filename=filename,
        original_content_type=content_type,
        transcoded=True,
    )
