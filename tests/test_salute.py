from types import SimpleNamespace

from app.providers.salute import SaluteProvider, _salute_content_type


def test_salute_upload_content_types() -> None:
    assert _salute_content_type("MP3", 44100) == "audio/mpeg"
    assert _salute_content_type("OPUS", 48000) == "audio/ogg;codecs=opus"
    assert _salute_content_type("PCM_S16LE", 16000) == "audio/x-pcm;bit=16;rate=16000"


def test_salute_async_request_contains_selected_model(monkeypatch) -> None:
    import salute_speech.speech_recognition as recognition
    import salute_speech.utils.audio as audio_utils
    import salute_speech.utils.russian_certs as certs

    requests: list[dict] = []

    class Parser:
        @staticmethod
        def parse_response(response):
            return response

        @staticmethod
        def extract_result(response, required_fields):
            return response["result"]

    class SR:
        base_url = "https://example.test/"
        response_parser = Parser()

        @staticmethod
        def _get_headers(raw=False):
            return {"Authorization": "Bearer token"}

        @staticmethod
        def download_result(response_file_id):
            return "[]"

    def fake_post(url, **kwargs):
        requests.append({"url": url, **kwargs})
        if url.endswith("data:upload"):
            return {"status": 200, "result": {"request_file_id": "file-id"}}
        return {"status": 200, "result": {"id": "task-id"}}

    monkeypatch.setattr(
        audio_utils.AudioValidator,
        "detect_and_validate",
        lambda file_obj: ("MP3", 44100, 2),
    )
    monkeypatch.setattr(certs, "russian_secure_post", fake_post)
    monkeypatch.setattr(
        recognition.TaskPoller,
        "poll_for_result",
        lambda self, task_id: "response-id",
    )
    monkeypatch.setattr(
        recognition,
        "_convert_to_whisper",
        lambda raw, language=None: ("hello", [], "en", 1.0),
    )

    provider = object.__new__(SaluteProvider)
    provider._client = SimpleNamespace(sr=SR())
    config = SimpleNamespace(to_dict=lambda: {"hypotheses_count": 1})

    response = provider._transcribe_sync(
        SimpleNamespace(seek=lambda offset: None), "en-US", "asdasd", config
    )

    assert response.text == "hello"
    assert requests[0]["headers"]["Content-Type"] == "audio/mpeg"
    assert requests[1]["json"]["options"]["model"] == "asdasd"
