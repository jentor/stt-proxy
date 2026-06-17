"""``stt-proxy`` console command — argparse front-end for the daemon.

Four subcommands, all routed through :func:`main`:

* ``stt-proxy start`` — spawn a detached background daemon (see
  :mod:`app.daemon`) that inherits the current shell's environment.
* ``stt-proxy stop``  — find the running daemon via its PID file and stop
  it (SIGTERM, then SIGKILL after a 10-second grace period).
* ``stt-proxy logs [-f]`` — print log/PID paths and optionally ``tail -f``
  the log file.
* ``stt-proxy config`` — print the effective configuration the daemon would
  see, with secrets masked. ``stt-proxy config init [--force]`` creates
  ``~/.config/stt-proxy/config.toml`` from a documented template.

If ``~/.config/stt-proxy/config.toml`` exists but is malformed, both
``start`` and ``config`` refuse to run with a clear error to stderr before
doing any I/O.

This module deliberately uses only the standard library so that installing
the tool doesn't pull in any dependency beyond what the FastAPI app already
needs (plus ``platformdirs``, which is shared with :mod:`app.daemon`).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
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

# ----- SaluteSpeech -----
# salutespeech_key = "base64(client_id:client_secret)"

# ----- Routing (only relevant when both providers are configured) -----
# default_provider = "yandex"             # or "salute"

# ----- HTTP server -----
# host = "0.0.0.0"
# port = 8000
# log_level = "INFO"                      # DEBUG / INFO / WARNING / ERROR
# workers = 1
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stt-proxy",
        description=(
            "Manage the stt-proxy background daemon. "
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
        help="View or initialise the config file (~/.config/stt-proxy/config.toml).",
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
    salute_state = "enabled" if settings.salute_enabled else "disabled"
    default_provider = settings.default_provider or "(auto)"

    yandex_api_key = _mask_secret(settings.yandex_api_key)
    yandex_folder_id = settings.yandex_folder_id or "(not set)"
    salutespeech_key = _mask_secret(settings.salutespeech_key)

    print(
        "stt-proxy configuration (daemon view)\n"
        "─────────────────────────────────────\n"
        "  HTTP server\n"
        f"    host       = {settings.host}\n"
        f"    port       = {settings.port}\n"
        f"    log_level  = {settings.log_level}\n"
        f"    workers    = {settings.workers}\n"
        "\n"
        "  Providers\n"
        f"    yandex     = {yandex_state}{yandex_suffix}\n"
        f"    salute     = {salute_state}\n"
        "\n"
        "  Routing\n"
        f"    default_provider = {default_provider}\n"
        "\n"
        "  Yandex\n"
        f"    api_key    = {yandex_api_key}\n"
        f"    folder_id  = {yandex_folder_id}\n"
        f"    model      = {settings.yandex_model}\n"
        "\n"
        "  SaluteSpeech\n"
        f"    key        = {salutespeech_key}\n"
        "\n"
        "  Sources\n"
        "    env_file          = (not loaded)\n"
        f"    config file       = {config_source}",
    )
    return 0


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

    # argparse with `required=True` on subparsers prevents reaching here.
    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
