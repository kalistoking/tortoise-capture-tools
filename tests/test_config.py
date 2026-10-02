"""Config file parsing, precedence, and the four-level log routing."""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
from pathlib import Path

from tortoise_capture import log as _log
from tortoise_capture.config import CONFIG_NAME, RunConfig

SAMPLE = """
[capture]
repo = "C:/checkout"
port = 9000

[log]
level = "warn"
file_level = "debug"

[log.modules]
update_object = "debug"

[output]
sql_dialect = "sqlite"

[server]
dbc = "C:/server/data/dbc"
"""


class Args:
    """Stands in for the argparse namespace: unset flags are None."""

    def __init__(self, **kwargs):
        defaults = dict(config=None, repo=None, port=None, server_ip=None, log_level=None,
                        debug=False, out_dir=None, log_dir=None, no_log_file=False,
                        cache_dir=None, key_only=False, logon_port=None, logon_ip=None)
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


def _in_dir(body):
    """Runs `body(dir)` with the process inside a fresh empty directory.

    The implicit config lookup is relative to the working directory, so a test
    must not see whatever tct.toml the developer has lying around.
    """
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.getcwd()
        os.chdir(tmp)
        try:
            return body(Path(tmp))
        finally:
            os.chdir(previous)


def test_defaults_apply_when_there_is_no_config_file():
    cfg = _in_dir(lambda _: RunConfig.resolve(Args()))
    assert cfg.source is None and cfg.port == 8090 and cfg.log_level == "info"
    assert cfg.repo is None and cfg.sql_dialect == "mysql"


def test_config_file_is_picked_up_from_the_working_directory():
    def body(tmp):
        (tmp / CONFIG_NAME).write_text(SAMPLE, encoding="utf-8")
        return RunConfig.resolve(Args())

    cfg = _in_dir(body)
    assert cfg.port == 9000 and cfg.repo == Path("C:/checkout")
    assert cfg.log_level == "warn" and cfg.file_log_level == "debug"
    assert cfg.module_levels == {"update_object": "debug"}
    assert cfg.sql_dialect == "sqlite"
    assert cfg.dbc_dir == Path("C:/server/data/dbc")


def test_the_server_dbc_directory_is_optional():
    assert _in_dir(lambda _: RunConfig.resolve(Args())).dbc_dir is None


def test_the_logon_server_defaults_to_3724_on_any_address():
    cfg = _in_dir(lambda _: RunConfig.resolve(Args()))
    assert cfg.logon_port == 3724 and cfg.logon_ip is None


def test_a_port_given_anywhere_is_named_and_a_default_is_not():
    """A named world port overrides the one the realm list gives; the default
    8090 must not, or it would silently win over what the capture says."""
    def body(tmp):
        defaults = RunConfig.resolve(Args())
        (tmp / CONFIG_NAME).write_text("[capture]\nlogon_port = 3725\n", encoding="utf-8")
        from_file = RunConfig.resolve(Args())
        os.environ["TCT_PORT"] = "8090"
        try:
            from_env = RunConfig.resolve(Args())
        finally:
            del os.environ["TCT_PORT"]
        from_flag = RunConfig.resolve(Args(logon_ip="10.0.0.1"))
        return defaults, from_file, from_env, from_flag

    defaults, from_file, from_env, from_flag = _in_dir(body)
    assert not defaults.named
    assert from_file.named == {"logon_port"} and from_file.logon_port == 3725
    assert "port" in from_env.named                    # named, even as the default's value
    assert "logon_ip" in from_flag.named and from_flag.logon_ip == "10.0.0.1"


def test_precedence_is_file_then_environment_then_flag():
    def body(tmp):
        (tmp / CONFIG_NAME).write_text(SAMPLE, encoding="utf-8")
        os.environ["TCT_PORT"] = "7000"
        try:
            from_env = RunConfig.resolve(Args())
            from_flag = RunConfig.resolve(Args(port=6000, log_level="debug"))
        finally:
            del os.environ["TCT_PORT"]
        return from_env, from_flag

    from_env, from_flag = _in_dir(body)
    assert from_env.port == 7000                  # environment beats the file
    assert from_flag.port == 6000                 # flag beats the environment
    assert from_flag.log_level == "debug"


