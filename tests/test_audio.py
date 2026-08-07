from app.audio import AudioFormat, detect_format


def test_detect_format_prefers_file_signature_over_extension() -> None:
    wav = b"RIFF\x00\x00\x00\x00WAVEfmt "

    assert detect_format("wrong.mp3", "audio/mpeg", wav) is AudioFormat.WAV


def test_detect_common_file_signatures() -> None:
    assert detect_format(None, None, b"fLaCdata") is AudioFormat.FLAC
    assert detect_format(None, None, b"OggSdata") is AudioFormat.OGG
    assert detect_format(None, None, b"ID3data") is AudioFormat.MP3
    assert detect_format(None, None, b"\x1aE\xdf\xa3data") is AudioFormat.WEBM


def test_detect_format_falls_back_to_filename() -> None:
    assert detect_format("recording.m4a", None, b"unknown") is AudioFormat.M4A
