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
ENV_LOG_LEVEL = "TCT_LOG_LEVEL"

SQL_DIALECTS = ("mysql", "sqlite")
TEXT_LAYOUTS = ("grouped", "stream")

# section -> {key in file: attribute name}
SCHEMA: dict[str, dict[str, str]] = {
    "capture": {"repo": "repo", "port": "port", "server_ip": "server_ip"},
    "log": {"level": "log_level", "file_level": "file_log_level", "dir": "log_dir",
            "file": "log_to_file", "modules": "module_levels"},
    "output": {"dir": "out_dir", "cache_dir": "cache_dir", "sql_dialect": "sql_dialect",
               "text_layout": "text_layout"},
}

_DEFAULTS: dict[str, Any] = {
    "repo": None,
    "port": 8090,                    # this fork's WorldServerPort
    "server_ip": "127.0.0.1",
    "log_level": _log.DEFAULT_LEVEL,
    "file_log_level": None,          # None -> same as log_level
    "log_dir": "logs",
    "log_to_file": True,
    "module_levels": {},
    "out_dir": "out",
    "cache_dir": ".cache",
    "sql_dialect": "mysql",
    "text_layout": "grouped",
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    repo: Path | None                # tortoise-wow checkout; None -> numeric-only tables
    port: int
    server_ip: str
    log_level: str
    file_log_level: str | None
    module_levels: dict[str, str]
    log_dir: Path | None             # None -> console only
    out_dir: Path
    cache_dir: Path
    sql_dialect: str
    text_layout: str
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
        if path is not None:
            values.update(_read_file(path, issues))
        _apply_env(values, issues)
        _apply_args(values, args)
        _validate(values, issues)

        log_dir = None if not values["log_to_file"] else Path(values["log_dir"])
        return cls(
            repo=Path(values["repo"]) if values["repo"] else None,
            port=int(values["port"]),
            server_ip=str(values["server_ip"]),
            log_level=values["log_level"],
            file_log_level=values["file_log_level"],
            module_levels=dict(values["module_levels"]),
            log_dir=log_dir,
            out_dir=Path(values["out_dir"]),
            cache_dir=Path(values["cache_dir"]),
            sql_dialect=values["sql_dialect"],
            text_layout=values["text_layout"],
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


def _apply_env(values: dict[str, Any], issues: list[tuple[str, str]]) -> None:
    for name, key in ((ENV_REPO, "repo"), (ENV_PORT, "port"),
                      (ENV_SERVER_IP, "server_ip"), (ENV_LOG_LEVEL, "log_level")):
        raw = os.environ.get(name)
        if raw:
            values[key] = raw


def _apply_args(values: dict[str, Any], args) -> None:
    """Only flags actually given override; argparse defaults are None."""
    for attribute, flag in (("repo", "repo"), ("port", "port"), ("server_ip", "server_ip"),
                            ("log_level", "log_level"), ("out_dir", "out_dir"),
                            ("log_dir", "log_dir"), ("cache_dir", "cache_dir")):
        given = getattr(args, flag, None)
        if given is not None:
            values[attribute] = given
    if getattr(args, "debug", False):
        values["log_level"] = "debug"
    if getattr(args, "no_log_file", False):
        values["log_to_file"] = False


def _validate(values: dict[str, Any], issues: list[tuple[str, str]]) -> None:
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
