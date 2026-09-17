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
"""


class Args:
    """Stands in for the argparse namespace: unset flags are None."""

    def __init__(self, **kwargs):
        defaults = dict(config=None, repo=None, port=None, server_ip=None, log_level=None,
                        debug=False, out_dir=None, log_dir=None, no_log_file=False,
                        cache_dir=None, key_only=False)
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
        logging.getLogger(_log.ROOT).handlers.clear()
        logging.getLogger(_log.ROOT).addHandler(logging.NullHandler())
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


def test_errors_and_warnings_are_counted_separately():
    _capture_logs("info")
    assert _log.error_count() == 1 and _log.warning_count() == 1
