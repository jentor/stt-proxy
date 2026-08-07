# stt-proxy

An OpenAI-Audio-API compatible proxy for **[Yandex SpeechKit](https://yandex.cloud/ru-kz/docs/speechkit/)** and **[Sber SaluteSpeech](https://developers.sber.ru/docs/ru/salutespeech/api/overview)**.

It exposes the same wire format as OpenAI's `/v1/audio/transcriptions` endpoint on the front side, so any client built for OpenAI Whisper / gpt-4o-transcribe can talk to Yandex or SaluteSpeech instead — without code changes.

```
┌──────────────────┐      ┌─────────────────┐      ┌────────────────────┐
│ OpenAI client    │ ───► │  stt-proxy      │ ───► │ Yandex SpeechKit   │
│ (any SDK / curl) │      │  (FastAPI)      │      │  or SaluteSpeech   │
└──────────────────┘      └─────────────────┘      └────────────────────┘
```

The proxy is intentionally small: it parses the OpenAI multipart request, picks a provider based on the `model` field, transcodes unsupported audio containers via `ffmpeg`, calls the appropriate SDK, and renders the response in the OpenAI shape (`json`, `text`, `verbose_json`, `srt`, `vtt`).

---

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Running the server](#running-the-server)
- [API reference](#api-reference)
- [Provider routing](#provider-routing)
- [Supported audio formats](#supported-audio-formats)
- [Supported response formats](#supported-response-formats)
- [Project layout](#project-layout)
- [Development workflow](#development-workflow)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

- ✅ Drop-in replacement for `POST /v1/audio/transcriptions`
- ✅ Multipart file upload + all OpenAI form fields (`model`, `language`, `prompt`, `response_format`, `temperature`, `timestamp_granularities`)
- ✅ Response formats: `json`, `text`, `verbose_json`, `srt`, `vtt`
- ✅ Yandex SpeechKit STT v3 via [`yandex-ai-studio-sdk`](https://github.com/yandex-cloud/yandex-ai-studio-sdk)
- ✅ Yandex text normalization with sentence capitalization and punctuation
- ✅ SaluteSpeech via [`salute-speech`](https://github.com/mmua/salute_speech)
- ✅ Automatic audio normalization via `ffmpeg` (mp4/webm/m4a/flac → WAV PCM16 16 kHz mono)
- ✅ Direct CLI transcription without a running daemon
- ✅ Provider selection by model id (`yandex/general`, `salutespeech/general`) or by `STT_PROXY_DEFAULT_PROVIDER` for HTTP requests
- ✅ Environment-driven configuration via `.env` / shell
- ✅ Refuses to start when no provider credentials are configured
- ✅ uv-managed Python project, runs on Python 3.12
- ✅ Taskfile-based dev workflow (auto-reload, lint, format, doctor)

---

## Quick start

```bash
# 1. Install the CLI tool directly from the Git repo (registers
#    `stt-proxy` on PATH — no clone needed)
uv tool install git+ssh://git@gitlab.sberautotech.ru:7999/vagayduk/stt-proxy.git
# To pin a version, append @vX.Y.Z to the URL.

# 2. Create the config file and fill in at least one provider's keys
stt-proxy config init
$EDITOR ~/.config/stt-proxy/config.toml

# 3. Inspect models and transcribe directly (no daemon needed)
stt-proxy models
stt-proxy transcribe --model salutespeech/general ./hello.wav --output transcription.json

# 4. Optionally start the OpenAI-compatible HTTP daemon
stt-proxy start
# => stt-proxy started (pid=...)
# => logs: ... (run `stt-proxy logs` for the exact path)

# 5. Send a test request
curl -sS -X POST http://127.0.0.1:8000/v1/audio/transcriptions \
    -F model=yandex/general \
    -F file=@./hello.wav \
    -F response_format=json

# Other useful commands: `stt-proxy stop`, `stt-proxy logs [-f]`, `stt-proxy config`
```

If neither provider is configured, the process exits at startup with exit code `2` and a clear error message on stderr.

---

## Configuration

All configuration is read from environment variables (and optionally a `.env` file in the working directory). **Every variable is namespaced under `STT_PROXY_`** — there's no need to remember per-provider prefixes. The full set lives in `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `STT_PROXY_HOST` | `127.0.0.1` | HTTP bind address (default: localhost only; set to `0.0.0.0` to accept from other hosts) |
| `STT_PROXY_PORT` | `8000` | HTTP bind port |
| `STT_PROXY_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `STT_PROXY_WORKERS` | `1` | uvicorn workers (keep at 1 — provider SDKs may not be fork-safe) |
| `STT_PROXY_YANDEX_API_KEY` | _none_ | Yandex Cloud API key |
| `STT_PROXY_YANDEX_FOLDER_ID` | _none_ | Yandex Cloud folder ID (must be set together with the API key) |
| `STT_PROXY_YANDEX_MODEL` | `general` | Yandex STT model tag (`general`, `general:rc`, `deferred-general`, ...) |
| `STT_PROXY_SALUTESPEECH_KEY` | _none_ | SaluteSpeech auth key: base64 of `client_id:client_secret` (see [SaluteSpeech docs](https://developers.sber.ru/docs/ru/salutespeech/rest/post-token)) |
| `STT_PROXY_DEFAULT_PROVIDER` | _auto_ | `yandex` or `salute` — used when both providers are configured and the request `model` has no prefix |

### Provider enable rules

- **Yandex** is enabled only when **both** `STT_PROXY_YANDEX_API_KEY` and `STT_PROXY_YANDEX_FOLDER_ID` are present. Setting one without the other disables the provider with a warning.
- **SaluteSpeech** is enabled when `STT_PROXY_SALUTESPEECH_KEY` is non-empty.
- If neither provider is enabled, the process refuses to start (exit code `2`).
- When exactly one provider is enabled, it is used for every request — `model` is passed through unchanged.
- When both providers are enabled, `STT_PROXY_DEFAULT_PROVIDER` decides which one is used for unprefixed HTTP `model` values; otherwise use `yandex/...` or `salutespeech/...`.

Inspect the effective configuration with:

```bash
task dev:info
```

---

## Running the server

There are two ways to run stt-proxy:

1. **As a CLI tool / background daemon** (new): `task install-tool` registers a `stt-proxy` command with `start` / `stop` / `logs` subcommands. The daemon runs detached, writes rotating logs to your user log directory, and reads configuration **only from the shell environment** (no `.env` lookup). Best for "always on" / production-style use.
2. **In the foreground** (existing): `task dev:serve` / `task run` / `uv run uvicorn ...` run the server in your terminal, with full `.env` support and (for `task dev:serve`) auto-reload on file changes. Best for development.

### Installing as a CLI tool (`uv tool install`)

```bash
task install-tool      # runs: uv tool install -e .
```

This installs the project as a uv-managed tool and puts `stt-proxy` on your `PATH` (typically under `~/.local/bin`). The commands are then available globally, independent of the project's `.venv`:

```bash
# Launch as a detached background daemon. Env vars come from the shell —
# export STT_PROXY_* before running; .env is NOT consulted by the daemon.
export STT_PROXY_YANDEX_API_KEY=...
export STT_PROXY_YANDEX_FOLDER_ID=...
stt-proxy start
# => stt-proxy started (pid=12345)
#    logs: /Users/<you>/Library/Logs/stt-proxy/stt-proxy.log

stt-proxy logs                # print log/PID paths (no tailing)
stt-proxy logs -f             # tail -f the log file
stt-proxy stop                # SIGTERM, then SIGKILL after 10s

stt-proxy config               # show effective config (secrets masked)
stt-proxy config init          # create ~/.config/stt-proxy/config.toml

stt-proxy models               # list accepted provider/model ids
stt-proxy transcribe --model yandex/general ./speech.mp3 --output transcription.json
```

Note: `stt-proxy start` polls for the daemon's PID file for up to 30 seconds
before giving up — Python 3.14 cold starts (yandex SDK + grpc imports) can
be slow.

Log and PID file locations (managed by [`platformdirs`](https://pypi.org/project/platformdirs/)):

| Platform | log file                                    | PID file                                          |
|---|---|---|
| **macOS** | `~/Library/Logs/stt-proxy/stt-proxy.log`  | `$TMPDIR/stt-proxy/stt-proxy.pid` (under `/var/folders/...`) |
| **Linux** | `~/.cache/stt-proxy/stt-proxy.log`        | `/run/user/<uid>/stt-proxy/stt-proxy.pid`          |

Logs rotate at 10 MiB with 5 backups.

### Persisting configuration (optional)

`stt-proxy config init` creates `~/.config/stt-proxy/config.toml` with a
documented template and sets permissions to `0o600`:

```bash
stt-proxy config init                # create the file (refuses if it exists)
stt-proxy config init --force        # overwrite
$EDITOR ~/.config/stt-proxy/config.toml   # uncomment and fill in the keys you need
stt-proxy stop && stt-proxy start         # restart the daemon to pick up changes
```

`stt-proxy config` (no subcommand) prints the effective configuration the
daemon would use, with secrets masked (`***<last 4>`). In daemon mode the
file is read as a TOML source alongside shell env. Precedence (highest
first): shell env → config.toml → defaults. The file is optional — if it
doesn't exist, the daemon falls back to shell env only.

If the config file exists but is malformed, `stt-proxy start` and
`stt-proxy config` refuse to run with a clear error pointing at the file
and the TOML line/column — no more digging through the log file to find
the typo.

> **Env-var precedence for the daemon**: the daemon reads *only* the process environment (whatever you `export` in the shell before `stt-proxy start`), plus the optional TOML config file above. It deliberately ignores `.env`. If you want to use `.env` values, run `set -a; . ./.env; set +a` before `stt-proxy start`, or stick with `task run` / `task dev:serve` which DO load `.env`.

### Running in the foreground (development)

`STT_PROXY_*` settings are resolved in this order (highest priority first):

1. shell environment variables (e.g. `STT_PROXY_PORT=12000 task run`)
2. `.env` file in the working directory — auto-loaded by both:
   - the Taskfile, which sets `UV_ENV_FILE=.env` in each task's `env:` block so `uv run` reads it into the subprocess OS environment
   - pydantic-settings in `app/config.py`, which reads it via `env_file=".env"` as a safety net for bare `python -m app.main` invocations
3. built-in defaults (see the table above)

This means `STT_PROXY_PORT=9000 task run` always wins over whatever is in `.env`. And because uv reads `.env` into the subprocess env, the values are visible to `env | grep STT_PROXY_` for debugging.

```bash
STT_PROXY_PORT=9000 task run
# uvicorn binds 9000 (shell wins)

task run
# uvicorn binds to whatever STT_PROXY_PORT says in .env, or 8000 by default
```

```bash
# Development (auto-reload on file change)
task dev:serve

# Production-style single worker
task run

# Direct uvicorn (bypasses our run() entry point)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The `stt-proxy` console script is the CLI documented above. For ad-hoc foreground runs from the project venv without installing the tool:

```bash
uv run uvicorn app.main:app            # foreground, no reload
uv run python -m app.main              # same, through app.main.run()
```

### Health / inspection endpoints

```bash
# Which providers are enabled right now
curl -sS http://127.0.0.1:8000/v1/audio/providers
# => {"providers":[{"name":"yandex","enabled":true,"model":"general"},{"name":"salute","enabled":false,"model":null}],"default_provider":"yandex"}

# OpenAI-compatible model catalogue (also available at /models)
curl -sS http://127.0.0.1:8000/v1/models
# => {"object":"list","data":[
#       {"id":"yandex/general","object":"model","created":0,"owned_by":"yandex"},
#       {"id":"yandex/general:rc","object":"model","created":0,"owned_by":"yandex"},
#       ...
#       {"id":"salutespeech/general","object":"model","created":0,"owned_by":"salutespeech"},
#       {"id":"salutespeech/callcenter","object":"model","created":0,"owned_by":"salutespeech"}
#     ]}
```

The model list is aggregated from every enabled provider. Since neither Yandex nor Salute expose a model catalogue API, the entries are static — they reflect the routing prefixes the proxy supports, not what the providers happen to accept on a given day. Use the `id` values in the `model` field of transcription requests.

Interactive docs (Swagger UI) live at `http://127.0.0.1:8000/docs`.

---

## API reference

### `POST /v1/audio/transcriptions`

Wire-compatible with the OpenAI Audio API. The request is `multipart/form-data`:

| Field | Required | Description |
|---|---|---|
| `file` | yes | Audio file (binary) |
| `model` | yes | Model id — used for routing. See [Provider routing](#provider-routing) |
| `language` | no | ISO-639-1 (`ru`, `en`, ...) or BCP-47 (`ru-RU`, `en-US`, ...). Defaults to provider default. |
| `prompt` | no | Optional hint text. Forwarded as vocabulary hints to SaluteSpeech; ignored by Yandex (the SDK has no first-class prompt slot). |
| `response_format` | no | `json` (default), `text`, `verbose_json`, `srt`, `vtt`. `diarized_json` is rejected with 400. |
| `temperature` | no | Sampling temperature. Accepted but not used by either provider. |
| `timestamp_granularities` | no | `word` and/or `segment`. Honored when present in `verbose_json` results. |

#### `response_format=json` (default)

```json
{ "text": "Hello, how are you?" }
```

#### `response_format=text`

```
Hello, how are you?
```

#### `response_format=verbose_json`

```json
{
  "task": "transcribe",
  "language": "ru-RU",
  "duration": 4.21,
  "text": "Привет, как дела?",
  "segments": [
    {
      "id": 0,
      "seek": 0,
      "start": 0.0,
      "end": 2.1,
      "text": " Привет,",
      "tokens": [],
      "temperature": 0.0,
      "avg_logprob": 0.0,
      "compression_ratio": 0.0,
      "no_speech_prob": 0.0,
      "words": [{"word": "Привет", "start": 0.0, "end": 0.6}]
    }
  ]
}
```

#### `response_format=srt`

```
1
00:00:00,000 --> 00:00:02,100
Привет,

2
00:00:02,100 --> 00:00:04,210
как дела?
```

#### Errors

Errors follow the OpenAI envelope:

```json
{
  "detail": {
    "error": {
      "message": "...",
      "type": "invalid_request_error | upstream_error | server_error"
    }
  }
}
```

| HTTP | When |
|---|---|
| `400` | Bad `response_format`, ambiguous `model` (both providers configured, no default, no prefix), unknown routed provider, empty upload |
| `413` | File larger than 25 MB |
| `422` | FastAPI form validation (missing required fields, bad types) |
| `500` | Misconfiguration (provider credentials changed between load and call) |
| `502` | Upstream provider error (network, auth, quota, ...) |

---

## Provider routing

The proxy decides which backend to call based on the request's `model` field:

| `model` value | Routing |
|---|---|
| `yandex/*` | Yandex SpeechKit; the suffix becomes the upstream model tag (for example `yandex/general:rc` → `general:rc`) |
| `salutespeech/*` | SaluteSpeech; the suffix is sent as `options.model` (for example `salutespeech/callcenter`) |
| Legacy `yandex-*`, `yc-*`, `speechkit-*`, `salute-*`, `sber-*` | Kept for compatibility |
| Anything else (e.g. `whisper-1`) | `STT_PROXY_DEFAULT_PROVIDER` if set, else the only configured provider (if there is exactly one), else 400 |

Examples:

```bash
# Yandex with explicit model tag
curl -X POST .../v1/audio/transcriptions \
    -F model=yandex/general:rc \
    -F file=@speech.wav

# SaluteSpeech
curl -X POST .../v1/audio/transcriptions \
    -F model=salutespeech/general \
    -F file=@speech.wav

# Use the default provider (configured via STT_PROXY_DEFAULT_PROVIDER or auto-detected)
curl -X POST .../v1/audio/transcriptions \
    -F model=whisper-1 \
    -F file=@speech.wav
```

Run `stt-proxy models` (or `stt-proxy models --json`) to inspect the curated catalogue. Provider APIs may accept additional model names; any non-empty suffix is passed through, so `salutespeech/<new-model>` does not require a tool release.

---

## Supported audio formats

OpenAI accepts: `flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm`.

Both providers natively consume MP3, WAV and OGG/OPUS. When the upload is in another container (`mp4`, `webm`, `m4a`, `flac`, or unknown), the proxy transcodes it to **16 kHz mono WAV PCM16** with `ffmpeg` before calling the provider.

If `ffmpeg` is not installed and the upload is in a non-native container, the request fails with `500` and a helpful error message. Install `ffmpeg` (`brew install ffmpeg` / `apt install ffmpeg`) to fix this.

You can force transcoding for every upload by setting an environment flag — see `app/audio.py` (`always_transcode` parameter, currently off by default).

---

## Supported response formats

| `response_format` | Backed by | Notes |
|---|---|---|
| `json` | always | `{"text": "..."}` |
| `text` | always | raw string |
| `verbose_json` | always | language / duration / segments / word timings when available |
| `srt` | always | single block if provider gives no segments, otherwise one block per segment |
| `vtt` | always | same as SRT, with `WEBVTT` header and `.` separator |
| `diarized_json` | **rejected (400)** | neither backend returns speaker labels |

---

## Project layout

```
stt-proxy/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, uvicorn entry point
│   ├── config.py          # pydantic-settings env loader
│   ├── audio.py           # format detection + ffmpeg fallback
│   ├── response.py        # OpenAI response shape renderers
│   ├── router.py          # /v1/audio/transcriptions endpoint
│   └── providers/
│       ├── __init__.py
│       ├── base.py        # abstract Provider + routing helpers
│       ├── salute.py      # SaluteSpeech SDK wrapper
│       └── yandex.py      # Yandex AI Studio SDK wrapper
├── scripts/
│   └── info.py            # `task dev:info` helper
├── pyproject.toml         # uv project + dependencies
├── Taskfile.yml           # end-user tasks (install-tool, run)
├── Taskfile.dev.yml       # developer tasks (included under the `dev:` namespace)
├── .env.example           # documented sample env
├── .gitignore
└── README.md
```

---

## Development workflow

```bash
# Install the global CLI tool (creates the `stt-proxy` command on PATH)
task install-tool

# Run with auto-reload on code changes
task dev:serve

# Lint
task dev:lint

# Auto-format
task dev:format
# or both
task dev:format-and-lint

# Print versions of key tools
task dev:doctor

# Inspect effective configuration
task dev:info

# Add a runtime dependency
task dev:add httpx
# Add a dev dependency
task dev:add --dev pytest-asyncio

# Run tests (none yet, but the wiring is in place)
task dev:test

# Wipe caches
task dev:clean
```

### Useful environment overrides for `task dev:serve`

```bash
STT_PROXY_HOST=127.0.0.1 STT_PROXY_PORT=9000 task dev:serve
```

### Code style

- Ruff for lint and format (`task dev:format-and-lint`)
- Type hints throughout; `from __future__ import annotations` in every module

---

## Troubleshooting

### "No STT provider configured"

You started the server without setting any provider credentials. Copy `.env.example` to `.env`, fill in at least one provider's keys, and try again. The process exits with code `2` on purpose — there is no useful behaviour without a backend.

### "Multiple STT providers configured but STT_PROXY_DEFAULT_PROVIDER is unset"

You enabled both Yandex and SaluteSpeech, but your request `model` has no prefix and you didn't set `STT_PROXY_DEFAULT_PROVIDER`. Either:
- set `STT_PROXY_DEFAULT_PROVIDER=yandex` (or `salute`) in `.env`, or
- prefix the request `model` with `yandex-` or `salute-`.

### "Yandex transcription failed: ... StatusCode.UNAUTHENTICATED"

`STT_PROXY_YANDEX_API_KEY` is wrong or does not have the `ai.speechkit.stt` role in the specified folder. Verify both the key and the folder id at <https://console.yandex.cloud/>.

### SaluteSpeech "TokenRequestError" / "401 Unauthorized"

`STT_PROXY_SALUTESPEECH_KEY` is malformed. It must be the base64 of `client_id:client_secret` exactly as shown in SaluteSpeech Studio → Project → Authorization key. You can recreate it:

```bash
printf 'CLIENT_ID:CLIENT_SECRET' | base64
```

### SaluteSpeech SSL: "self-signed certificate in certificate chain"

You're missing the Russian Ministry of Digital Development CA certificates. The `salute-speech` SDK normally installs them via `certifi`, but if you have a stripped-down Python install, follow the [official guide](https://developers.sber.ru/docs/ru/salutespeech/quick-start/certificates).

### Bad audio: "Unable to detect audio encoding"

The bytes you uploaded don't look like a recognised audio container. Either:
- rename the file with the right extension (`.wav`, `.mp3`, `.ogg`),
- include `Content-Type: audio/wav` in your upload, or
- install `ffmpeg` so the proxy can transcode unknown containers.

### ffmpeg "command not found"

`brew install ffmpeg` (macOS) / `apt install ffmpeg` (Debian/Ubuntu). Without it, only MP3 / WAV / OGG uploads succeed.

---

## License

MIT.
