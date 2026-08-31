"""``stt-proxy`` console command — daemon management and direct transcription.

Six subcommands, all routed through :func:`main`:

* ``stt-proxy start`` — spawn a detached background daemon (see
  :mod:`app.daemon`) that inherits the current shell's environment.
* ``stt-proxy stop``  — find the running daemon via its PID file and stop
  it (SIGTERM, then SIGKILL after a 10-second grace period).
* ``stt-proxy logs [-f]`` — print log/PID paths and optionally ``tail -f``
  the log file.
* ``stt-proxy config`` — print the effective configuration the daemon would
  see, with secrets masked. ``stt-proxy config init [--force]`` creates
  ``~/.config/stt-proxy/config.toml`` from a documented template.
* ``stt-proxy transcribe`` — transcribe a file directly with no daemon.
* ``stt-proxy models`` — print model ids accepted by ``--model``.

If ``~/.config/stt-proxy/config.toml`` exists but is malformed, both
``start`` and ``config`` refuse to run with a clear error to stderr before
doing any I/O.

This module deliberately uses only the standard library so that installing
the tool doesn't pull in any dependency beyond what the FastAPI app already
needs (plus ``platformdirs``, which is shared with :mod:`app.daemon`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Sequence

from .config import (
    ConfigFileError,
    _config_file_path,
    _parse_config_file,
    load_settings,
)
from .daemon import DaemonPaths, is_running, read_pid, stop_daemon


_CONFIG_TEMPLATE = """\
# stt-proxy configuration
# Location: ~/.config/stt-proxy/config.toml
# Permissions: 0o600 (owner read/write — this file may contain API keys).
#
# Settings are read in this order (highest priority first):
#   1. shell environment variables (STT_PROXY_*)
#   2. this file
#   3. built-in defaults
#
# After editing, restart the daemon: `stt-proxy stop && stt-proxy start`.
#
# Uncomment a line to set the value. Strings need quotes, numbers/booleans
# don't. See README.md for the full description of every key.

# ----- Yandex SpeechKit (both required to enable Yandex) -----
# yandex_api_key = "AQVNxxxxxxxxxxxxxxxx"
# yandex_folder_id = "b1gxxxxxxxxxxxxxxxxxx"
# yandex_model = "general"                # general, general:rc, deferred-general, ...

# ----- HTTP server -----
# host = "127.0.0.1"
# port = 8000
# log_level = "INFO"                      # DEBUG / INFO / WARNING / ERROR
# workers = 1
# api_key = "replace-with-at-least-32-random-characters"
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stt-proxy",
        description=(
            "Transcribe audio or manage the stt-proxy background daemon. "
            "Run `stt-proxy transcribe --model PROVIDER/MODEL FILE` for a direct request; "
            "`stt-proxy models` lists known model ids. "
            "Run `stt-proxy start` to launch it; `stt-proxy stop` to terminate it; "
            "`stt-proxy logs [-f]` to inspect the log file; "
            "`stt-proxy config` to view effective config or "
            "`stt-proxy config init` to create the config file."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Spawn the stt-proxy daemon in the background.")

    sub.add_parser("stop", help="Stop the running stt-proxy daemon.")

    p_logs = sub.add_parser(
        "logs", help="Print log/PID paths, optionally tail the log file."
    )
    p_logs.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Continuously tail the log file (like `tail -f`).",
    )

    p_config = sub.add_parser(
        "config",
        help=(
            "Show effective config (default); pass 'init' to create the "
            "config file at ~/.config/stt-proxy/config.toml."
        ),
    )
    # `config_command` is intentionally NOT required — running `stt-proxy config`
    # with no subcommand falls through to the "show effective config" path.
    config_sub = p_config.add_subparsers(dest="config_command")

    p_config_init = config_sub.add_parser(
        "init",
        help="Create the default config file (refuses to overwrite without --force).",
    )
    p_config_init.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing config file.",
    )

    p_transcribe = sub.add_parser(
        "transcribe",
        help="Transcribe an audio file directly, without starting the daemon.",
    )
    p_transcribe.add_argument("file", help="Audio file path, or '-' to read stdin.")
    p_transcribe.add_argument(
        "--model",
        required=True,
        help="Yandex model id, for example yandex/general.",
    )
    p_transcribe.add_argument(
        "--language",
        help="Optional ISO-639-1 or BCP-47 language code (for example ru or ru-RU).",
    )
    p_transcribe.add_argument("--prompt", help="Optional vocabulary hint.")
    p_transcribe.add_argument(
        "--response-format",
        default="json",
        choices=("json", "text", "verbose_json", "srt", "vtt"),
        help="Output format written to stdout (default: json).",
    )
    p_transcribe.add_argument(
        "-o",
        "--output",
        help="Write output atomically to this file instead of stdout.",
    )
    # Some argv-based runners do not invoke a shell and pass `> file` to the
    # program literally. Accept that spelling as a compatibility alias for
    # --output; a real shell consumes both tokens before Python sees them.
    p_transcribe.add_argument("redirect", nargs="*", help=argparse.SUPPRESS)

    p_models = sub.add_parser(
        "models", help="List model ids accepted by the --model option."
    )
    p_models.add_argument(
        "--json", action="store_true", help="Print the model catalogue as JSON."
    )

    return parser