def test_debug_flag_is_a_shortcut_for_the_level():
    cfg = _in_dir(lambda _: RunConfig.resolve(Args(debug=True)))
    assert cfg.log_level == "debug"


def test_unknown_keys_and_levels_are_reported_but_not_fatal():
    def body(tmp):
        (tmp / CONFIG_NAME).write_text(
            '[log]\nlevel = "shouty"\n[nonsense]\nx = 1\n[output]\nsql_dialect = "oracle"\n',
            encoding="utf-8")
        return RunConfig.resolve(Args())

    cfg = _in_dir(body)
    text = " ".join(message for _, message in cfg.issues)
    assert all(level == "warn" for level, _ in cfg.issues)
    assert "shouty" in text and "nonsense" in text and "oracle" in text
    assert cfg.log_level == "info" and cfg.sql_dialect == "mysql"      # fell back


def test_a_value_of_the_wrong_type_is_reported_and_not_used():
    """`file = "false"` is a non-empty string, so file logging stayed on; and
    `port = 8090.9` became 8090 -- counted as a port the user named."""
    def body(tmp):
        (tmp / CONFIG_NAME).write_text('[capture]\nport = 8090.9\n[log]\nfile = "false"\n',
                                       encoding="utf-8")
        return RunConfig.resolve(Args())

    cfg = _in_dir(body)
    text = " ".join(message for _, message in cfg.issues)
    assert "8090.9" in text and "'false'" in text
    assert "port" not in cfg.named and cfg.port == 8090


def _resolve_text(text):
    def body(tmp):
        (tmp / CONFIG_NAME).write_text(text, encoding="utf-8")
        return RunConfig.resolve(Args())
    return _in_dir(body)


def test_a_path_or_database_port_of_the_wrong_type_is_a_config_error():
    """`repo = 1` went through Path() and came out a TypeError traceback; the
    same for the other path keys, and `port = "3306"` reached the mysql client
    as it was. No default is honest for them -- it would point the run at some
    other checkout, directory or database -- so the file is refused, naming
    the key as the file spells it."""
    for text, named in (('[capture]\nrepo = 1\n', "[capture] repo"),
                        ('[output]\ndir = 1\n', "[output] dir"),
                        ('[output]\ncache_dir = ["a"]\n', "[output] cache_dir"),
                        ('[log]\ndir = 1\n', "[log] dir"),
                        ('[server]\ndbc = true\n', "[server] dbc"),
                        ('[database]\nport = "3306"\n', "[database] port"),
                        ('[database]\nport = 3306.5\n', "[database] port"),
                        ('[database]\nclient = 7\n', "[database] client")):
        try:
            _resolve_text(text)
        except ValueError as exc:
            assert named in str(exc), (text, str(exc))
        else:
            raise AssertionError(f"{text!r} must be refused")


def test_a_path_and_a_database_port_of_the_right_type_still_resolve():
    cfg = _resolve_text('[capture]\nrepo = "x/y"\n[output]\ndir = "o"\ncache_dir = "c"\n'
                        '[log]\ndir = "l"\n[server]\ndbc = "d"\n'
                        '[database]\nport = 3307\nclient = "mysql"\n')
    assert cfg.repo == Path("x/y") and cfg.out_dir == Path("o") and cfg.cache_dir == Path("c")
    assert cfg.log_dir == Path("l") and cfg.dbc_dir == Path("d")
    assert cfg.database["port"] == 3307 and cfg.database["client"] == "mysql"


