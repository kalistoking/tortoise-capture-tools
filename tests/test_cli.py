"""The command line's own decisions: what a run does besides its main job."""

from __future__ import annotations

import contextlib
import io
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from test_slim import _session
from tortoise_capture import cli
from tortoise_capture.wire import slim
from tortoise_capture.wire.opcodes import OpcodeTable

_CFG = SimpleNamespace(logon_ip=None, logon_port=slim.LOGON_PORT)
_ARGS = SimpleNamespace(no_slim=False)
_SESSION = SimpleNamespace(server=("127.0.0.1", 8090), c2s=SimpleNamespace(segments=()),
                           s2c="the server's stream")
_TABLES = SimpleNamespace(opcodes=OpcodeTable(by_value={0x1EC: "SMSG_AUTH_CHALLENGE", 0x4FF: "X"}))


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


def test_a_slim_copy_whose_write_fails_leaves_no_partial_file_behind():
    """The write ran before the try whose cleanup unlinks the .partial file, so
    a disk filling up mid-write left half a capture beside the original."""
    def failing_write(plan, partial):
        Path(partial).write_bytes(b"half a capture")
        raise OSError("no space left on device")

    with tempfile.TemporaryDirectory() as tmp, _replaced(slim, "write", failing_write):
        capture = Path(tmp) / "capture.pcap"
        capture.write_bytes(_session())
        plan = slim.plan(capture, slim.Endpoint(None, slim.LOGON_PORT), None)
        try:
            cli._write_verified(plan, Path(tmp) / "capture.wow.pcap", _SESSION, b"key", None, None)
        except OSError:
            pass
        else:
            raise AssertionError("the write's own failure must still reach the caller")
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["capture.pcap"]


def _slim_replace(decode_errors):
    """`tct slim --replace` on a capture holding foreign traffic, whose decode
    logs `decode_errors` errors. Returns (exit code, the capture's bytes before
    and after, the files left in its folder)."""
    calls = []

    def decoded(*_):
        calls.append(1)
        if len(calls) == 1:                                  # the original's decode speaks
            for _ in range(decode_errors):
                cli.pipeline._logger.error("framing desync")
        return ["the same"]

    root = logging.getLogger(cli._log.ROOT)
    saved = (root.level, root.propagate, list(root.handlers))
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        capture = Path(tmp) / "capture.pcap"
        capture.write_bytes(_session())
        before = capture.read_bytes()
        config = Path(tmp) / "tct.toml"
        config.write_text("", encoding="utf-8")
        for (owner, name), value in {(cli, "_load_tables"): lambda _: _TABLES,
                                     (cli.registry_mod, "load"): lambda _: None,
                                     (cli, "_context"): lambda *_, **__: None,
                                     (cli, "_decoded"): decoded,
                                     (cli.crypt, "recover_session_key"): lambda *_: b"key"}.items():
            stack.enter_context(_replaced(owner, name, value))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            code = cli.main(["slim", str(capture), "--replace", "--config", str(config),
                             "--no-log-file"])
        finally:
            root.setLevel(saved[0])                  # main() installs the tct logger's handlers
            root.propagate = saved[1]
            root.handlers[:] = saved[2]
        return code, before, capture.read_bytes(), sorted(p.name for p in Path(tmp).iterdir())


def test_replace_keeps_the_original_when_the_run_logged_an_error():
    """--replace swapped the slim copy in as soon as it verified, before main
    turned the original's decode errors into exit 2: the original -- the only
    copy of a session that cannot be recorded again -- was gone from a run that
    was not clean. The verified copy stays beside it for the user to keep."""
    code, before, after, files = _slim_replace(decode_errors=1)
    assert code == cli.EXIT_WITH_ERRORS
    assert after == before
    assert files == ["capture.pcap", "capture.wow.pcap", "tct.toml"]


def test_replace_still_replaces_the_original_after_a_clean_run():
    code, before, after, files = _slim_replace(decode_errors=0)
    assert code == cli.EXIT_OK
    assert after != before and len(after) < len(before)
    assert files == ["capture.pcap", "tct.toml"]


def test_a_slim_copy_that_decodes_differently_is_dropped_without_failing_the_run():
    decoded = iter([["the original"], ["something else"]])
    read = lambda *_: _SESSION                                           # noqa: E731
    errors, files = _slim_beside_a_capture({
        (cli.pcap, "read_session"): read,
        (cli.crypt, "recover_session_key"): lambda _: b"key",
        (cli, "_decoded"): lambda *_: next(decoded),
    })
    assert files == ["capture.pcap"] and errors == 0


def test_checking_a_slim_copy_reports_nothing_the_run_would_count_as_an_error():
    """The check decodes both captures again and recovers both keys, only to
    compare them; with --session-key given for a capture no key comes out of,
    that logged the recovery's errors twice and failed a run that had worked."""
    def recover(_):
        cli.crypt._logger.error("recovered only 3/40 session key bytes")
        return None

    def decoded(*_):
        cli.pipeline._logger.error("framing desync")
        return ["the same"]

    seen = _Errors()
    root = logging.getLogger(cli._log.ROOT)
    root.addHandler(seen)
    try:
        _, files = _slim_beside_a_capture({
            (cli.pcap, "read_session"): lambda *_: _SESSION,
            (cli.crypt, "recover_session_key"): recover,
            (cli, "_decoded"): decoded,
        })
    finally:
        root.removeHandler(seen)
    assert seen.count == 0 and len(files) == 2


