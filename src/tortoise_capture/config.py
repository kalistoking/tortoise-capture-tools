"""Run configuration: config file, then environment, then command line.

The file is TOML -- `parametr = hodnota` with `#` comments and `[section]`
groups, i.e. the classic key/value style, but with real types: `port = 8090`
is an integer, `file = true` is a boolean and `modules` is a table. Nothing
downstream has to guess whether "false" meant false. `tomllib` is in the
standard library, so this adds no dependency.

Precedence, lowest to highest: built-in default -> config file -> environment
-> command-line flag. Anything not given at a level falls through to the one
below, so a config file sets the everyday values and a flag overrides one of
them for a single run.

Problems found while reading the file (unknown key, bad level name) are
collected in `issues` rather than logged here: logging is not configured yet
at that point -- the file is what configures it. The CLI replays them once
the handlers exist.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log as _log

CONFIG_NAME = "tct.toml"

ENV_CONFIG = "TCT_CONFIG"
ENV_REPO = "TCT_REPO"
ENV_PORT = "TCT_PORT"
ENV_SERVER_IP = "TCT_SERVER_IP"
ENV_LOGON_PORT = "TCT_LOGON_PORT"
ENV_LOGON_IP = "TCT_LOGON_IP"
ENV_LOG_LEVEL = "TCT_LOG_LEVEL"

SQL_DIALECTS = ("mysql", "sqlite")
TEXT_LAYOUTS = ("grouped", "stream")

# section -> {key in file: attribute name}
SCHEMA: dict[str, dict[str, str]] = {
    "capture": {"repo": "repo", "port": "port", "server_ip": "server_ip",
                "logon_port": "logon_port", "logon_ip": "logon_ip"},
    "log": {"level": "log_level", "file_level": "file_log_level", "dir": "log_dir",
            "file": "log_to_file", "modules": "module_levels"},
    "output": {"dir": "out_dir", "cache_dir": "cache_dir", "sql_dialect": "sql_dialect",
               "text_layout": "text_layout"},
    # Read-only, and only for the lookups authoring needs. The password is not
    # a key here on purpose: it comes from TCT_DB_PASSWORD, so a config file
    # stays safe to share.
    "database": {"client": "db_client", "host": "db_host", "port": "db_port",
                 "user": "db_user", "world": "db_world"},
    # The target server's own client data, for what the database defers to it.
    "server": {"dbc": "dbc_dir"},
}

_DEFAULTS: dict[str, Any] = {
    "repo": None,
    "port": 8090,                    # this fork's WorldServerPort
    "server_ip": "127.0.0.1",
    "logon_port": 3724,              # realmd's port; the world address comes from its realm list
    "logon_ip": None,                # None -> any address
    "log_level": _log.DEFAULT_LEVEL,
    "file_log_level": None,          # None -> same as log_level
    "log_dir": "logs",
    "log_to_file": True,
    "module_levels": {},
    "out_dir": "out",
    "cache_dir": ".cache",
    "sql_dialect": "mysql",
    "text_layout": "grouped",
    "db_client": None,
    "db_host": "127.0.0.1",
    "db_port": 3306,
    "db_user": "mangos",
    "db_world": "tw_world",
    "dbc_dir": None,
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    repo: Path | None                # tortoise-wow checkout; None -> numeric-only tables
    port: int
    server_ip: str
    logon_port: int
    logon_ip: str | None
    log_level: str
    file_log_level: str | None
    module_levels: dict[str, str]
    log_dir: Path | None             # None -> console only
    out_dir: Path
    cache_dir: Path
    sql_dialect: str
    text_layout: str
    database: dict[str, Any]
    dbc_dir: Path | None = None      # the server's data/dbc; None -> model scales unchecked
    # Capture settings given in a file, the environment or a flag rather than
    # left at their default. A named world port overrides the one the realm
    # list gives; a default one must not.
    named: frozenset[str] = frozenset()
    quiet: bool = False
    source: Path | None = None       # the config file actually used
    issues: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def log_file(self, stem: str) -> Path | None:
        return None if self.log_dir is None else self.log_dir / f"{stem}.log"

    def describe(self) -> str:
        where = self.source or "defaults"
        return (f"config from {where}: repo={self.repo} port={self.port} "
                f"log={self.log_level}")

    # -- construction ------------------------------------------------------

    @classmethod
    def resolve(cls, args) -> "RunConfig":
        values = dict(_DEFAULTS)
        issues: list[tuple[str, str]] = []

        path = _find_config(getattr(args, "config", None), issues)
        named: set[str] = set()
        if path is not None:
            from_file = _read_file(path, issues)
            values.update(from_file)
            named.update(from_file)
        named.update(_apply_env(values, issues))
        named.update(_apply_args(values, args))
        _validate(values, issues, named, path)

        log_dir = None if not values["log_to_file"] else Path(values["log_dir"])
        return cls(
            repo=Path(values["repo"]) if values["repo"] else None,
            port=int(values["port"]),
            server_ip=str(values["server_ip"]),
            logon_port=int(values["logon_port"]),
            logon_ip=str(values["logon_ip"]) if values["logon_ip"] else None,
            log_level=values["log_level"],
            file_log_level=values["file_log_level"],
            module_levels=dict(values["module_levels"]),
            log_dir=log_dir,
            out_dir=Path(values["out_dir"]),
            cache_dir=Path(values["cache_dir"]),
            sql_dialect=values["sql_dialect"],
            text_layout=values["text_layout"],
            database={"client": values["db_client"], "host": values["db_host"],
                      "port": values["db_port"], "user": values["db_user"],
                      "world": values["db_world"]},
            dbc_dir=Path(values["dbc_dir"]) if values["dbc_dir"] else None,
            named=frozenset(named & set(SCHEMA["capture"].values())),
            quiet=bool(getattr(args, "key_only", False)),
            source=path,
            issues=tuple(issues),
        )


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def _find_config(explicit: str | None, issues: list[tuple[str, str]]) -> Path | None:
    """--config, then $TCT_CONFIG, then ./tct.toml.

    A path given explicitly and missing is an error worth stopping for; the
    implicit ./tct.toml simply may not exist.
    """
    for candidate, required in ((explicit, True), (os.environ.get(ENV_CONFIG), True),
                                (CONFIG_NAME, False)):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
        if required:
            raise FileNotFoundError(f"config file not found: {path}")
    return None


def _read_file(path: Path, issues: list[tuple[str, str]]) -> dict[str, Any]:
    with open(path, "rb") as fp:
        raw = tomllib.load(fp)

    values: dict[str, Any] = {}
    for section, contents in raw.items():
        known = SCHEMA.get(section)
        if known is None:
            issues.append(("warn", f"{path}: unknown section [{section}], ignored"))
            continue
        if not isinstance(contents, dict):
            issues.append(("warn", f"{path}: [{section}] must be a table, ignored"))
            continue
        for key, value in contents.items():
            attribute = known.get(key)
            if attribute is None:
                issues.append(("warn", f"{path}: unknown key {key!r} in [{section}], ignored"))
                continue
            values[attribute] = value
    return values


def _apply_env(values: dict[str, Any], issues: list[tuple[str, str]]) -> set[str]:
    given = set()
    for name, key in ((ENV_REPO, "repo"), (ENV_PORT, "port"),
                      (ENV_SERVER_IP, "server_ip"), (ENV_LOGON_PORT, "logon_port"),
                      (ENV_LOGON_IP, "logon_ip"), (ENV_LOG_LEVEL, "log_level")):
        raw = os.environ.get(name)
        if raw:
            values[key] = raw
            given.add(key)
    return given


def _apply_args(values: dict[str, Any], args) -> set[str]:
    """Only flags actually given override; argparse defaults are None."""
    named = set()
    for attribute, flag in (("repo", "repo"), ("port", "port"), ("server_ip", "server_ip"),
                            ("logon_port", "logon_port"), ("logon_ip", "logon_ip"),
                            ("log_level", "log_level"), ("out_dir", "out_dir"),
                            ("log_dir", "log_dir"), ("cache_dir", "cache_dir")):
        given = getattr(args, flag, None)
        if given is not None:
            values[attribute] = given
            named.add(attribute)
    if getattr(args, "debug", False):
        values["log_level"] = "debug"
    if getattr(args, "no_log_file", False):
        values["log_to_file"] = False
    return named


def _whole(value: Any) -> bool:
    """An int, or the digits of one (the environment gives strings)."""
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and value.isdigit())


def _setting(attribute: str) -> str:
    """`[section] key`: an attribute as the config file spells it."""
    for section, keys in SCHEMA.items():
        for key, name in keys.items():
            if name == attribute:
                return f"[{section}] {key}"
    return attribute


# Settings with no honest default to fall back on: a path of the wrong type
# used as the default would run against another checkout, directory or
# database than the one the file named. Those are refused (the config error
# main reports, exit 1) rather than warned about like a port or a flag below.
_PATH_KEYS = ("repo", "log_dir", "out_dir", "cache_dir", "dbc_dir", "db_client")


def _require_types(values: dict[str, Any], source: Path | None) -> None:
    where = f"{source}: " if source else ""
    for key in _PATH_KEYS:
        value = values.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{where}{_setting(key)} must be a path (a string), "
                             f"not {value!r}")
    port = values.get("db_port")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(f"{where}{_setting('db_port')} must be a whole number, not {port!r}")


def _validate(values: dict[str, Any], issues: list[tuple[str, str]],
              named: set[str] | None = None, source: Path | None = None) -> None:
    _require_types(values, source)

    # A value of the wrong type is reported and not used: "false" is a true
    # string, and 8090.9 would pass as a port the user named.
    for key, fits, kind in (("port", _whole, "a whole number"),
                            ("logon_port", _whole, "a whole number"),
                            ("log_to_file", lambda v: isinstance(v, bool), "true or false")):
        if key in values and not fits(values[key]):
            issues.append(("warn", f"{key} must be {kind}, not {values[key]!r}; "
                                   f"using {_DEFAULTS[key]!r}"))
            values[key] = _DEFAULTS[key]
            if named is not None:
                named.discard(key)

    for key, default in (("log_level", _log.DEFAULT_LEVEL), ("file_log_level", None)):
        level = values.get(key)
        if level is not None and _log.level_value(level) is None:
            issues.append(("warn", f"unknown log level {level!r} for {key}; "
                                   f"using {default or values['log_level']!r} "
                                   f"(known: {', '.join(_log.LEVEL_NAMES)})"))
            values[key] = default

    modules = values.get("module_levels") or {}
    if not isinstance(modules, dict):
        issues.append(("warn", "[log] modules must be a table of module = level; ignored"))
        values["module_levels"] = {}
    else:
        for name, level in list(modules.items()):
            if _log.level_value(level) is None:
                issues.append(("warn", f"unknown log level {level!r} for module {name!r}; ignored"))
                modules.pop(name)

    for key, allowed in (("sql_dialect", SQL_DIALECTS), ("text_layout", TEXT_LAYOUTS)):
        if values[key] not in allowed:
            issues.append(("warn", f"unknown {key} {values[key]!r}; using {allowed[0]!r}"))
            values[key] = allowed[0]
