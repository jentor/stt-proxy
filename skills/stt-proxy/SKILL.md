---
name: stt-proxy
description: Transcribe local audio files with the stt-proxy CLI through Yandex SpeechKit, list accepted model ids, select JSON/text/verbose JSON/SRT/VTT output, and diagnose configuration or audio-format failures. Use when the user asks to transcribe speech or audio, save a transcription, inspect STT models, or operate the stt-proxy command.
---

# STT Proxy

Use the installed `stt-proxy` CLI. It reads shell `STT_PROXY_*` variables and
`~/.config/stt-proxy/config.toml`; direct transcription does not require the
daemon to be running.

## Transcribe a file

1. Resolve the exact input path and verify that it is a non-empty regular file.
2. Honor an explicitly requested Yandex model. Otherwise use `yandex/general`.
3. Use JSON unless the user requests another supported format.
4. Run the direct command; do not start the daemon:

```bash
stt-proxy transcribe --model yandex/general --language ru /absolute/path/audio.ogg
```

The CLI detects the source container from its bytes and falls back to its name
and MIME type. Do not add a format flag or pre-convert supported input. Let the
CLI use ffmpeg when conversion is required.

Yandex transcription enables API v3 text normalization with literature mode,
which adds sentence capitalization and punctuation. Do not post-process a raw
Yandex transcript merely to add guessed punctuation; rerun it with the current
tool version instead.

## Save output safely

Use the extension matching `--response-format`:

- `json` or `verbose_json`: `.json`
- `text`: `.txt`
- `srt`: `.srt`
- `vtt`: `.vtt`

Prefer `--output` so the CLI writes a temporary file and atomically replaces
the destination only after transcription succeeds. This avoids shell
redirection truncating an existing transcription or leaving an empty result
after failure. Do not overwrite an existing destination unless the user
explicitly permits it.

Example final invocation:

```bash
stt-proxy transcribe \
  --model yandex/general \
  --response-format json \
  --output /absolute/path/transcription.json \
  /absolute/path/audio.wav
```

The CLI also accepts a literal trailing `> FILE` pair for argv-based runners
that do not invoke a shell. In an actual shell, ordinary `> FILE` redirection
continues to work. On success, verify that the output is non-empty and report
its absolute path. For JSON output, parse it before delivery to confirm it is
valid JSON.

## List and select models

Run one of:

```bash
stt-proxy models
stt-proxy models --json
```

Treat this as a curated catalogue, not an exhaustive upstream registry. A
non-empty `yandex/<model>` suffix is passed to Yandex, so a user-specified model
absent from the catalogue may still be valid. Preserve legacy ids only when the
user supplies them; prefer slash ids in new commands.

## Output formats

Pass `--response-format` only when needed:

- `json` returns `{"text": "..."}` and is the default.
- `verbose_json` adds language, duration, segments, and available timings.
- `text` returns plain text.
- `srt` and `vtt` return subtitles.

Pass `--language` for a user-specified ISO-639-1 or BCP-47 code. Pass
`--prompt` only as a vocabulary hint; do not present it as guaranteed text.

## Diagnose failures

- Run `stt-proxy config` to inspect Yandex configuration without exposing full
  secrets.
- If no provider is enabled, explain the required settings. Never print keys
  and never modify `.env` to make a command work.
- For Yandex, require both `STT_PROXY_YANDEX_API_KEY` and
  `STT_PROXY_YANDEX_FOLDER_ID`.
- If conversion fails, check `ffmpeg` availability and report the original
  stderr concisely.
- If the upstream rejects a custom model, show the provider error and suggest a
  model returned by `stt-proxy models`; do not silently retry with a different
  model.
- Keep diagnostics on stderr. Treat stdout as transcription data only.
