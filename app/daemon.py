"""Detached background daemon for ``stt-proxy``.

Launched indirectly by ``stt-proxy start`` (see :mod:`app.cli`) as a child
process running ``python -m app.daemon``. The child:

* reuses the parent shell's environment (no ``.env`` lookup — enforced via
  the ``STT_PROXY_DAEMON`` flag, see :func:`app.config.load_settings`);
* redirects all logging to a single :class:`~logging.handlers.RotatingFileHandler`
  under :func:`platformdirs.user_log_dir`;
* writes its own PID to :func:`platformdirs.user_runtime_dir` so the CLI's
  ``stop`` subcommand can find and signal it;
* unlinks the PID file in a ``finally`` block around :func:`run_server`, so
  the file goes away on clean exit, crash, or uvicorn's signal-triggered
  graceful shutdown (SIGTERM/SIGINT → uvicorn returns → ``finally`` runs).

This module is POSIX-only; it relies on ``SIGTERM`` / ``SIGINT`` being
defined. Windows is not supported.
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs

from .config import load_settings

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_LOG_BACKUP_COUNT = 5


@dataclass(frozen=True)
class DaemonPaths:
    """Filesystem locations the daemon and CLI agree on."""

    log_dir: Path
    log_file: Path
    pid_file: Path

    @classmethod
    def from_platformdirs(cls) -> DaemonPaths:
        """Resolve the standard per-user log / runtime directories.

        On macOS ``user_log_dir`` resolves to ``~/Library/Logs/stt-proxy`` and
        ``user_runtime_dir`` to ``$(TMPDIR)/stt-proxy`` (typically under
        ``/var/folders/...``). On Linux it is ``~/.cache/stt-proxy/log`` and
        ``/run/user/<uid>/stt-proxy`` respectively.
        """
        log_dir = Path(platformdirs.user_log_dir("stt-proxy", "stt-proxy"))
        runtime_dir = Path(platformdirs.user_runtime_dir("stt-proxy", "stt-proxy"))
        return cls(
            log_dir=log_dir,
            log_file=log_dir / "stt-proxy.log",
            pid_file=runtime_dir / "stt-proxy.pid",
        )

    def ensure(self) -> None:
        """Create the log directory and the PID file's parent if missing."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)


