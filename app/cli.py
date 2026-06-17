"""``stt-proxy`` console command — argparse front-end for the daemon.

Three subcommands, all routed through :func:`main`:

* ``stt-proxy start`` — spawn a detached background daemon (see
  :mod:`app.daemon`) that inherits the current shell's environment.
* ``stt-proxy stop``  — find the running daemon via its PID file and stop
  it (SIGTERM, then SIGKILL after a 10-second grace period).
* ``stt-proxy logs [-f]`` — print log/PID paths and optionally ``tail -f``
  the log file.

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

from .daemon import DaemonPaths, is_running, read_pid, stop_daemon


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stt-proxy",
        description=(
            "Manage the stt-proxy background daemon. "
            "Run `stt-proxy start` to launch it; `stt-proxy stop` to terminate it; "
            "`stt-proxy logs [-f]` to inspect the log file."
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
    return parser


def _cmd_start(_args: argparse.Namespace) -> int:
    paths = DaemonPaths.from_platformdirs()
    paths.ensure()

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

    # Give the child a moment to write its PID file (or fail).
    time.sleep(0.5)

    pid = read_pid(paths.pid_file)
    if pid is None or not is_running(pid):
        print(
            f"Failed to start; check log file: {paths.log_file}",
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


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        return _cmd_start(args)
    if args.command == "stop":
        return _cmd_stop(args)
    if args.command == "logs":
        return _cmd_logs(args)

    # argparse with `required=True` on subparsers prevents reaching here.
    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