def _cmd_start(_args: argparse.Namespace) -> int:
    paths = DaemonPaths.from_platformdirs()
    paths.ensure()

    # Validate the config file up front so a malformed TOML produces a
    # clear stderr error instead of "Failed to start; check log file…".
    try:
        _parse_config_file(_config_file_path())
    except ConfigFileError as exc:
        print(f"stt-proxy: {exc}", file=sys.stderr)
        print("Fix the config file or remove it, then retry.", file=sys.stderr)
        return 1

    pid = read_pid(paths.pid_file)
    if pid is not None and is_running(pid):
        print(f"already running (pid={pid})", file=sys.stderr)
        return 1
    if pid is not None:
        # Stale PID file (process gone) — clean it up and continue.
        try:
            paths.pid_file.unlink()
        except FileNotFoundError:
            pass

    # Spawn a fully detached child. It inherits the parent's environment —
    # which is exactly what we want: the daemon sees whatever STT_PROXY_*
    # values were exported in the calling shell, and does NOT consult .env.
    # STT_PROXY_DAEMON=1 is the flag that makes load_settings() skip .env on
    # every code path (including the module-level create_app() that uvicorn
    # re-runs when importing "app.main:app"). See app.config.load_settings.
    child_env = os.environ.copy()
    child_env["STT_PROXY_DAEMON"] = "1"
    subprocess.Popen(  # noqa: S603 -- argv is fully controlled by us
        [sys.executable, "-m", "app.daemon"],
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    # Poll for the daemon's PID file. The daemon can take a few seconds to
    # start on cold Python (uvicorn + yandex SDK + grpc imports), so we wait
    # up to 30s before giving up. 0.1s polling keeps the loop responsive
    # without being wasteful.
    deadline = time.monotonic() + 30.0
    pid: int | None = None
    while time.monotonic() < deadline:
        pid = read_pid(paths.pid_file)
        if pid is not None and is_running(pid):
            break
        if pid is not None:
            # PID file written but process is gone — fail fast.
            break
        time.sleep(0.1)
    else:
        print(
            f"Failed to start within 30s; check log file: {paths.log_file}",
            file=sys.stderr,
        )
        return 1

    if pid is None or not is_running(pid):
        print(
            f"Daemon exited before becoming ready; check log file: {paths.log_file}",
            file=sys.stderr,
        )
        return 1

    print(f"stt-proxy started (pid={pid})")
    print(f"logs: {paths.log_file}")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    paths = DaemonPaths.from_platformdirs()

    pid = read_pid(paths.pid_file)
    if pid is None:
        print("not running (no pid file)", file=sys.stderr)
        return 1

    if not is_running(pid):
        try:
            paths.pid_file.unlink()
        except FileNotFoundError:
            pass
        print("stale pid file removed", file=sys.stderr)
        return 1

    stop_daemon(pid, paths)
    print("Stopped.")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    paths = DaemonPaths.from_platformdirs()
    print(f"log_dir:  {paths.log_dir}")
    print(f"log_file: {paths.log_file}")
    print(f"pid_file: {paths.pid_file}")

    if not args.follow:
        return 0

    if not paths.log_file.is_file():
        print(
            "log file does not exist yet (is the daemon running?)",
            file=sys.stderr,
        )
        return 1

    try:
        subprocess.run(["tail", "-f", str(paths.log_file)], check=True)  # noqa: S603,S607 -- tail is the explicit user request
    except KeyboardInterrupt:
        return 0
    return 0


def _mask_secret(value: str | None) -> str:
    """Render a secret as ``***<last4>`` or ``(not set)`` if empty/None.

    Values shorter than 4 characters collapse to just ``***`` so we never
    reveal the whole secret through the mask.
    """
    if not value:
        return "(not set)"
    tail = value[-4:] if len(value) >= 4 else ""
    return f"***{tail}"


def _cmd_config_init(args: argparse.Namespace) -> int:
    path = _config_file_path()
    if path.exists() and not args.force:
        print(
            f"config file already exists: {path} (use --force to overwrite)",
            file=sys.stderr,
        )
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"Created config file: {path}")
    return 0


