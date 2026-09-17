"""Four log levels: error, warn, info, debug.

  error  the work failed: a result is missing or wrong   -> stderr, always
  warn   something suspicious, but the work continued    -> stderr
  info   where the run has got to                        -> stdout
  debug  detail: offsets, per-packet decisions           -> stdout

The dividing line between error and warn is whether anything was lost. A
desync stops a direction, so it is an error; a zero-filled TCP gap, a
container whose declared size disagrees with the inflated one, or a missing
checkout that degrades output to numeric-only are all warnings -- the run
still produced its result.

Warnings share stderr with errors on purpose: stdout carries the text report,
and a diagnostic line in the middle of it would corrupt that output.

Verbosity comes from the config file ([log] level / file_level / modules) and
can be overridden per run on the command line. Per-module levels work by
setting the level on `tct.mod.<id>` and keeping the handlers permissive, so
`update_object = "debug"` gives detail for one module without flooding the
console with the other 824.

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
MODULE_PREFIX = "mod"

LEVELS = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,   # accepted spelling, "warn" is the canonical one
    "info": logging.INFO,
    "debug": logging.DEBUG,
}
LEVEL_NAMES = ("error", "warn", "info", "debug")
DEFAULT_LEVEL = "info"

_PLAIN = "%(levelname)-5s %(message)s"
_DETAIL = "%(asctime)s %(levelname)-5s %(name)s %(message)s"

# "warn" is the vocabulary this project uses, and it also keeps every level
# name inside the 5-character column the formats above reserve.
logging.addLevelName(logging.WARNING, "WARN")


def level_value(name: str) -> int | None:
    """Level number for a config/CLI spelling, or None if it is not one."""
    return LEVELS.get(str(name).strip().lower())


class _Counter(logging.Handler):
    """Counts warnings and errors so the exit code needs no bookkeeping."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.errors += 1
        else:
            self.warnings += 1


class _MaxLevel(logging.Filter):
    """Keeps info/debug on stdout: warn and error are stderr's job."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self._level


class _LevelPolicy(logging.Filter):
    """One threshold per destination, with per-module exceptions.

    Handler levels cannot express "everything at info, but this one module at
    debug" -- a handler sitting at info drops the module's debug records before
    anyone can ask which logger they came from. Filtering per record can, so
    the loggers stay permissive and this decides.
    """

    def __init__(self, default: int, overrides: dict[str, int]) -> None:
        super().__init__()
        self._default = default
        self._overrides = overrides          # full logger name -> level

    def filter(self, record: logging.LogRecord) -> bool:
        threshold = self._default
        for name, value in self._overrides.items():
            if record.name == name or record.name.startswith(f"{name}."):
                threshold = value
                break
        return record.levelno >= threshold


_counter = _Counter()


def setup(level: str = DEFAULT_LEVEL, file_level: str | None = None,
          log_file: Path | None = None, command_line: str | None = None,
          quiet: bool = False, module_levels: dict[str, str] | None = None) -> None:
    """Installs the handlers. Call once, right after the config is resolved.

    `quiet` keeps stdout free of log output entirely, for the commands whose
    stdout is a contract (a bare session key parsed by another process).
    Warnings and errors still reach stderr, and the log file still records
    everything its own level admits.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # not a real stream (captured by a test runner) -- harmless

    console = level_value(level) or logging.INFO
    to_file = level_value(file_level or level) or console
    overrides = {f"{ROOT}.{MODULE_PREFIX}.{name}": value
                 for name, raw in (module_levels or {}).items()
                 if (value := level_value(raw)) is not None}

    # Loggers stay permissive at the most verbose threshold anyone asked for;
    # each handler's own policy decides what it keeps. That is what lets the
    # console sit at info while the file keeps debug, and lets one module be
    # more verbose than either.
    floor = min([console, to_file, *overrides.values()])
    # The console format follows what can actually reach the console: a debug
    # line is only useful with its logger name, but a file kept at debug is no
    # reason to clutter an info-level console.
    console_floor = min([console, *overrides.values()])
    fmt = logging.Formatter(_DETAIL if console_floor <= logging.DEBUG else _PLAIN,
                            datefmt="%H:%M:%S")

    root = logging.getLogger(ROOT)
    root.setLevel(floor)
    root.handlers.clear()
    root.propagate = False

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.CRITICAL if quiet else logging.DEBUG)
    out.addFilter(_LevelPolicy(console, overrides))
    out.addFilter(_MaxLevel(logging.INFO))
    out.setFormatter(fmt)
    root.addHandler(out)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)      # warn and error only, whatever the level
    err.addFilter(_LevelPolicy(console, overrides))
    err.setFormatter(fmt)
    root.addHandler(err)

    _counter.warnings = _counter.errors = 0
    root.addHandler(_counter)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.addFilter(_LevelPolicy(to_file, overrides))
        handler.setFormatter(logging.Formatter(_DETAIL, datefmt="%H:%M:%S"))
        root.addHandler(handler)
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        with open(log_file, "a", encoding="utf-8") as fp:
            fp.write(f"\n----- {stamp} -----\n")
            if command_line:
                fp.write(command_line + "\n")

    # Per-module levels live in the handler policies above, not on the loggers
    # themselves -- a level set on a logger would outlive this call and leak
    # into the next run in the same process.


def get_logger(name: str) -> logging.Logger:
    """Logger under the package root; `name` is a component or module id."""
    return logging.getLogger(f"{ROOT}.{name}")


def error_count() -> int:
    return _counter.errors


def warning_count() -> int:
    return _counter.warnings