def test_an_explicit_config_path_that_is_missing_stops_the_run():
    try:
        RunConfig.resolve(Args(config="no/such/file.toml"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a config file named explicitly must exist")


# --------------------------------------------------------------------------
# log routing
# --------------------------------------------------------------------------

def _capture_logs(level="info", **kwargs):
    """Runs setup() against string streams and returns (stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    root = logging.getLogger(_log.ROOT)
    saved = (root.level, root.propagate, list(root.handlers))
    sys.stdout, sys.stderr = out, err
    try:
        _log.setup(level=level, log_file=None, **kwargs)
        log = _log.get_logger("probe")
        log.debug("a debug line")
        log.info("an info line")
        log.warning("a warn line")
        log.error("an error line")
        _log.get_logger("mod.update_object").debug("module detail")
        return out.getvalue(), err.getvalue()
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        # setup() sets the level, propagation and handlers of the `tct` logger;
        # put back all three, or the next test in this process inherits them.
        root.setLevel(saved[0])
        root.propagate = saved[1]
        root.handlers[:] = saved[2]
        logging.getLogger(f"{_log.ROOT}.mod.update_object").setLevel(logging.NOTSET)


def test_info_goes_to_stdout_and_warn_and_error_to_stderr():
    out, err = _capture_logs("info")
    assert "an info line" in out and "a debug line" not in out
    assert "a warn line" not in out and "an error line" not in out
    assert "a warn line" in err and "an error line" in err


def test_warn_level_hides_info_but_keeps_warnings():
    out, err = _capture_logs("warn")
    assert out.strip() == ""
    assert "a warn line" in err and "an error line" in err


def test_error_level_hides_warnings():
    _, err = _capture_logs("error")
    assert "a warn line" not in err and "an error line" in err


def test_a_module_level_overrides_the_console_level_for_that_module_only():
    out, _ = _capture_logs("info", module_levels={"update_object": "debug"})
    assert "module detail" in out          # the module's own debug got through
    assert "a debug line" not in out       # everything else stayed at info


def test_capturing_logs_leaves_the_tct_logger_as_it_found_it():
    """setup() sets the `tct` logger's level to the most verbose threshold
    asked for; the helper that drives it must put the level back, or a later
    test in the same process that expects a warning (one that watches a
    handler on a child logger) sees none."""
    root = logging.getLogger(_log.ROOT)
    level, handlers = root.level, list(root.handlers)
    _capture_logs("error")
    assert root.level == level
    assert root.handlers == handlers


def _logged_command_line(command_line):
    """What setup() leaves in the log file for the command line it is given."""
    root = logging.getLogger(_log.ROOT)
    saved = (root.level, root.propagate, list(root.handlers))
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.log"
        try:
            _log.setup(level="info", log_file=path, command_line=command_line)
        finally:
            sys.stdout, sys.stderr = real_out, real_err
            for handler in root.handlers:
                if handler not in saved[2]:
                    handler.close()          # a file left open cannot be removed on Windows
            root.setLevel(saved[0])
            root.propagate = saved[1]
            root.handlers[:] = saved[2]
        return path.read_text(encoding="utf-8")


def test_the_session_key_never_reaches_the_log_file():
    """The command line goes into logs/<stem>.log, which travels with handoffs;
    --session-key <hex> put the key that decrypts the whole capture in it."""
    for line in ("tct dump x --session-key 00ff", "tct dump x --session-key=00ff",
                 "tct dump x --session-k 00ff", "tct dump x --session=00ff",
                 "tct dump x --session-key 00ff --entry 5"):
        logged = _logged_command_line(line)
        assert "00ff" not in logged, line
        assert "tct dump x --ses" in logged, line         # the rest of the line is kept
    assert "--entry 5" in _logged_command_line("tct author x --session-key 00ff --entry 5")


def test_a_command_line_without_a_session_key_is_logged_as_it_was():
    line = "tct dump x --server-ip 10.0.0.5 --sessions-dir y"
    assert line in _logged_command_line(line)


def test_errors_and_warnings_are_counted_separately():
    _capture_logs("info")
    assert _log.error_count() == 1 and _log.warning_count() == 1


def test_the_example_config_leaves_the_world_server_to_the_realm_list():
    """The README says to copy it and that no port has to be given; a copy that
    named 8090 and 127.0.0.1 turned the realm-list detection off unasked."""
    example = (Path(__file__).parent.parent / "tct.example.toml").read_text(encoding="utf-8")

    def body(tmp):
        (tmp / CONFIG_NAME).write_text(example, encoding="utf-8")
        return RunConfig.resolve(Args())

    assert not {"port", "server_ip"} & _in_dir(body).named
