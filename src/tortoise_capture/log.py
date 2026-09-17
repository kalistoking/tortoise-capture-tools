"""Three-level logging: info, debug, error. Nothing else.

  info   where the run has got to           -> stdout (always)
  debug  detail, only when --debug is on    -> stdout
  error  a failure                          -> stderr (always) and counted

There is deliberately no WARNING: if it is a failure it is an error, if it is
not it is progress. All three levels also go to the per-capture log file, so
one file holds a target's whole processing history.

Console output must stay ASCII: the local console is cp1250 and a stray
non-ASCII character in a printed line is a real crash source. Streams are
reconfigured to UTF-8 with replacement so a slipped character degrades
instead of aborting the run.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sys
from pathlib import Path

ROOT = "tct"

_PLAIN = "%(levelname)-5s %(message)s"
_DETAIL = "%(asctime)s %(levelname)-5s %(name)s %(message)s"


class _ErrorCounter(logging.Handler):
    """Counts errors so the process can exit 2 without every caller tracking it."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


class _MaxLevel(logging.Filter):
    """Keeps info/debug on stdout: errors are stderr's job, never duplicated."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self._level


_counter = _ErrorCounter()


def setup(debug: bool = False, log_file: Path | None = None, command_line: str | None = None,
          quiet: bool = False) -> None:
    """Installs the handlers. Call once, right after argument parsing.

    `quiet` keeps stdout free of log output entirely, for the commands whose
    stdout is a contract (a bare session key parsed by another process).
    Errors still reach stderr and the log file still receives everything.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # not a real stream (captured by a test runner) -- harmless

    fmt = logging.Formatter(_DETAIL if debug else _PLAIN, datefmt="%H:%M:%S")

    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    root.propagate = False

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.CRITICAL if quiet else (logging.DEBUG if debug else logging.INFO))
    out.addFilter(_MaxLevel(logging.INFO))
    out.setFormatter(fmt)
    root.addHandler(out)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.ERROR)
    err.setFormatter(fmt)
    root.addHandler(err)

    _counter.count = 0
    root.addHandler(_counter)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)  # the file always keeps the detail level
        fh.setFormatter(logging.Formatter(_DETAIL, datefmt="%H:%M:%S"))
        root.addHandler(fh)
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        with open(log_file, "a", encoding="utf-8") as fp:
            fp.write(f"\n----- {stamp} -----\n")
            if command_line:
                fp.write(command_line + "\n")


def get_logger(name: str) -> logging.Logger:
    """Logger under the package root; `name` is a component or module id."""
    return logging.getLogger(f"{ROOT}.{name}")


def error_count() -> int:
    return _counter.count
