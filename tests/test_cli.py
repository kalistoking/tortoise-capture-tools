"""The command line's own decisions: what a run does besides its main job."""

from __future__ import annotations

import contextlib
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from test_slim import _session
from tortoise_capture import cli
from tortoise_capture.wire import slim

_CFG = SimpleNamespace(logon_ip=None, logon_port=slim.LOGON_PORT)
_ARGS = SimpleNamespace(no_slim=False)
_SESSION = SimpleNamespace(server=("127.0.0.1", 8090), c2s=SimpleNamespace(segments=()))


class _Errors(logging.Handler):
    """Counts what a run would report as an error -- what fails its exit code."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


@contextlib.contextmanager
def _replaced(owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def _slim_beside_a_capture(patches):
    """Runs the automatic slim step on a small capture holding foreign traffic."""
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        capture = Path(tmp) / "capture.pcap"
        capture.write_bytes(_session())
        for (owner, name), value in patches.items():
            stack.enter_context(_replaced(owner, name, value))
        seen = _Errors()
        cli._logger.addHandler(seen)
        try:
            cli._slim_beside(capture, _SESSION, b"key", _CFG, None, None, _ARGS)
        finally:
            cli._logger.removeHandler(seen)
        return seen.count, sorted(p.name for p in Path(tmp).iterdir())


def _refuse(*_):
    raise PermissionError("read-only share")


def test_a_slim_copy_that_cannot_be_written_does_not_stop_the_run():
    """The copy is a courtesy beside the real work; a read-only capture folder
    must cost the copy, not the decode the user asked for."""
    errors, files = _slim_beside_a_capture({(slim, "write"): _refuse})
    assert files == ["capture.pcap"] and errors == 0


def test_a_slim_copy_that_decodes_differently_is_dropped_without_failing_the_run():
    decoded = iter([["the original"], ["something else"]])
    read = lambda *_: _SESSION                                           # noqa: E731
    errors, files = _slim_beside_a_capture({
        (cli.pcap, "read_session"): read,
        (cli.crypt, "recover_session_key"): lambda _: b"key",
        (cli, "_decoded"): lambda *_: next(decoded),
    })
    assert files == ["capture.pcap"] and errors == 0
