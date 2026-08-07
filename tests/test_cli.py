import json
from argparse import Namespace

from app import cli
from app.cli import main


def test_models_lists_ids_without_credentials(capsys) -> None:
    assert main(["models"]) == 0

    output = capsys.readouterr().out.splitlines()
    assert "yandex/general" in output
    assert "salutespeech/general" in output


def test_models_json(capsys) -> None:
    assert main(["models", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in output} >= {
        "yandex/general",
        "salutespeech/general",
    }


def test_transcribe_accepts_literal_redirect(monkeypatch, tmp_path, capsys) -> None:
    async def fake_transcribe(args: Namespace) -> object:
        return {"text": "hello"}

    monkeypatch.setattr(cli, "_transcribe_file", fake_transcribe)
    output = tmp_path / "audio.json"

    assert (
        main(
            [
                "transcribe",
                "--model",
                "salutespeech/general",
                "audio.m4a",
                ">",
                str(output),
            ]
        )
        == 0
    )

    assert json.loads(output.read_text(encoding="utf-8")) == {"text": "hello"}
    assert capsys.readouterr().out == ""


def test_transcribe_output_option(monkeypatch, tmp_path) -> None:
    async def fake_transcribe(args: Namespace) -> object:
        return "hello"

    monkeypatch.setattr(cli, "_transcribe_file", fake_transcribe)
    output = tmp_path / "audio.txt"

    assert (
        main(
            [
                "transcribe",
                "--model",
                "yandex/general",
                "--response-format",
                "text",
                "--output",
                str(output),
                "audio.wav",
            ]
        )
        == 0
    )

    assert output.read_text(encoding="utf-8") == "hello\n"


def test_transcribe_rejects_two_output_spellings(monkeypatch, tmp_path, capsys) -> None:
    async def must_not_run(args: Namespace) -> object:
        raise AssertionError("transcription must not start")

    monkeypatch.setattr(cli, "_transcribe_file", must_not_run)

    assert (
        main(
            [
                "transcribe",
                "--model",
                "yandex/general",
                "--output",
                str(tmp_path / "one.json"),
                "audio.wav",
                ">",
                str(tmp_path / "two.json"),
            ]
        )
        == 1
    )
    assert "either --output" in capsys.readouterr().err