def read_pid(pid_file: Path) -> int | None:
    """Return the PID stored in ``pid_file`` or ``None`` if absent / invalid."""
    if not pid_file.is_file():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(pid: int) -> bool:
    """True if a process with ``pid`` is currently alive.

    Mirrors the standard "signal 0" probe: ESRCH means no such process,
    EPERM means it exists but is owned by someone else (we treat that as
    "running" so we don't accidentally clobber a foreign PID file).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:  # ESRCH
        return False
    except PermissionError:  # EPERM — exists but not ours
        return True
    return True


def stop_daemon(pid: int, paths: DaemonPaths, timeout: float = 10.0) -> int:
    """Stop the running daemon, escalating from SIGTERM to SIGKILL.

    Returns 0 on success. The PID file is unlinked regardless of which
    signal actually brought the process down.
    """
    import time

    print(f"Stopping stt-proxy (pid={pid})")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Vanished between the liveness check and the signal — treat as success.
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            break
        time.sleep(0.2)

    if is_running(pid):
        print(f"Daemon did not exit after {timeout:g}s, sending SIGKILL", flush=True)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        paths.pid_file.unlink()
    except FileNotFoundError:
        pass
    return 0


def _configure_logging(log_level: str, paths: DaemonPaths) -> None:
    """Replace the root logger's handlers with a single rotating file handler.

    ``force=True`` on :func:`logging.basicConfig` drops handlers from the
    root logger, but uvicorn attaches its own handlers to the
    ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` named loggers, so we
    also swap those out — otherwise uvicorn's startup errors (e.g. a lifespan
    crash) would go to the inherited stderr (which is ``DEVNULL`` for the
    daemon) and never reach the log file. After this, every log line — ours,
    uvicorn's, and the providers' — lands in the same rotating file.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    handler = RotatingFileHandler(
        paths.log_file,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    logging.basicConfig(force=True, level=level, handlers=[handler])

    # Re-point uvicorn's named loggers at the same file handler. uvicorn
    # normally installs a StreamHandler(stderr) on these via its default
    # LOGGING_CONFIG; for a daemon whose stderr is /dev/null that would
    # silently swallow startup errors.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
        uv_logger.setLevel(level)


def main() -> None:
    """Daemon entry point — invoked via ``python -m app.daemon``.

    PID-file lifecycle: the file is written just before entering
    :func:`run_server` and unlinked in a ``finally`` block. uvicorn's own
    graceful-shutdown handlers (installed on its asyncio event loop) catch
    SIGTERM/SIGINT, drain the server, and return from ``run_server`` — which
    runs the ``finally`` and removes the PID file. We deliberately do NOT
    install competing ``signal.signal`` handlers: a low-level handler that
    raises during uvicorn's startup window can crash the server, and any
    stale PID file left by a SIGKILL is cleaned up by ``stt-proxy start``'s
    stale-PID detection on the next launch.
    """
    paths = DaemonPaths.from_platformdirs()
    paths.ensure()

    # Configure logging BEFORE load_settings() so even a config error lands
    # in the log file rather than vanishing into /dev/null (the parent CLI
    # redirected our stdio to DEVNULL).
    try:
        # Importing app.main triggers its module-level ``app = create_app()``,
        # which itself calls ``load_settings()`` (honouring STT_PROXY_DAEMON).
        # We import it lazily inside this try block so that a malformed TOML
        # config file — which makes ``load_settings()`` raise during source
        # construction — is caught here and lands in the log file instead of
        # dying silently during module import.
        from .main import run_server

        settings = load_settings(env_file=None)
    except BaseException as exc:
        # load_settings() may raise SystemExit(2) on a "no provider
        # configured" refusal (with a message already printed to stderr),
        # OR any other exception — most notably a TOML parse error from
        # pydantic-settings when the config file at
        # ~/.config/stt-proxy/config.toml is malformed. Either way stderr
        # is DEVNULL for the daemon, so re-initialise logging with the
        # default level and record the cause before re-raising.
        _configure_logging("INFO", paths)
        if isinstance(exc, SystemExit):
            logging.getLogger(__name__).error(
                "load_settings() refused to start (no provider configured); exiting"
            )
        else:
            logging.getLogger(__name__).exception(
                "load_settings() failed (malformed config file or other error); exiting"
            )
        raise

    _configure_logging(settings.log_level, paths)
    log = logging.getLogger(__name__)
    log.info("stt-proxy daemon starting (pid=%d)", os.getpid())

    paths.pid_file.write_text(str(os.getpid()))

    try:
        # log_config=None tells uvicorn to leave logging alone so our
        # RotatingFileHandler (configured above) keeps capturing uvicorn's
        # startup banner and access logs. Otherwise uvicorn's LOGGING_CONFIG
        # would re-point "uvicorn.error"/"uvicorn.access" at a stderr
        # StreamHandler — which is DEVNULL for a detached daemon.
        run_server(settings, reload=False, log_config=None)
    except BaseException as exc:
        # Catch anything (crash, KeyboardInterrupt, unhandled SystemExit)
        # so it lands in the log file before the process dies. uvicorn's
        # own graceful shutdown on SIGTERM/SIGINT causes run_server() to
        # return normally — that path skips this branch and goes straight
        # to the finally block, which is what we want.
        if not isinstance(exc, SystemExit):
            log.exception("stt-proxy daemon crashed")
        else:
            log.info("stt-proxy daemon exiting (code=%s)", exc.code)
        raise
    finally:
        log.debug("stt-proxy daemon cleaning up pid file")
        try:
            paths.pid_file.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