def _cmd_config_show(_args: argparse.Namespace) -> int:
    """Render the effective daemon-view configuration to stdout.

    Loads with ``daemon=True`` (skip ``.env``, optionally read the TOML
    config file) and ``validate=False`` (so a no-provider state still
    renders instead of exiting).
    """
    # Validate the config file up front so a malformed TOML produces a
    # clear stderr error (same fail-fast behaviour as `stt-proxy start`).
    try:
        _parse_config_file(_config_file_path())
    except ConfigFileError as exc:
        print(f"stt-proxy: {exc}", file=sys.stderr)
        return 1

    try:
        settings = load_settings(daemon=True, validate=False)
    except ConfigFileError as exc:
        # Defense-in-depth: catches the race where the file was valid at
        # pre-validation but malformed by the time load_settings() read it.
        print(f"stt-proxy: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any other config error to the user
        print(
            f"Failed to load configuration: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    config_path = _config_file_path()
    config_source = str(config_path) if config_path.is_file() else "(not loaded)"

    yandex_state = "enabled" if settings.yandex_enabled else "disabled"
    yandex_suffix = (
        f" (model={settings.yandex_model})" if settings.yandex_enabled else ""
    )
    api_key = _mask_secret(settings.api_key)
    yandex_api_key = _mask_secret(settings.yandex_api_key)
    yandex_folder_id = settings.yandex_folder_id or "(not set)"

    print(
        "stt-proxy configuration (daemon view)\n"
        "─────────────────────────────────────\n"
        "  HTTP server\n"
        f"    host       = {settings.host}\n"
        f"    port       = {settings.port}\n"
        f"    log_level  = {settings.log_level}\n"
        f"    workers    = {settings.workers}\n"
        f"    api_key    = {api_key}\n"
        "\n"
        "  Providers\n"
        f"    yandex     = {yandex_state}{yandex_suffix}\n"
        "\n"
        "  Yandex\n"
        f"    api_key    = {yandex_api_key}\n"
        f"    folder_id  = {yandex_folder_id}\n"
        f"    model      = {settings.yandex_model}\n"
        "\n"
        "  Sources\n"
        "    env_file          = (not loaded)\n"
        f"    config file       = {config_source}",
    )
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    from .providers import YandexProvider

    models = YandexProvider.list_models()
    if args.json:
        print(
            json.dumps(
                [{"id": model.id, "owned_by": model.owned_by} for model in models],
                ensure_ascii=False,
            )
        )
    else:
        for model in models:
            print(model.id)
    return 0


async def _transcribe_file(args: argparse.Namespace) -> object:
    from .audio import normalize
    from .providers import (
        ProviderNotConfiguredError,
        TranscriptionRequest,
        YandexProvider,
        detect_routing,
    )
    from .response import render

    settings = load_settings(daemon=True)
    if detect_routing(args.model) != "yandex":
        raise ValueError(
            "--model must start with 'yandex/' (run `stt-proxy models` for examples)"
        )

    if not settings.yandex_enabled:
        raise ProviderNotConfiguredError(
            "Yandex is not configured; set STT_PROXY_YANDEX_API_KEY and "
            "STT_PROXY_YANDEX_FOLDER_ID"
        )
    provider = YandexProvider(
        api_key=settings.yandex_api_key or "",
        folder_id=settings.yandex_folder_id or "",
        default_model=settings.yandex_model,
    )

    if args.file == "-":
        data = await asyncio.to_thread(sys.stdin.buffer.read)
        filename = None
        content_type = None
    else:
        path = Path(args.file).expanduser()
        if not path.is_file():
            raise ValueError(f"audio file does not exist: {path}")
        data = await asyncio.to_thread(path.read_bytes)
        filename = path.name
        content_type = mimetypes.guess_type(path.name)[0]
    if not data:
        raise ValueError("audio file is empty")

    audio = await normalize(data, filename, content_type)
    result = await provider.transcribe(
        TranscriptionRequest(
            audio=audio,
            model=args.model,
            language=args.language,
            prompt=args.prompt,
            response_format=args.response_format,
            deferred=True,
        )
    )
    return render(result, args.response_format)


def _cmd_transcribe(args: argparse.Namespace) -> int:
    try:
        output_path = _resolve_transcribe_output(args)
        payload = asyncio.run(_transcribe_file(args))
        rendered = _render_transcription(payload)
        if output_path is not None:
            _write_text_atomic(output_path, rendered)
        else:
            print(rendered, end="")
    except (ConfigFileError, OSError, RuntimeError, ValueError) as exc:
        print(f"stt-proxy: {exc}", file=sys.stderr)
        return 1
    return 0


def _resolve_transcribe_output(args: argparse.Namespace) -> Path | None:
    """Resolve --output or a literal argv-level ``> file`` compatibility pair."""
    redirect = list(args.redirect or ())
    if redirect and (len(redirect) != 2 or redirect[0] != ">"):
        raise ValueError("unexpected trailing arguments; use --output FILE or '> FILE'")
    if args.output and redirect:
        raise ValueError("use either --output FILE or '> FILE', not both")
    value = args.output or (redirect[1] if redirect else None)
    return Path(value).expanduser() if value else None


def _render_transcription(payload: object) -> str:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False) + "\n"
    text = str(payload)
    return text if text.endswith("\n") else text + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    """Replace *path* only after the complete transcription is written."""
    parent = path.parent
    if not parent.is_dir():
        raise OSError(f"output directory does not exist: {parent}")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            output.write(text)
            temp_path = Path(output.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Returns a process exit code."""
    parser = _build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # No subcommand: print help instead of erroring on the required
        # subparsers. `stt-proxy --help`, `stt-proxy -h`, and
        # `stt-proxy <subcommand>` all keep their previous behaviour.
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    if args.command == "start":
        return _cmd_start(args)
    if args.command == "stop":
        return _cmd_stop(args)
    if args.command == "logs":
        return _cmd_logs(args)
    if args.command == "config":
        if args.config_command == "init":
            return _cmd_config_init(args)
        return _cmd_config_show(args)
    if args.command == "transcribe":
        return _cmd_transcribe(args)
    if args.command == "models":
        return _cmd_models(args)

    # argparse with `required=True` on subparsers prevents reaching here.
    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