def test_slim_itself_reports_what_the_original_decodes_with():
    """`tct slim` decodes the original only to check its copy, so muting that
    decode hid a damaged capture: it exited 0, and --replace went ahead."""
    copy = SimpleNamespace(c2s=SimpleNamespace(segments=()))

    def decoded(session, *_):
        if session is _SESSION:
            cli.pipeline._logger.error("framing desync")
        return ["the same"]

    seen = _Errors()
    root = logging.getLogger(cli._log.ROOT)
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        capture = Path(tmp) / "capture.pcap"
        capture.write_bytes(_session())
        for (owner, name), value in {(cli.pcap, "read_session"): lambda *_: copy,
                                     (cli.crypt, "recover_session_key"): lambda _: b"key",
                                     (cli, "_decoded"): decoded}.items():
            stack.enter_context(_replaced(owner, name, value))
        plan = slim.plan(capture, slim.Endpoint(None, slim.LOGON_PORT), None)
        root.addHandler(seen)
        try:
            cli._write_verified(plan, Path(tmp) / "capture.wow.pcap", _SESSION, b"key", None, None)
        finally:
            root.removeHandler(seen)
    assert seen.count == 1


def test_muting_ends_even_when_the_muted_work_fails():
    try:
        with cli._log.muted():
            raise RuntimeError("the copy could not be read")
    except RuntimeError:
        pass
    assert not logging.getLogger(cli._log.ROOT).manager.disable


def _world_of(content, named=frozenset(), port=8090, server_ip="127.0.0.1"):
    cfg = SimpleNamespace(logon_ip=None, logon_port=slim.LOGON_PORT, named=named,
                          server_ip=server_ip, port=port)
    with tempfile.TemporaryDirectory() as tmp:
        capture = Path(tmp) / "capture.pcap"
        capture.write_bytes(content)
        return cli._world(cfg, capture)


def test_a_capture_is_decoded_on_the_world_server_its_realm_list_names():
    """A server on 8085 used to need --port, though the capture says so itself."""
    assert _world_of(_session(world_port=8085, listed="127.0.0.1:8085")) == ("127.0.0.1", 8085)


def test_a_named_port_still_wins_over_the_realm_list():
    content = _session(world_port=8085, listed="127.0.0.1:8085")
    assert _world_of(content, named=frozenset({"port"}), port=9000) == ("127.0.0.1", 9000)


def test_a_named_address_alone_still_wins_over_the_realm_list():
    """slim.py promises that a named endpoint always wins; the address alone
    was dropped in favour of whatever the realm list named."""
    content = _session(world_port=8085, listed="127.0.0.1:8085")
    assert _world_of(content, named=frozenset({"server_ip"}), server_ip="10.0.0.5")[0] == "10.0.0.5"
    assert _world_of(content, named=frozenset({"server_ip"})) == ("127.0.0.1", 8085)


def test_a_named_address_no_realm_is_at_is_warned_about_not_whispered():
    """A tct.toml copied from the old example keeps server_ip uncommented; with
    the realm elsewhere the run fell back to 127.0.0.1:8090 and said why only
    at debug level."""
    warned = _Errors()
    warned.setLevel(logging.WARNING)
    cli._logger.addHandler(warned)
    try:
        _world_of(_session(world_port=8085, listed="127.0.0.1:8085"),
                  named=frozenset({"server_ip"}), server_ip="10.0.0.5")
    finally:
        cli._logger.removeHandler(warned)
    assert warned.count == 1


def test_a_capture_without_a_realm_list_falls_back_to_the_default():
    assert _world_of(_session(with_logon=False)) == ("127.0.0.1", 8090)


def test_a_config_value_of_the_wrong_type_is_a_config_error_not_a_traceback():
    """`repo = 1` in tct.toml was a TypeError out of main; it is the config
    error with exit 1 that a malformed file already gets."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "tct.toml"
        config.write_text("[capture]\nrepo = 1\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["opcodes", "--config", str(config)])
    assert code == cli.EXIT_FATAL
    assert err.getvalue().startswith("ERROR config:") and "[capture] repo" in err.getvalue()


def test_slim_refuses_an_out_path_it_would_not_write_to():
    """--replace writes beside the capture and then over it; an --out given
    with it was dropped without a word."""
    try:
        cli.build_parser().parse_args(["slim", "capture.pcap", "--replace", "--out", "x.pcap"])
    except SystemExit:
        return
    raise AssertionError("--out and --replace together must be refused")


def test_a_recovered_key_is_checked_against_the_server_and_the_forks_highest_opcode():
    """The key came out of the client's headers alone and was trusted: a slot pair
    gone wrong dispatched every tenth server message to the wrong module."""
    seen = []

    def recover(segments, server=None, ceiling=None):
        seen.append((segments, server, ceiling))
        return b"key"

    with _replaced(cli.crypt, "recover_session_key", recover):
        assert cli._session_key(_SESSION, None, _TABLES.opcodes) == b"key"
        assert cli._session_key(_SESSION, "6b6579", _TABLES.opcodes) == b"key"  # given
    assert seen == [((), "the server's stream", 0x4FF)]
